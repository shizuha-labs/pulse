"""Merge the duplicate Research roster into canonical Research Analytics.

Operator 2026-07-16: Research and Research Analytics are one team.  The July 9
fleet consolidation already made ``research-analytics`` canonical, but a stale
reconcile reactivated ``research`` on July 16.  This migration atomically moves
the remaining task/lead references, merges membership, and leaves an inactive
snapshot-bearing tombstone so a rollback can restore the exact pre-merge data.
"""
import json

from django.db import migrations


OLD = 'research'
NEW = 'research-analytics'
ORG = 'shizuha'
ORG_ID = 1
PROFILE_NAMES = ('research', 'analytics', 'research-analytics')
LEGACY_NAMES = ('research', 'analytics')
SNAPSHOT_PREFIX = '\n2026-07-16 merge rollback snapshot: '


def _members(*tier_sets):
    out = []
    seen = set()
    for tiers in tier_sets:
        for tier in tiers or []:
            for raw in tier or []:
                email = str(raw or '').strip().lower()
                if email and email not in seen:
                    seen.add(email)
                    out.append(email)
    return out


def _emails(*email_sets):
    out = []
    seen = set()
    for values in email_sets:
        for raw in values or []:
            email = str(raw or '').strip().lower()
            if email and email not in seen:
                seen.add(email)
                out.append(email)
    return out


