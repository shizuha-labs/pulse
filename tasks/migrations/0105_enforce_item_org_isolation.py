"""Make task identity and relationships organization-local.

Legacy cross-organization relationships are archived before removal. Active
dependency meaning is retained by materializing one local blocker per
``(owning project/routing context, former external blocker)`` and sharing that
local blocker across compatible dependent tasks.

PostgreSQL triggers cover bulk/query writers that bypass model ``save()`` and
make both project ownership and a task's owning organization immutable.
"""

import re

from django.db import migrations, models
from django.utils import timezone

from ._org_safe_condition_capsule_v1 import (
    build_capsule,
    merge_unknown_tag,
    upsert_capsule_description,
)


TERMINAL_STATUSES = {
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'silenced', 'applied', 'change_rejected',
    'rfc_approved', 'rfc_implementation_linked', 'rfc_validation',
}
BLOCK_LINK_TYPES = {'blocks', 'is_blocked_by', 'blocked_by'}
ORGANIZATION_IDS = {
    'shizuha': 1,
    'shizuha-digital': 2,
    'shizuha-trading': 3,
    'personal-3': 4,
}
ORGANIZATION_SLUGS = {
    organization_id: organization_slug
    for organization_slug, organization_id in ORGANIZATION_IDS.items()
}
TEAM_ALIASES = {
    'admin_ops': 'admin-ops',
    'ceo-decisions': 'human-ops',
    'leads': 'human-ops',
    'triage': 'human-ops',
    'security-triage': 'security',
    'analytics': 'research-analytics',
    'research': 'research-analytics',
    'personal-accounting': 'accounting',
    'digital-accounting': 'accounting',
    'digital-quant': 'quant',
    'securities-engineering': 'engineering',
    'trading-engineering': 'engineering',
    'governance': 'admin-ops',
    'program-management': 'admin-ops',
}
def _canonical_team(value):
    normalized = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    return TEAM_ALIASES.get(normalized, normalized)


def _member_email(member):
    if isinstance(member, dict):
        member = (
            member.get('email')
            or member.get('user_email')
            or member.get('agent_email')
            or ''
        )
    return str(member or '').strip().lower()


def _email_aliases(email):
    normalized = _member_email(email)
    if not normalized:
        return set()
    aliases = {normalized}
    if normalized.endswith('@shizuha.com'):
        aliases.add(
            normalized[:-len('@shizuha.com')] + '@agents.shizuha.io'
        )
    elif normalized.endswith('@agents.shizuha.io'):
        aliases.add(
            normalized[:-len('@agents.shizuha.io')] + '@shizuha.com'
        )
    return aliases


def _merge_tiers(rosters):
    merged = []
    seen = set()
    for roster in rosters:
        for tier_index, tier in enumerate(roster.tiers or []):
            while len(merged) <= tier_index:
                merged.append([])
            for member in tier or []:
                aliases = _email_aliases(member)
                identity_key = sorted(aliases)[0] if aliases else repr(member)
                if identity_key in seen:
                    continue
                seen.add(identity_key)
                merged[tier_index].append(member)
    return merged


def _merge_emails(rosters, field_name):
    merged = []
    seen = set()
    for roster in rosters:
        for member in getattr(roster, field_name) or []:
            aliases = _email_aliases(member)
            identity_key = sorted(aliases)[0] if aliases else repr(member)
            if identity_key in seen:
                continue
            seen.add(identity_key)
            merged.append(member)
    return merged


def _organization_id(item, project_orgs):
    return project_orgs.get(item.project_id)


def _is_active_owned_task(item):
    status = str(item.status or '').strip().lower().replace('-', '_')
    concretely_owned = bool(
        item.assignee_id and str(item.assignee_email or '').strip()
    )
    team_owned = bool(str(item.assignment_group or '').strip())
    return (
        str(item.mode or '').strip().lower() == 'task'
        and status not in TERMINAL_STATUSES
        and (concretely_owned or team_owned)
    )


def _blocked_dependency(link):
    if link.link_type == 'blocks':
        return link.target_item, link.source_item
    if link.link_type in {'is_blocked_by', 'blocked_by'}:
        return link.source_item, link.target_item
    return None, None


def _next_item_key(Project, Item, project_id):
    if project_id is None:
        return None
    project = Project.objects.select_for_update().get(pk=project_id)
    number = max(int(project.next_item_number or 1), 1)
    item_key = f'{project.key}-{number}'
    while Item.objects.filter(item_key=item_key).exists():
        number += 1
        item_key = f'{project.key}-{number}'
    project.next_item_number = number + 1
    project.save(update_fields=['next_item_number'])
    return item_key