def forward(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Item = apps.get_model('tasks', 'Item')
    ProjectTeamLead = apps.get_model('tasks', 'ProjectTeamLead')
    TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')

    legacy = TeamRoster.objects.filter(
        organization_slug=ORG, team_name=OLD,
    ).first()
    if not legacy or SNAPSHOT_PREFIX in (legacy.note or ''):
        return

    canonical = TeamRoster.objects.filter(
        organization_slug=ORG, team_name=NEW,
    ).first()
    analytics_roster = TeamRoster.objects.filter(
        organization_slug=ORG, team_name='analytics',
    ).first()
    canonical_existed = canonical is not None
    if canonical is None:
        canonical = TeamRoster.objects.create(
            organization_slug=ORG,
            team_name=NEW,
            tiers=[],
            junior_emails=[],
            is_active=True,
            note='',
            updated_by='migration:research-team-merge',
        )

    # TeamRoster is slug-scoped while task ownership is keyed by Admin org id.
    # Restrict the data move to Shizuha Global; another tenant may legitimately
    # use the same human-facing team label.
    task_rows = list(Item.objects.filter(
        project__org_id=ORG_ID,
        assignment_group__in=LEGACY_NAMES,
    ).values('id', 'assignment_group'))
    recurring_metadata_rows = list(Item.objects.filter(
        project__org_id=ORG_ID,
        metadata__recurring_owner_team__in=(OLD, 'analytics'),
    ).values('id', 'metadata'))
    lead_rows = list(ProjectTeamLead.objects.filter(
        org_id=ORG_ID,
        team_name__in=LEGACY_NAMES,
    ).values('id', 'team_name'))
    snapshot = {
        'canonical_existed': canonical_existed,
        'canonical_tiers': canonical.tiers or [],
        'canonical_junior_emails': canonical.junior_emails or [],
        'canonical_is_active': canonical.is_active,
        'canonical_note': canonical.note or '',
        'canonical_updated_by': canonical.updated_by or '',
        'legacy_tiers': legacy.tiers or [],
        'legacy_junior_emails': legacy.junior_emails or [],
        'legacy_is_active': legacy.is_active,
        'legacy_note': legacy.note or '',
        'legacy_updated_by': legacy.updated_by or '',
        'analytics_roster': ({
            'id': analytics_roster.id,
            'tiers': analytics_roster.tiers or [],
            'junior_emails': analytics_roster.junior_emails or [],
            'is_active': analytics_roster.is_active,
            'note': analytics_roster.note or '',
            'updated_by': analytics_roster.updated_by or '',
        } if analytics_roster else None),
        'task_rows': task_rows,
        'recurring_metadata_rows': recurring_metadata_rows,
        'lead_rows': lead_rows,
        'capability_profiles': list(
            TeamCapabilityProfile.objects.filter(team_name__in=PROFILE_NAMES)
            .values('id', 'team_name', 'capabilities', 'note', 'updated_by')
        ),
    }

    active_legacy_lead = ProjectTeamLead.objects.filter(
        org_id=ORG_ID, team_name__in=LEGACY_NAMES, is_active=True,
    ).order_by('-elected_at').values_list('lead_email', flat=True).first()
    active_analytics_tiers = (
        analytics_roster.tiers
        if analytics_roster is not None and analytics_roster.is_active
        else []
    )
    merged_members = _members(canonical.tiers, legacy.tiers, active_analytics_tiers)
    if active_legacy_lead:
        preferred = str(active_legacy_lead).strip().lower()
        merged_members = [preferred] + [e for e in merged_members if e != preferred]

    canonical.tiers = [merged_members] if merged_members else []
    canonical.junior_emails = _emails(
        canonical.junior_emails,
        legacy.junior_emails,
        analytics_roster.junior_emails
        if analytics_roster is not None and analytics_roster.is_active
        else [],
    )
    canonical.is_active = True
    canonical.note = (
        (canonical.note or '').rstrip()
        + '\n2026-07-16: absorbed legacy research roster; canonical team is '
          'research-analytics, displayed as Research.'
    ).lstrip()
    canonical.updated_by = 'migration:research-team-merge'
    canonical.save()

    Item.objects.filter(
        id__in=[row['id'] for row in task_rows],
        assignment_group__in=LEGACY_NAMES,
    ).update(
        assignment_group=NEW,
    )
    for row in recurring_metadata_rows:
        metadata = dict(row.get('metadata') or {})
        metadata['recurring_owner_team'] = NEW
        Item.objects.filter(id=row['id']).update(metadata=metadata)
    ProjectTeamLead.objects.filter(
        id__in=[row['id'] for row in lead_rows],
        team_name__in=LEGACY_NAMES,
    ).update(
        team_name=NEW,
    )

    profiles = list(TeamCapabilityProfile.objects.filter(team_name__in=PROFILE_NAMES))
    canonical_profile = next((p for p in profiles if p.team_name == NEW), None)
    if canonical_profile is None:
        canonical_profile = TeamCapabilityProfile.objects.create(
            team_name=NEW,
            capabilities=[],
            note='',
            updated_by='migration:research-team-merge',
        )
    canonical_profile.capabilities = sorted({
        str(cap).strip()
        for profile in profiles
        for cap in (profile.capabilities or [])
        if str(cap).strip()
    })
    canonical_profile.note = (
        (canonical_profile.note or '').rstrip()
        + '\n2026-07-16: merged legacy research/analytics capability profiles.'
    ).lstrip()
    canonical_profile.updated_by = 'migration:research-team-merge'
    canonical_profile.save()
    TeamCapabilityProfile.objects.filter(
        team_name__in=(OLD, 'analytics'),
    ).delete()

    legacy.is_active = False
    legacy.note = (legacy.note or '').rstrip() + SNAPSHOT_PREFIX + json.dumps(
        snapshot, separators=(',', ':'), sort_keys=True,
    )
    legacy.updated_by = 'migration:research-team-merge'
    legacy.save()
    if analytics_roster:
        analytics_roster.is_active = False
        analytics_roster.note = (
            (analytics_roster.note or '').rstrip()
            + '\n2026-07-16: merged into research-analytics; rollback snapshot '
              'is stored on the legacy research roster.'
        ).lstrip()
        analytics_roster.updated_by = 'migration:research-team-merge'
        analytics_roster.save()


def backward(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    Item = apps.get_model('tasks', 'Item')
    ProjectTeamLead = apps.get_model('tasks', 'ProjectTeamLead')
    TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')

    legacy = TeamRoster.objects.filter(
        organization_slug=ORG, team_name=OLD,
    ).first()
    if not legacy or SNAPSHOT_PREFIX not in (legacy.note or ''):
        return
    raw = legacy.note.rsplit(SNAPSHOT_PREFIX, 1)[1]
    snapshot = json.loads(raw)

    for row in snapshot.get('task_rows', []):
        Item.objects.filter(id=row['id'], assignment_group=NEW).update(
            assignment_group=row['assignment_group'],
        )
    for row in snapshot.get('recurring_metadata_rows', []):
        Item.objects.filter(id=row['id']).update(metadata=row.get('metadata') or {})
    for row in snapshot.get('lead_rows', []):
        ProjectTeamLead.objects.filter(id=row['id'], team_name=NEW).update(
            team_name=row['team_name'],
        )

    TeamCapabilityProfile.objects.filter(team_name__in=PROFILE_NAMES).delete()
    for row in snapshot.get('capability_profiles', []):
        TeamCapabilityProfile.objects.create(**row)

    canonical = TeamRoster.objects.filter(
        organization_slug=ORG, team_name=NEW,
    ).first()
    if canonical:
        if snapshot.get('canonical_existed'):
            canonical.tiers = snapshot.get('canonical_tiers', [])
            canonical.junior_emails = snapshot.get('canonical_junior_emails', [])
            canonical.is_active = snapshot.get('canonical_is_active', True)
            canonical.note = snapshot.get('canonical_note', '')
            canonical.updated_by = snapshot.get('canonical_updated_by', '')
            canonical.save()
        else:
            canonical.delete()

    legacy.tiers = snapshot.get('legacy_tiers', [])
    legacy.junior_emails = snapshot.get('legacy_junior_emails', [])
    legacy.is_active = snapshot.get('legacy_is_active', True)
    legacy.note = snapshot.get('legacy_note', '')
    legacy.updated_by = snapshot.get('legacy_updated_by', '')
    legacy.save()

    analytics_snapshot = snapshot.get('analytics_roster')
    analytics_roster = TeamRoster.objects.filter(
        organization_slug=ORG, team_name='analytics',
    ).first()
    if analytics_snapshot:
        if analytics_roster is None:
            analytics_roster = TeamRoster(
                id=analytics_snapshot['id'],
                organization_slug=ORG,
                team_name='analytics',
            )
        analytics_roster.tiers = analytics_snapshot.get('tiers', [])
        analytics_roster.junior_emails = analytics_snapshot.get('junior_emails', [])
        analytics_roster.is_active = analytics_snapshot.get('is_active', False)
        analytics_roster.note = analytics_snapshot.get('note', '')
        analytics_roster.updated_by = analytics_snapshot.get('updated_by', '')
        analytics_roster.save()
    elif analytics_roster:
        analytics_roster.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0102_merge_plat4609_plat4215'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