def _initial_status(apps, workflow_name):
    Workflow = apps.get_model('tasks', 'Workflow')
    workflow = (
        Workflow.objects
        .filter(slug=workflow_name)
        .select_related('initial_status')
        .first()
    )
    if workflow is not None and workflow.initial_status_id:
        return workflow.initial_status.slug
    return 'todo' if workflow_name == 'simple' else 'open'


def backfill_team_roster_organization_ids(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    unknown = []
    grouped = {}
    for roster in TeamRoster.objects.all().order_by('id'):
        organization_slug = str(
            roster.organization_slug or 'shizuha'
        ).strip().lower()
        organization_id = ORGANIZATION_IDS.get(organization_slug)
        if organization_id is None:
            unknown.append(organization_slug)
            continue
        grouped.setdefault(
            (organization_id, _canonical_team(roster.team_name)),
            [],
        ).append(roster)
    if unknown:
        raise RuntimeError(
            'Cannot enforce organization-scoped Team membership for unknown '
            f'organization slugs: {sorted(set(unknown))}'
        )

    for (organization_id, team_name), rosters in grouped.items():
        survivor = next(
            (
                roster for roster in rosters
                if str(roster.team_name or '').strip().lower() == team_name
            ),
            rosters[0],
        )
        originals = [
            {
                'id': roster.id,
                'team_name': roster.team_name,
                'organization_slug': roster.organization_slug,
                'tiers': roster.tiers,
                'junior_emails': roster.junior_emails,
                'is_active': roster.is_active,
                'note': roster.note,
                'updated_by': roster.updated_by,
            }
            for roster in rosters
        ]
        merged_state = {
            'team_name': team_name,
            'organization_slug': ORGANIZATION_SLUGS[organization_id],
            'organization_id': organization_id,
            'tiers': _merge_tiers(rosters),
            'junior_emails': _merge_emails(rosters, 'junior_emails'),
            'is_active': any(roster.is_active for roster in rosters),
            'note': survivor.note,
            'updated_by': 'org-isolation-migration',
        }
        if (
            len(rosters) > 1
            or originals[0]['team_name'] != team_name
            or survivor.organization_id != organization_id
            or survivor.organization_slug != merged_state['organization_slug']
        ):
            Archive.objects.create(
                kind='team_roster_alias_group',
                object_key=f'{organization_id}:{team_name}',
                payload={
                    'survivor_id': survivor.id,
                    'originals': originals,
                    'merged_state': merged_state,
                },
            )
        TeamRoster.objects.filter(pk=survivor.pk).update(**merged_state)
        duplicate_ids = [
            roster.id for roster in rosters if roster.id != survivor.id
        ]
        if duplicate_ids:
            TeamRoster.objects.filter(pk__in=duplicate_ids).delete()


def clear_team_roster_organization_ids(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    for row in Archive.objects.filter(
        kind='team_roster_alias_group',
    ).order_by('-id'):
        payload = row.payload or {}
        survivor_id = payload.get('survivor_id')
        merged_state = payload.get('merged_state') or {}
        survivor = TeamRoster.objects.filter(pk=survivor_id).first()
        if survivor is None:
            raise RuntimeError(
                'Refusing to reverse organization-scoped Team aliases because '
                f'merged roster {survivor_id} no longer exists.'
            )
        current_state = {
            'team_name': survivor.team_name,
            'organization_slug': survivor.organization_slug,
            'organization_id': survivor.organization_id,
            'tiers': survivor.tiers,
            'junior_emails': survivor.junior_emails,
            'is_active': survivor.is_active,
            'note': survivor.note,
            'updated_by': survivor.updated_by,
        }
        if current_state != merged_state:
            raise RuntimeError(
                'Refusing to reverse organization-scoped Team aliases after '
                f'roster {survivor_id} changed.'
            )
        TeamRoster.objects.filter(pk=survivor_id).delete()
        for original in payload.get('originals') or []:
            TeamRoster.objects.create(
                id=original['id'],
                team_name=original['team_name'],
                organization_slug=original['organization_slug'],
                organization_id=None,
                tiers=original.get('tiers') or [],
                junior_emails=original.get('junior_emails') or [],
                is_active=bool(original.get('is_active')),
                note=original.get('note') or '',
                updated_by=original.get('updated_by') or '',
            )
    TeamRoster.objects.update(organization_id=None)


def remove_cross_organization_relationships(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    ItemLink = apps.get_model('tasks', 'ItemLink')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    Project = apps.get_model('tasks', 'Project')
    TeamRoster = apps.get_model('tasks', 'TeamRoster')

    # Grandfathered project-less tasks have no tenant identity and cannot meet
    # the task-page ownership contract. Attach them deterministically: an issue
    # key mentioned in the title selects that project, the known personal debit
    # card task belongs to BKSH, and remaining platform legacy work lands in AT.
    projects_by_key = {}
    for project in Project.objects.order_by('org_id', 'id'):
        projects_by_key.setdefault(str(project.key or '').upper(), project)
    fallback_project = projects_by_key.get('AT')
    if fallback_project is None and Item.objects.filter(
        mode='task',
        project_id__isnull=True,
    ).exists():
        raise RuntimeError(
            'Cannot scope legacy tasks because the AT fallback project is missing.'
        )
    for item in Item.objects.filter(
        mode='task',
        project_id__isnull=True,
    ).order_by('id').iterator():
        title = str(item.title or '')
        project = None
        if item.id == 5654 and title.strip() == 'SBI Debit Card Renewal':
            project = projects_by_key.get('BKSH')
        if project is None:
            match = re.search(r'\b([A-Z][A-Z0-9]{1,9})-\d+\b', title.upper())
            if match:
                project = projects_by_key.get(match.group(1))
        project = project or fallback_project
        new_item_key = item.item_key or _next_item_key(
            Project,
            Item,
            project.id,
        )
        Archive.objects.create(
            kind='item_scope',
            object_key=str(item.id),
            payload={
                'item_id': item.id,
                'old_project_id': None,
                'old_item_key': item.item_key,
                'new_project_id': project.id,
                'new_item_key': new_item_key,
                'updated_at': (
                    item.updated_at.isoformat() if item.updated_at else None
                ),
            },
        )
        Item.objects.filter(pk=item.pk).update(
            project_id=project.id,
            item_key=new_item_key,
        )

    project_orgs = dict(Project.objects.values_list('id', 'org_id'))

    # Canonicalize legacy team labels and drain every concrete owner that is
    # not authorized by the task organization's exact Team roster. Keeping the
    # assignment_group preserves ownership by the correct org-local queue; the
    # normal resolver can then select a live member. Direct assignments to an
    # identity known only in another organization's roster are cleared too.
    roster_members = {}
    identity_organizations = {}
    active_roster_keys = set()
    for roster in TeamRoster.objects.filter(is_active=True).iterator():
        roster_key = (
            roster.organization_id,
            _canonical_team(roster.team_name),
        )
        active_roster_keys.add(roster_key)
        members = []
        for tier in roster.tiers or []:
            members.extend(tier or [])
        members.extend(roster.junior_emails or [])
        for member in members:
            aliases = _email_aliases(member)
            roster_members.setdefault(roster_key, set()).update(aliases)
            for alias in aliases:
                identity_organizations.setdefault(alias, set()).add(
                    roster.organization_id
                )

    for item in Item.objects.filter(project_id__isnull=False).order_by('id').iterator():
        old_group = str(item.assignment_group or '')
        group_key = _canonical_team(old_group)
        new_group = group_key if old_group.strip() else item.assignment_group
        assigned = bool(
            item.assignee_id or str(item.assignee_email or '').strip()
        )
        invalid_assignment = False
        if assigned:
            assignee_aliases = _email_aliases(item.assignee_email)
            task_org_id = project_orgs.get(item.project_id)
            if not assignee_aliases:
                invalid_assignment = True
            elif group_key:
                exact_key = (task_org_id, group_key)
                allowed_members = roster_members.get(exact_key, set())
                invalid_assignment = not bool(
                    assignee_aliases & allowed_members
                )
            else:
                known_orgs = set()
                for alias in assignee_aliases:
                    known_orgs.update(
                        identity_organizations.get(alias, set())
                    )
                invalid_assignment = bool(
                    known_orgs and task_org_id not in known_orgs
                )

        new_assignee_id = None if invalid_assignment else item.assignee_id
        new_assignee_email = (
            None if invalid_assignment else item.assignee_email
        )
        if (
            new_group == old_group
            and new_assignee_id == item.assignee_id
            and new_assignee_email == item.assignee_email
        ):
            continue
        Archive.objects.create(
            kind='item_assignment',
            object_key=str(item.id),
            payload={
                'item_id': item.id,
                'old_assignment_group': item.assignment_group,
                'old_assignee_id': item.assignee_id,
                'old_assignee_email': item.assignee_email,
                'new_assignment_group': new_group,
                'new_assignee_id': new_assignee_id,
                'new_assignee_email': new_assignee_email,
                'reason': (
                    'cross_organization_assignee'
                    if invalid_assignment
                    else 'canonical_team_alias'
                ),
            },
        )
        Item.objects.filter(pk=item.pk).update(
            assignment_group=new_group,
            assignee_id=new_assignee_id,
            assignee_email=new_assignee_email,
        )

    invalid_links = []
    invalid_link_ids = set()
    for link in ItemLink.objects.select_related(
        'source_item__project',
        'target_item__project',
    ).iterator():
        source_org_id = _organization_id(link.source_item, project_orgs)
        target_org_id = _organization_id(link.target_item, project_orgs)
        # Both-unscoped legacy relationships remain valid. If either item is
        # organization-owned, both must resolve to the exact same organization.
        if source_org_id != target_org_id:
            invalid_links.append(link)
            invalid_link_ids.add(link.id)

    Archive.objects.bulk_create([
        Archive(
            kind='item_link',
            object_key=str(link.id),
            payload={
                'id': str(link.id),
                'source_item_id': link.source_item_id,
                'target_item_id': link.target_item_id,
                'link_type': link.link_type,
                'created_by_id': link.created_by_id,
                'created_by_email': link.created_by_email,
                'created_at': (
                    link.created_at.isoformat() if link.created_at else None
                ),
            },
        )
        for link in invalid_links
    ])

    # Preserve every distinct active dependency without preserving any shared
    # cross-org task. Project/access and routing context are part of the group:
    # a private project's dependency must not be materialized in some other
    # project's visibility boundary merely because both share an organization.
    replacement_groups = {}
    for link in invalid_links:
        if link.link_type not in BLOCK_LINK_TYPES:
            continue
        dependent, blocker = _blocked_dependency(link)
        if (
            dependent is not None
            and str(dependent.status or '').strip().lower() == 'blocked'
            and _is_active_owned_task(blocker)
        ):
            target_org_id = _organization_id(dependent, project_orgs)
            workflow_name = dependent.workflow_name or 'simple'
            assignment_group = dependent.assignment_group or 'admin-ops'
            group = replacement_groups.setdefault(
                (
                    target_org_id,
                    dependent.project_id,
                    workflow_name,
                    assignment_group,
                    blocker.id,
                ),
                {
                    'former_blocker': blocker,
                    'dependent_ids': set(),
                },
            )
            group['dependent_ids'].add(dependent.id)

    for (
        _target_org_id,
        _target_project_id,
        _workflow_name,
        _assignment_group,
        _former_id,
    ), group in sorted(
        replacement_groups.items(),
        key=lambda row: (
            -1 if row[0][0] is None else int(row[0][0]),
            -1 if row[0][1] is None else int(row[0][1]),
            str(row[0][2]),
            str(row[0][3]),
            int(row[0][4]),
        ),
    ):
        dependent_ids = sorted(group['dependent_ids'])
        dependent = Item.objects.select_related('project').get(
            pk=dependent_ids[0],
        )
        former_blocker = group['former_blocker']
        workflow_name = _workflow_name
        former_key = former_blocker.item_key or f'#{former_blocker.id}'
        former_org = _organization_id(former_blocker, project_orgs)
        migration_stamp = (
            f'{timezone.now().isoformat()} '
            '(0105_enforce_item_org_isolation)'
        )
        capsule = build_capsule(
            source_key=former_key,
            source_title=former_blocker.title,
            source_description=former_blocker.description,
            source_status=former_blocker.status,
            local_owning_team=_assignment_group,
            migration_stamp=migration_stamp,
        )
        local_metadata = {
            'org_isolation_replacement': True,
            'dependent_item_ids': dependent_ids,
            'former_external_item_id': former_blocker.id,
            'former_external_item_key': former_blocker.item_key,
            'former_external_org_id': former_org,
            'target_project_id': _target_project_id,
            'org_safe_condition_capsule': capsule,
        }
        local_blocker = Item.objects.create(
            project_id=dependent.project_id,
            item_key=_next_item_key(
                Project,
                Item,
                dependent.project_id,
            ),
            mode='task',
            item_type='task.generic',
            title=(
                f'[Org-local dependency] Resolve condition formerly tracked by '
                f'{former_key}'
            )[:400],
            description=(
                upsert_capsule_description(
                    'Organization-isolation migration: this task is the local, '
                    'independently owned replacement for a legacy dependency on a '
                    'task from another organization. Verify the external condition '
                    'from this organization’s own evidence and complete this blocker '
                    'when it is satisfied; do not recreate a cross-organization task '
                    'link.',
                    capsule,
                )
            ),
            source='org_isolation_migration',
            status=_initial_status(apps, workflow_name),
            priority=dependent.priority or 'normal',
            workflow_name=workflow_name,
            assignment_group=_assignment_group,
            tags=merge_unknown_tag(
                None,
                capsule['extraction_state'] == 'unknown',
            ),
            created_by_id=0,
            created_by_email='system@pulse',
            metadata=local_metadata,
        )
        replacement_links = [
            ItemLink.objects.create(
                source_item_id=local_blocker.id,
                target_item_id=dependent_id,
                link_type='blocks',
                created_by_id=0,
                created_by_email='system@pulse',
            )
            for dependent_id in dependent_ids
        ]
        Archive.objects.create(
            kind='replacement_task',
            object_key=str(local_blocker.id),
            payload={
                'item_id': local_blocker.id,
                'link_ids': [str(link.id) for link in replacement_links],
                'dependent_item_ids': dependent_ids,
                'former_external_item_id': former_blocker.id,
                'target_org_id': _target_org_id,
                'target_project_id': _target_project_id,
                'initial_state': {
                    'status': local_blocker.status,
                    'assignee_id': local_blocker.assignee_id,
                    'assignee_email': local_blocker.assignee_email,
                    'assignment_group': local_blocker.assignment_group,
                    'metadata': local_blocker.metadata,
                },
            },
        )

    if invalid_link_ids:
        ItemLink.objects.filter(id__in=invalid_link_ids).delete()

    for field_name in ('parent_item', 'parent_alert'):
        invalid_item_ids = []
        queryset = Item.objects.exclude(
            **{f'{field_name}__isnull': True}
        ).select_related(
            'project',
            f'{field_name}__project',
        )
        for item in queryset.iterator():
            parent = getattr(item, field_name)
            if (
                _organization_id(item, project_orgs)
                != _organization_id(parent, project_orgs)
            ):
                invalid_item_ids.append(item.id)
        if invalid_item_ids:
            Archive.objects.bulk_create([
                Archive(
                    kind='item_parent',
                    object_key=f'{item_id}:{field_name}',
                    payload={
                        'item_id': item_id,
                        'field_name': field_name,
                        'parent_id': (
                            Item.objects
                            .filter(pk=item_id)
                            .values_list(f'{field_name}_id', flat=True)
                            .get()
                        ),
                    },
                )
                for item_id in invalid_item_ids
            ])
            Item.objects.filter(id__in=invalid_item_ids).update(
                **{field_name: None}
            )


def restore_cross_organization_relationships(apps, schema_editor):
    """Best-effort reverse using the durable audit snapshot."""
    Attachment = apps.get_model('tasks', 'Attachment')
    Comment = apps.get_model('tasks', 'Comment')
    Item = apps.get_model('tasks', 'Item')
    ItemLink = apps.get_model('tasks', 'ItemLink')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    WorkLog = apps.get_model('tasks', 'WorkLog')

    for row in Archive.objects.filter(kind='replacement_task').order_by('-id'):
        payload = row.payload or {}
        item_id = payload.get('item_id')
        if item_id:
            replacement = Item.objects.filter(pk=item_id).first()
            if replacement is None:
                continue
            initial_state = payload.get('initial_state') or {}
            current_state = {
                'status': replacement.status,
                'assignee_id': replacement.assignee_id,
                'assignee_email': replacement.assignee_email,
                'assignment_group': replacement.assignment_group,
                'metadata': replacement.metadata,
            }
            if (
                current_state != initial_state
                or Comment.objects.filter(item_id=item_id).exists()
                or Attachment.objects.filter(item_id=item_id).exists()
                or WorkLog.objects.filter(item_id=item_id).exists()
            ):
                raise RuntimeError(
                    'Refusing to reverse organization isolation after work was '
                    f'recorded on replacement task {item_id}. Preserve or move '
                    'that work before a manual rollback.'
                )
            Item.objects.filter(pk=item_id).delete()

    for row in Archive.objects.filter(kind='item_link').order_by('id'):
        payload = row.payload or {}
        source_id = payload.get('source_item_id')
        target_id = payload.get('target_item_id')
        if not (
            source_id and target_id
            and Item.objects.filter(pk=source_id).exists()
            and Item.objects.filter(pk=target_id).exists()
        ):
            continue
        link_id = payload.get('id')
        if ItemLink.objects.filter(pk=link_id).exists():
            continue
        link = ItemLink.objects.create(
            id=link_id,
            source_item_id=source_id,
            target_item_id=target_id,
            link_type=payload.get('link_type') or 'relates_to',
            created_by_id=payload.get('created_by_id') or 0,
            created_by_email=payload.get('created_by_email') or 'system@pulse',
        )
        if payload.get('created_at'):
            ItemLink.objects.filter(pk=link.pk).update(
                created_at=payload['created_at'],
            )

    for row in Archive.objects.filter(kind='item_parent').order_by('id'):
        payload = row.payload or {}
        item_id = payload.get('item_id')
        parent_id = payload.get('parent_id')
        field_name = payload.get('field_name')
        if (
            field_name in {'parent_item', 'parent_alert'}
            and Item.objects.filter(pk=item_id).exists()
            and Item.objects.filter(pk=parent_id).exists()
        ):
            Item.objects.filter(pk=item_id).update(
                **{f'{field_name}_id': parent_id},
            )

    for row in Archive.objects.filter(kind='item_assignment').order_by('-id'):
        payload = row.payload or {}
        item = Item.objects.filter(pk=payload.get('item_id')).first()
        if item is None:
            continue
        current_state = {
            'assignment_group': item.assignment_group,
            'assignee_id': item.assignee_id,
            'assignee_email': item.assignee_email,
        }
        migrated_state = {
            'assignment_group': payload.get('new_assignment_group'),
            'assignee_id': payload.get('new_assignee_id'),
            'assignee_email': payload.get('new_assignee_email'),
        }
        if current_state != migrated_state:
            raise RuntimeError(
                'Refusing to restore a legacy cross-organization assignment '
                f'after task {item.id} was re-routed.'
            )
        Item.objects.filter(pk=item.pk).update(
            assignment_group=payload.get('old_assignment_group'),
            assignee_id=payload.get('old_assignee_id'),
            assignee_email=payload.get('old_assignee_email'),
        )

    for row in Archive.objects.filter(kind='item_scope').order_by('-id'):
        payload = row.payload or {}
        item = Item.objects.filter(pk=payload.get('item_id')).first()
        if item is None:
            continue
        current_updated_at = (
            item.updated_at.isoformat() if item.updated_at else None
        )
        if (
            item.project_id != payload.get('new_project_id')
            or item.item_key != payload.get('new_item_key')
            or current_updated_at != payload.get('updated_at')
        ):
            raise RuntimeError(
                'Refusing to remove organization ownership after work was '
                f'recorded on migrated task {item.id}.'
            )
        Item.objects.filter(pk=item.pk).update(
            project_id=payload.get('old_project_id'),
            item_key=payload.get('old_item_key'),
        )


CREATE_CONSTRAINTS_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_item_organization_id(p_item_id bigint)
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT project.org_id
      FROM tasks_task item
      LEFT JOIN tasks_project project ON project.id = item.project_id
     WHERE item.id = p_item_id
$$;

CREATE OR REPLACE FUNCTION tasks_guard_item_link_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_org bigint;
    target_org bigint;
BEGIN
    source_org := tasks_item_organization_id(NEW.source_item_id);
    target_org := tasks_item_organization_id(NEW.target_item_id);
    IF source_org IS DISTINCT FROM target_org THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'cross_organization_item_link_denied',
            DETAIL = format(
                'source_item_id=%s org=%s target_item_id=%s org=%s',
                NEW.source_item_id, source_org,
                NEW.target_item_id, target_org
            );
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_guard_item_hierarchy_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    new_org bigint;
    old_org bigint;
    related_org bigint;
BEGIN
    IF NEW.project_id IS NOT NULL THEN
        SELECT org_id INTO new_org
          FROM tasks_project
         WHERE id = NEW.project_id;
    END IF;

    IF NEW.parent_item_id IS NOT NULL THEN
        related_org := tasks_item_organization_id(NEW.parent_item_id);
        IF new_org IS DISTINCT FROM related_org THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'cross_organization_parent_item_denied';
        END IF;
    END IF;

    IF NEW.parent_alert_id IS NOT NULL THEN
        related_org := tasks_item_organization_id(NEW.parent_alert_id);
        IF new_org IS DISTINCT FROM related_org THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'cross_organization_parent_alert_denied';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        IF OLD.project_id IS NOT NULL THEN
            SELECT org_id INTO old_org
              FROM tasks_project
             WHERE id = OLD.project_id;
        END IF;
        IF old_org IS DISTINCT FROM new_org THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'item_organization_is_immutable';
        END IF;

        IF EXISTS (
            SELECT 1
              FROM tasks_item_link link
             WHERE (link.source_item_id = NEW.id
                    AND tasks_item_organization_id(link.target_item_id)
                        IS DISTINCT FROM new_org)
                OR (link.target_item_id = NEW.id
                    AND tasks_item_organization_id(link.source_item_id)
                        IS DISTINCT FROM new_org)
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'item_project_move_would_cross_organization_link';
        END IF;

        IF EXISTS (
            SELECT 1
              FROM tasks_task child
             WHERE (
                    child.parent_item_id = NEW.id
                    OR child.parent_alert_id = NEW.id
                   )
               AND tasks_item_organization_id(child.id)
                   IS DISTINCT FROM new_org
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'item_project_move_would_cross_organization_hierarchy';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_canonical_team_key(p_team text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
        WHEN 'personal-accounting' THEN 'accounting'
        WHEN 'digital-accounting' THEN 'accounting'
        WHEN 'digital-quant' THEN 'quant'
        WHEN 'securities-engineering' THEN 'engineering'
        WHEN 'trading-engineering' THEN 'engineering'
        WHEN 'admin_ops' THEN 'admin-ops'
        WHEN 'ceo-decisions' THEN 'human-ops'
        WHEN 'leads' THEN 'human-ops'
        WHEN 'triage' THEN 'human-ops'
        WHEN 'security-triage' THEN 'security'
        WHEN 'analytics' THEN 'research-analytics'
        WHEN 'research' THEN 'research-analytics'
        WHEN 'governance' THEN 'admin-ops'
        WHEN 'program-management' THEN 'admin-ops'
        ELSE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
    END
$$;

CREATE OR REPLACE FUNCTION tasks_canonical_assignment_email(p_email text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN lower(trim(COALESCE(p_email, ''))) LIKE '%@agents.shizuha.io'
        THEN regexp_replace(
            lower(trim(COALESCE(p_email, ''))),
            '@agents\.shizuha\.io$',
            '@shizuha.com'
        )
        ELSE lower(trim(COALESCE(p_email, '')))
    END
$$;

CREATE OR REPLACE FUNCTION tasks_guard_item_assignment_org()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    new_org bigint;
    group_key text;
    email_key text;
    expected_proof text;
    supplied_proof text;
    roster_member boolean;
    routing_team_exists boolean;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.project_id IS NOT DISTINCT FROM NEW.project_id
       AND OLD.assignee_id IS NOT DISTINCT FROM NEW.assignee_id
       AND tasks_canonical_assignment_email(OLD.assignee_email)
           IS NOT DISTINCT FROM
           tasks_canonical_assignment_email(NEW.assignee_email)
       AND tasks_canonical_team_key(OLD.assignment_group)
           IS NOT DISTINCT FROM
           tasks_canonical_team_key(NEW.assignment_group) THEN
        RETURN NEW;
    END IF;
    IF NEW.assignee_id IS NULL
       AND trim(COALESCE(NEW.assignee_email, '')) = '' THEN
        RETURN NEW;
    END IF;
    IF NEW.project_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT org_id INTO new_org
      FROM tasks_project
     WHERE id = NEW.project_id;
    email_key := tasks_canonical_assignment_email(NEW.assignee_email);
    IF email_key = '' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'assigned_task_requires_canonical_email';
    END IF;
    group_key := tasks_canonical_team_key(NEW.assignment_group);

    -- ADR-0041 RoutingTeam is an exact (organization, Team) authority. Its
    -- earlier BEFORE trigger validates and consumes the one-use assignment
    -- receipt for this same tuple, so TeamRoster must not be required as a
    -- second competing membership source.
    SELECT EXISTS (
        SELECT 1
          FROM tasks_routing_team
         WHERE organization_id = new_org
           AND key = group_key
    ) INTO routing_team_exists;
    IF routing_team_exists THEN
        PERFORM set_config('pulse.assignment_org_proof', '', TRUE);
        RETURN NEW;
    END IF;

    roster_member := FALSE;
    IF group_key <> '' THEN
        SELECT EXISTS (
            SELECT 1
              FROM tasks_team_roster roster
              LEFT JOIN LATERAL jsonb_array_elements(
                    COALESCE(roster.tiers, '[]'::jsonb)
              ) tier ON TRUE
              LEFT JOIN LATERAL jsonb_array_elements_text(
                    CASE
                        WHEN jsonb_typeof(tier.value) = 'array'
                        THEN tier.value
                        ELSE '[]'::jsonb
                    END
              ) member ON TRUE
             WHERE roster.organization_id = new_org
               AND roster.team_name = group_key
               AND roster.is_active
               AND tasks_canonical_assignment_email(member.value) = email_key
        ) OR EXISTS (
            SELECT 1
              FROM tasks_team_roster roster
              CROSS JOIN LATERAL jsonb_array_elements_text(
                    COALESCE(roster.junior_emails, '[]'::jsonb)
              ) member
             WHERE roster.organization_id = new_org
               AND roster.team_name = group_key
               AND roster.is_active
               AND tasks_canonical_assignment_email(member.value) = email_key
        )
          INTO roster_member;
    END IF;
    IF roster_member THEN
        PERFORM set_config('pulse.assignment_org_proof', '', TRUE);
        RETURN NEW;
    END IF;

    expected_proof := concat_ws(
        '|',
        COALESCE(new_org, 0),
        COALESCE(NEW.project_id, 0),
        COALESCE(NEW.assignee_id, 0),
        email_key,
        group_key
    );
    supplied_proof := current_setting('pulse.assignment_org_proof', TRUE);
    IF supplied_proof IS DISTINCT FROM expected_proof THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'cross_organization_assignment_denied',
            DETAIL = format(
                'project_id=%s org=%s team=%s assignee=%s',
                NEW.project_id, new_org, group_key, email_key
            );
    END IF;
    PERFORM set_config('pulse.assignment_org_proof', '', TRUE);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_guard_project_org_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.org_id IS DISTINCT FROM NEW.org_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'project_organization_is_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tasks_item_link_org_guard ON tasks_item_link;
CREATE TRIGGER tasks_item_link_org_guard
BEFORE INSERT OR UPDATE OF source_item_id, target_item_id
ON tasks_item_link
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_link_org();

DROP TRIGGER IF EXISTS tasks_item_hierarchy_org_guard ON tasks_task;
CREATE TRIGGER tasks_item_hierarchy_org_guard
BEFORE INSERT OR UPDATE OF project_id, parent_item_id, parent_alert_id
ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_hierarchy_org();

DROP TRIGGER IF EXISTS tasks_item_assignment_org_guard ON tasks_task;
CREATE TRIGGER tasks_item_assignment_org_guard
BEFORE INSERT OR UPDATE OF assignee_id, assignee_email, assignment_group, project_id
ON tasks_task
FOR EACH ROW EXECUTE FUNCTION tasks_guard_item_assignment_org();

DROP TRIGGER IF EXISTS tasks_project_org_immutable_guard ON tasks_project;
CREATE TRIGGER tasks_project_org_immutable_guard
BEFORE UPDATE OF org_id
ON tasks_project
FOR EACH ROW EXECUTE FUNCTION tasks_guard_project_org_immutable();
"""


DROP_CONSTRAINTS_SQL = r"""
DROP TRIGGER IF EXISTS tasks_project_org_immutable_guard ON tasks_project;
DROP TRIGGER IF EXISTS tasks_item_assignment_org_guard ON tasks_task;
DROP TRIGGER IF EXISTS tasks_item_hierarchy_org_guard ON tasks_task;
DROP TRIGGER IF EXISTS tasks_item_link_org_guard ON tasks_item_link;
DROP FUNCTION IF EXISTS tasks_guard_project_org_immutable();
DROP FUNCTION IF EXISTS tasks_guard_item_assignment_org();
DROP FUNCTION IF EXISTS tasks_canonical_assignment_email(text);
DROP FUNCTION IF EXISTS tasks_canonical_team_key(text);
DROP FUNCTION IF EXISTS tasks_guard_item_hierarchy_org();
DROP FUNCTION IF EXISTS tasks_guard_item_link_org();
DROP FUNCTION IF EXISTS tasks_item_organization_id(bigint);
"""


def install_database_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_CONSTRAINTS_SQL)


def remove_database_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_CONSTRAINTS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0104_canonicalize_org_business_rosters'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationIsolationArchive',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('kind', models.CharField(db_index=True, max_length=40)),
                (
                    'object_key',
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default='',
                        max_length=80,
                    ),
                ),
                ('payload', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('restored_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'tasks_org_isolation_archive',
                'ordering': ['id'],
            },
        ),
        migrations.AddField(
            model_name='teamroster',
            name='organization_id',
            field=models.PositiveIntegerField(
                blank=True,
                db_index=True,
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_team_roster_organization_ids,
            clear_team_roster_organization_ids,
        ),
        migrations.AddConstraint(
            model_name='teamroster',
            constraint=models.UniqueConstraint(
                condition=models.Q(organization_id__isnull=False),
                fields=('organization_id', 'team_name'),
                name='uniq_team_roster_org_id_name',
            ),
        ),
        migrations.AddConstraint(
            model_name='project',
            constraint=models.UniqueConstraint(
                fields=('org_id', 'key'),
                name='uniq_project_org_key',
            ),
        ),
        migrations.RunPython(
            remove_cross_organization_relationships,
            reverse_code=restore_cross_organization_relationships,
        ),
        migrations.RunPython(
            install_database_constraints,
            remove_database_constraints,
        ),
    ]
