"""Typed external-wait authority for ADR-0056 and ADR-0064 (PLS-733)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from ..models import (
    EscalationRoutingDisposition,
    ExternalDependencyBinding,
    ExternalDependencyReleaseEvent,
    ExternalResponseHoldAudit,
    ExternalResponseHoldWake,
    ExternalWaitAuthority,
    Item,
    ItemLink,
)
from .permissions import PermissionService
from .workflow_service import WorkflowService, WorkflowValidationError


GENERIC_ACCEPT_ERROR = 'external_wait_not_accepted'
HUMAN_CLASSES = frozenset({
    'human_held_source_evidence',
    'operator_decision',
    'operator_held_credential_access',
    'human_physical_action',
})
EXTERNAL_RESPONSE_EVIDENCE_KINDS = frozenset({
    'external_reply_v1',
    'policy_ref_v1',
    'batch_status_v1',
})
EXTERNAL_RESPONSE_DISPLAY_KINDS = frozenset({
    'review_response',
    'policy_window',
    'batch_window',
})
_CORRELATION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')


def _actor(user):
    raw_id = getattr(user, 'id', None)
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError):
        user_id = None
    email = str(
        getattr(user, 'email', '') or getattr(user, 'username', '') or ''
    ).strip().lower()
    return user_id, email


def _item_org_id(item: Item) -> int:
    if not item.project_id or not item.project:
        raise ValidationError({'item': 'external_wait_requires_organization'})
    return int(item.project.org_id)


def _authorize_item(user, item: Item) -> None:
    user_id, email = _actor(user)
    org_id = _item_org_id(item)
    if not PermissionService.identity_belongs_to_organization(
        user_id, org_id, user_email=email,
    ):
        raise PermissionDenied(GENERIC_ACCEPT_ERROR)
    if not PermissionService.can_access_item(
        user_id,
        item,
        permission='edit',
        is_staff=False,
        user_email=email,
    ):
        raise PermissionDenied(GENERIC_ACCEPT_ERROR)


def _email_matches(left: str, right: str) -> bool:
    return bool(
        set(WorkflowService._agent_email_aliases(left))
        & set(WorkflowService._agent_email_aliases(right))
    )


def _authorize_external_response_actor(user, item: Item, *, action: str) -> None:
    """Enforce branch-3's closed human/agent principal classes.

    Ordinary edit permission is intentionally insufficient. Admission/wake/
    revoke require the current owner or a current member of the carrier's
    assignment group. Scheduler checkpoint wakes never call this path.
    """
    _authorize_item(user, item)
    _user_id, email = _actor(user)
    owner = _email_matches(email, item.assignee_email or '') or (
        not (item.assignee_email or '').strip()
        and bool(item.assignee_id)
        and item.assignee_id == _actor(user)[0]
    )
    lane = bool(
        item.assignment_group
        and WorkflowService._is_team_member(
            email, item.assignment_group, item=item,
        )
    )
    configured_services = {
        value.strip().lower()
        for value in str(
            getattr(settings, 'PULSE_EXTERNAL_RESPONSE_EVIDENCE_PRODUCERS', '')
        ).split(',')
        if value.strip()
    }
    named_evidence_service = (
        action in {'admit', 'wake'} and email in configured_services
    )
    if not (owner or lane or named_evidence_service):
        raise PermissionDenied(f'external_response_hold_{action}_not_authorized')


def _validate_correlation_key(value: str) -> str:
    key = str(value or '').strip()
    if not _CORRELATION_RE.fullmatch(key):
        raise ValidationError({'correlation_key': 'invalid_correlation_key'})
    return key


def _validate_display_reference(value) -> dict:
    if not isinstance(value, dict) or set(value) != {
        'kind', 'reference_id', 'redacted_label',
    }:
        raise ValidationError({'display_reference': 'invalid_display_reference'})
    kind = str(value.get('kind') or '').strip()
    reference_id = str(value.get('reference_id') or '').strip()
    label = str(value.get('redacted_label') or '').strip()
    if (
        kind not in EXTERNAL_RESPONSE_DISPLAY_KINDS
        or not reference_id
        or len(reference_id) > 80
        or '@' in reference_id
        or not label
        or len(label) > 120
        or '@' in label
    ):
        raise ValidationError({'display_reference': 'invalid_display_reference'})
    return {
        'kind': kind,
        'reference_id': reference_id,
        'redacted_label': label,
    }


def _canonical_source_evidence(item: Item, user, value) -> tuple[dict, str]:
    """Resolve a local authoritative object and bind its immutable snapshot.

    The caller supplies only a closed kind plus a local Item identifier. Pulse
    resolves that row inside the carrier org, verifies the caller can read it,
    then stores a digest of the canonical snapshot, never a mutable comment,
    label, URL, or caller-supplied digest.
    """
    if not isinstance(value, dict) or set(value) != {'kind', 'item_id'}:
        raise ValidationError({'source_evidence': 'invalid_source_evidence'})
    kind = str(value.get('kind') or '').strip()
    if kind not in EXTERNAL_RESPONSE_EVIDENCE_KINDS:
        raise ValidationError({'source_evidence': 'invalid_source_evidence_kind'})
    try:
        source = Item.objects.select_related('project').get(pk=value.get('item_id'))
    except (Item.DoesNotExist, TypeError, ValueError):
        raise ValidationError({'source_evidence': 'source_evidence_not_found'})
    if _item_org_id(source) != _item_org_id(item):
        raise PermissionDenied('external_response_hold_wrong_org_evidence')
    _authorize_item(user, source)
    snapshot = {
        'schema': kind,
        'item_id': source.pk,
        'item_key': source.item_key or '',
        'state_generation': int(source.state_generation or 0),
        'status': source.status,
        'updated_at': source.updated_at.isoformat(),
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    return snapshot, digest


def _external_response_state(item: Item) -> dict:
    return {
        'status': item.status,
        'workflow_status': item.workflow_status or '',
        'assignee_email': item.assignee_email or '',
        'assignment_group': item.assignment_group or '',
    }


def _audit_external_response(
    authority: ExternalWaitAuthority,
    *,
    event_type: str,
    correlation_key: str,
    actor=None,
    service_identity: str = '',
    evidence_digest: str = '',
    before=None,
    after=None,
    reason_code: str,
) -> ExternalResponseHoldAudit:
    actor_id, actor_email = _actor(actor)
    audit, _created = ExternalResponseHoldAudit.objects.get_or_create(
        authority=authority,
        event_type=event_type,
        correlation_key=correlation_key,
        defaults={
            'organization_id': authority.organization_id,
            'item_id': authority.item_id,
            'wait_generation': authority.wait_generation,
            'actor_id': actor_id,
            'actor_email': actor_email,
            'service_identity': str(service_identity or '')[:128],
            'evidence_digest': evidence_digest or authority.source_evidence_digest,
            'before_state': dict(before or {}),
            'after_state': dict(after or {}),
            'reason_code': str(reason_code or '')[:80],
        },
    )
    return audit


def _pre_state(item: Item) -> dict:
    return {
        'status': item.status,
        'workflow_status': item.workflow_status or '',
        'assignee_id': item.assignee_id,
        'assignee_email': item.assignee_email or '',
        'assignment_group': item.assignment_group or '',
    }


def _next_wait_generation(item: Item) -> int:
    authority_max = ExternalWaitAuthority.objects.filter(item=item).aggregate(
        value=Max('wait_generation')
    )['value'] or 0
    binding_max = ExternalDependencyBinding.objects.filter(
        consumer_item=item,
    ).aggregate(value=Max('wait_generation'))['value'] or 0
    return max(int(item.external_wait_generation or 0), authority_max, binding_max) + 1


def _create_authority(
    item: Item,
    *,
    generation: int,
    branch: str,
    pre: dict,
    user=None,
    evidence=None,
) -> ExternalWaitAuthority:
    user_id, email = _actor(user)
    return ExternalWaitAuthority.objects.create(
        item=item,
        organization_id=_item_org_id(item),
        wait_generation=generation,
        branch=branch,
        state=ExternalWaitAuthority.STATE_ENTER_ARMED,
        pre_status=pre['status'],
        pre_workflow_status=pre.get('workflow_status') or '',
        pre_assignee_id=pre.get('assignee_id'),
        pre_assignee_email=pre.get('assignee_email') or '',
        pre_assignment_group=pre.get('assignment_group') or '',
        evidence=dict(evidence or {}),
        requested_by_id=user_id,
        requested_by_email=email,
        revalidate_after=timezone.now() + timedelta(hours=12),
    )


def _enter_wait(item: Item, authority: ExternalWaitAuthority) -> Item:
    """Keep the item workable. ``waiting_external`` is retired.

    Operator 2026-09-26: a hold that is not ``blocked`` and has no parent
    blocker became an unworkable chain root. Admission now leaves the item
    on the status and owner the caller already set (a todo/open/in_progress
    root, or a blocked parent that already has its own blocker). A seated
    root releases this authority immediately. A group-owned root with no
    seat keeps ``STATE_WAITING`` only as the 12h owner-retry clock; the
    status stays runnable, and seating the owner releases the clock.
    """
    if authority.state != ExternalWaitAuthority.STATE_ENTER_ARMED:
        raise ValidationError({'status': 'external_wait_entry_not_armed'})
    if WorkflowService._status_is_terminal(item.status) or item.resolution_id:
        raise ValidationError({'status': 'external_wait_terminal_item_refused'})
    evidence = dict(authority.evidence or {})
    evidence['retired_reason'] = 'waiting_external_retired'
    authority.evidence = evidence
    seated = bool(item.assignee_id and (item.assignee_email or '').strip())
    # A human escalation child is the chain root. Once it has a seat, or it
    # is itself blocked, there is nothing to park. Other branches keep a
    # WAITING authority as their receipt, but the item status stays workable.
    if authority.branch == ExternalWaitAuthority.BRANCH_HUMAN and (
        seated or (item.status or '').strip().lower() == 'blocked'
    ):
        authority.state = ExternalWaitAuthority.STATE_RELEASED
        authority.released_at = timezone.now()
        authority.save(update_fields=['state', 'released_at', 'evidence', 'updated_at'])
        return item
    item.external_wait_generation = authority.wait_generation
    item.save(update_fields=['external_wait_generation', 'updated_at'])
    authority.state = ExternalWaitAuthority.STATE_WAITING
    authority.entered_at = timezone.now()
    authority.save(update_fields=[
        'state', 'entered_at', 'evidence', 'updated_at',
    ])
    return item


def _safe_restore_target(item: Item, authority: ExternalWaitAuthority) -> tuple[str, int | None, str, str]:
    """Return a currently valid status/owner tuple, never stale authority."""
    valid = WorkflowService._workflow_status_slugs(item)
    target = authority.pre_status
    if (
        (valid and target not in valid)
        or WorkflowService._status_is_terminal(target)
    ):
        target = WorkflowService._blocker_activation_status(item)
        if not target:
            raise WorkflowValidationError(
                'No current enterable workflow status is available.',
                code='EXTERNAL_WAIT_RESTORE_STATUS_INVALID',
            )

    owner_id = authority.pre_assignee_id
    owner_email = authority.pre_assignee_email or ''
    group = WorkflowService._canonical_team_name(
        authority.pre_assignment_group or item.assignment_group or ''
    )
    eligible = False
    if owner_email:
        if group:
            eligible = WorkflowService._is_team_member(
                owner_email, group, item=item,
            )
        else:
            mismatch, _owner_org, _task_org = WorkflowService.assignment_org_mismatch(
                item, owner_email, user_id=owner_id,
            )
            eligible = not mismatch
    if not eligible:
        owner_id, owner_email = WorkflowService._resolve_team_assignee(
            group, enforce_cap=False, item=item,
        ) if group else (None, None)
        if not owner_email:
            # Fail safe and visible: retain durable team ownership rather than
            # resurrecting an ineligible principal.
            owner_id, owner_email = None, ''
    return target, owner_id, owner_email or '', group


def _restore_wait(item: Item, authority: ExternalWaitAuthority, *, cause: str) -> bool:
    target, owner_id, owner_email, group = _safe_restore_target(item, authority)
    focus = WorkflowService._higher_impact_owned_blocker(
        item,
        authority.pre_assignee_email or item.assignee_email,
        current_status_override=target,
        # External-wait reconciliation is system-driven, but it still has an
        # exact tenant boundary. Never let a same-email row in another
        # organization freeze this release or leak its key into diagnostics.
        visible_queryset=(
            Item.objects.filter(project__org_id=item.project.org_id)
            if item.project_id
            else Item.objects.filter(project__isnull=True)
        ),
    )
    if focus is not None:
        return False

    authority.state = ExternalWaitAuthority.STATE_EXIT_ARMED
    authority.save(update_fields=['state', 'updated_at'])
    item.status = target
    item.workflow_status = (
        authority.pre_workflow_status
        if target == authority.pre_status and authority.pre_workflow_status
        else WorkflowService._workflow_status_display_for_item(item, target)
    )
    item.assignee_id = owner_id
    item.assignee_email = owner_email or None
    item.assignment_group = group or None
    item._external_wait_authorized = True
    item._activity_actor = (0, 'workflow@pulse')
    item._state_mutation_cause = {
        'kind': cause,
        'wait_generation': str(authority.wait_generation),
        'receipt_id': str(authority.receipt_id),
    }
    item.save(update_fields=[
        'status', 'workflow_status', 'assignee_id', 'assignee_email',
        'assignment_group', 'updated_at',
    ])
    authority.state = ExternalWaitAuthority.STATE_RELEASED
    authority.released_at = timezone.now()
    authority.save(update_fields=['state', 'released_at', 'updated_at'])
    return True


@transaction.atomic
def admit_human_escalation(
    blocker: Item,
    disposition: EscalationRoutingDisposition,
    *,
    user=None,
) -> ExternalWaitAuthority | None:
    """Park one validated PLAT-4215 child; anything ambiguous stays runnable."""

    blocker = Item.objects.select_for_update().get(pk=blocker.pk)
    tags = {
        part.strip() for part in str(blocker.tags or '').split(',') if part.strip()
    }
    evidence = disposition.normalized_evidence or {}
    dependency = evidence.get('dependency') if isinstance(evidence, dict) else None
    metadata = blocker.metadata if isinstance(blocker.metadata, dict) else {}
    group_owned = (
        bool(metadata.get('escalation_group_owned_wait'))
        and not blocker.assignee_id
        and not (blocker.assignee_email or '').strip()
        and int(metadata.get('escalation_group_owned_org_id') or 0)
        == _item_org_id(blocker)
        and WorkflowService._team_registered_for_item(
            blocker.assignment_group,
            blocker,
        )
    )
    if (
        blocker.mode != 'task'
        or 'PLAT-4215' not in tags
        or not isinstance(dependency, dict)
        or dependency.get('evidence_class') not in HUMAN_CLASSES
        or not str(dependency.get('holder_ref') or '').strip()
        or not str(dependency.get('requested_subject') or '').strip()
        or not str(dependency.get('acceptance_check') or '').strip()
        or (not blocker.assignee_email and not group_owned)
    ):
        return None
    delivered = ItemLink.objects.filter(
        source_item=blocker,
        target_item=disposition.item,
        link_type='blocks',
    ).exists()
    if not delivered:
        return None
    existing = ExternalWaitAuthority.objects.select_for_update().filter(
        item=blocker,
        state__in={
            ExternalWaitAuthority.STATE_ENTER_ARMED,
            ExternalWaitAuthority.STATE_WAITING,
            ExternalWaitAuthority.STATE_EXIT_ARMED,
        },
    ).first()
    if existing:
        return existing
    generation = _next_wait_generation(blocker)
    authority = _create_authority(
        blocker,
        generation=generation,
        branch=ExternalWaitAuthority.BRANCH_HUMAN,
        pre=_pre_state(blocker),
        user=user,
        evidence={
            'schema': evidence.get('schema'),
            'holder_ref': dependency['holder_ref'],
            'requested_subject': dependency['requested_subject'],
            'acceptance_check': dependency['acceptance_check'],
            'source_refs': dependency.get('source_refs') or [],
            'ask_ref': {'kind': 'pulse_blocker_child', 'id': blocker.item_key},
            'disposition_id': str(disposition.id),
            'group_owned_at_admission': group_owned,
        },
    )
    _enter_wait(blocker, authority)
    return authority


@transaction.atomic
def admit_unresolved_operator_destination(
    item: Item,
    *,
    evidence: dict | None,
    user=None,
) -> ExternalWaitAuthority | None:
    """Park the carrier itself when a valid human ask cannot be seated.

    PLS-278 / ``org_local_operator_destination_unresolved`` used to keep the
    item ``in_progress`` on the escalating agent (Sato/BKSK-6). ADR-0056
    requires that a proven human-held dependency leave the ready set even
    when the exact-org operator team has no roster. Technical/schema holds
    stay runnable so the agent can repair them.
    """
    item = Item.objects.select_for_update().get(pk=item.pk)
    dependency = evidence.get('dependency') if isinstance(evidence, dict) else None
    if (
        item.mode != 'task'
        or not isinstance(dependency, dict)
        or dependency.get('evidence_class') not in HUMAN_CLASSES
        or not str(dependency.get('holder_ref') or '').strip()
        or not str(dependency.get('requested_subject') or '').strip()
        or not str(dependency.get('acceptance_check') or '').strip()
    ):
        return None
    if WorkflowService._status_is_terminal(item.status) or item.resolution_id:
        return None
    existing = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        state__in={
            ExternalWaitAuthority.STATE_ENTER_ARMED,
            ExternalWaitAuthority.STATE_WAITING,
            ExternalWaitAuthority.STATE_EXIT_ARMED,
        },
    ).first()
    if existing:
        return existing
    generation = _next_wait_generation(item)
    authority = _create_authority(
        item,
        generation=generation,
        branch=ExternalWaitAuthority.BRANCH_HUMAN,
        pre=_pre_state(item),
        user=user,
        evidence={
            'schema': (evidence or {}).get('schema'),
            'holder_ref': dependency['holder_ref'],
            'requested_subject': dependency['requested_subject'],
            'acceptance_check': dependency['acceptance_check'],
            'source_refs': dependency.get('source_refs') or [],
            'ask_ref': {'kind': 'unresolved_operator_destination', 'id': item.item_key},
            'reason_code': 'org_local_operator_destination_unresolved',
        },
    )
    _enter_wait(item, authority)
    return authority


def reconcile_group_owned_human_waits(
    *,
    organization_id: int | None = None,
    team_name: str | None = None,
    only_agent_email: str | None = None,
    item_id: int | None = None,
    limit: int = 100,
) -> dict:
    """Assign parked exact-org escalation children after a roster/live event.

    Group-owned escalation roots stay on a runnable status. Roster writes
    and the 12h retry call this reconciler to seat them. A legacy
    ``waiting_external`` row, if one still exists, is seated the same way
    and remains parked until a typed release. Seating a runnable root
    releases its owner-retry authority.
    """

    qs = Item.objects.filter(
        mode='task',
        status__in=('todo', 'open', 'pending', 'in_progress', 'waiting_external'),
        assignee_id__isnull=True,
        metadata__escalation_group_owned_wait=True,
    ).filter(Q(assignee_email__isnull=True) | Q(assignee_email=''))
    if item_id is not None:
        qs = qs.filter(pk=item_id)
    if organization_id is not None:
        qs = qs.filter(project__org_id=int(organization_id))
    canonical_team = WorkflowService._canonical_team_name(team_name or '')
    if canonical_team:
        qs = qs.filter(assignment_group=canonical_team)
    scoped_email = str(only_agent_email or '').strip().lower()

    assigned = considered = 0
    for item_id in qs.order_by('created_at', 'pk').values_list('pk', flat=True)[:limit]:
        with transaction.atomic():
            # Project is nullable; locking across select_related()'s outer join
            # is rejected by PostgreSQL. Lock the Item row only.
            item = Item.objects.select_for_update(of=('self',)).get(pk=item_id)
            if (
                item.status not in (
                    'todo', 'open', 'pending', 'in_progress', 'waiting_external',
                )
                or item.assignee_id is not None
                or (item.assignee_email or '').strip()
            ):
                continue
            authority = ExternalWaitAuthority.objects.select_for_update().filter(
                item=item,
                wait_generation=item.external_wait_generation,
                branch=ExternalWaitAuthority.BRANCH_HUMAN,
                state=ExternalWaitAuthority.STATE_WAITING,
            ).first()
            if item.status == 'waiting_external' and authority is None:
                continue
            team = WorkflowService._canonical_team_name(item.assignment_group)
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            if (
                not team
                or int(metadata.get('escalation_group_owned_org_id') or 0)
                != _item_org_id(item)
                or not WorkflowService._team_registered_for_item(team, item)
            ):
                continue
            if scoped_email and not WorkflowService._is_team_member(
                scoped_email,
                team,
                item=item,
            ):
                continue
            considered += 1
            uid, email = WorkflowService._resolve_team_assignee(
                team,
                enforce_cap=False,
                project_key=WorkflowService._item_project_key(item),
                item=item,
            )
            if not uid or not (email or '').strip():
                continue
            # ``scoped_email`` proves that this exact Team had a recovery event;
            # it does not override the canonical resolver's election.  Another
            # currently eligible Team member may be the lower-load winner.  The
            # exact-org membership check above is the authorization boundary, so
            # accept that winner rather than waiting for a second member-specific
            # recovery event that may never arrive.
            fields = []
            WorkflowService._apply_resolved_assignee(
                item,
                uid,
                email,
                fields,
            )
            # PLS-860 item 12: both returns of _apply_resolved_assignee are
            # 2-tuples (always truthy). Guard on assignee_id only.
            if not item.assignee_id or not (item.assignee_email or '').strip():
                continue
            metadata = dict(item.metadata or {})
            metadata.pop('escalation_group_owned_wait', None)
            metadata.pop('escalation_group_owned_cause', None)
            item.metadata = metadata
            fields.append('metadata')
            item._activity_actor = (0, 'system@pulse')
            item.save(update_fields=[*dict.fromkeys(fields), 'updated_at'])
            if item.status != 'waiting_external' and authority is not None:
                authority.state = ExternalWaitAuthority.STATE_RELEASED
                authority.released_at = timezone.now()
                authority.save(update_fields=['state', 'released_at', 'updated_at'])
            assigned += 1
    return {
        'assigned': assigned,
        'considered': considered,
        'organization_id': organization_id,
        'team_name': canonical_team or None,
        'only_agent_email': scoped_email or None,
    }


@transaction.atomic
def admit_migration_capsule_wait(
    item: Item,
    *,
    capsule_digest: str,
    reason_code: str,
    local_recheck_criterion: str,
    reviewer_email: str = '',
    user=None,
    legacy_key_redacted: str = '',
) -> ExternalWaitAuthority:
    """PLS-842 / BKSD-14 §7.2: park an externally-unverifiable migration capsule.

    Enters ``waiting_external`` via typed ``migration_capsule_v1`` authority.
    Does not release dependents. Generic completion remains blocked by the
    existing waiting_external admission guards. Reassignment does not clear
    this authority (authority is bound to the item generation, not assignee).
    """
    item = Item.objects.select_for_update().get(pk=item.pk)
    digest = str(capsule_digest or '').strip()
    reason = str(reason_code or '').strip()
    recheck = str(local_recheck_criterion or '').strip()
    if not digest or len(digest) < 16:
        raise ValidationError({'capsule_digest': 'capsule_digest_required'})
    if not reason:
        raise ValidationError({'reason_code': 'reason_code_required'})
    if not recheck:
        raise ValidationError({
            'local_recheck_criterion': 'local_recheck_criterion_required',
        })
    if WorkflowService._status_is_terminal(item.status) or item.resolution_id:
        raise ValidationError({'item': 'external_wait_terminal_item_refused'})

    existing = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        state__in={
            ExternalWaitAuthority.STATE_ENTER_ARMED,
            ExternalWaitAuthority.STATE_WAITING,
            ExternalWaitAuthority.STATE_EXIT_ARMED,
        },
    ).first()
    if existing:
        if existing.branch != ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE:
            raise ValidationError({'item': 'external_wait_branch_conflict'})
        return existing
    retired = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        branch=ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE,
        state=ExternalWaitAuthority.STATE_RELEASED,
        evidence__retired_reason='waiting_external_retired',
    ).order_by('-id').first()
    if retired:
        return retired

    generation = _next_wait_generation(item)
    authority = _create_authority(
        item,
        generation=generation,
        branch=ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE,
        pre=_pre_state(item),
        user=user,
        evidence={
            'schema': 'pls842.migration_capsule_v1',
            'capsule_digest': digest,
            'reason_code': reason[:120],
            'local_recheck_criterion': recheck[:500],
            # Redacted legacy key only — never a foreign item id/org tuple.
            'legacy_key_redacted': str(legacy_key_redacted or '')[:80],
            'reviewer_email': str(reviewer_email or '')[:254],
        },
    )
    _enter_wait(item, authority)
    return authority


@transaction.atomic
def prepare_cross_org_wait(
    item: Item,
    *,
    user,
    release_policy: str,
    local_display_reference: str = '',
    _skip_authorization: bool = False,
) -> dict:
    if not getattr(settings, 'PULSE_CROSS_ORG_EXTERNAL_WAIT_ENABLED', False):
        raise ValidationError({'detail': 'cross_org_external_wait_disabled'})
    item = Item.objects.select_for_update().get(pk=item.pk)
    if not _skip_authorization:
        _authorize_item(user, item)
    if release_policy not in dict(ExternalDependencyBinding.POLICY_CHOICES):
        raise ValidationError({'release_policy': 'invalid_release_policy'})
    if WorkflowService._status_is_terminal(item.status) or item.resolution_id:
        raise ValidationError({'item': 'external_wait_terminal_item_refused'})
    authority = ExternalWaitAuthority.objects.filter(
        item=item,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    if authority:
        if authority.branch != ExternalWaitAuthority.BRANCH_CROSS_ORG:
            raise ValidationError({'item': 'external_wait_branch_conflict'})
        generation = authority.wait_generation
    else:
        if item.status == 'waiting_external':
            raise ValidationError({'item': 'external_wait_authority_missing'})
        generation = _next_wait_generation(item)
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    try:
        ttl = int(os.environ.get('PULSE_EXTERNAL_WAIT_TOKEN_TTL_SECONDS', '900'))
    except (TypeError, ValueError):
        ttl = 900
    ttl = max(60, min(ttl, 3600))
    user_id, email = _actor(user)
    binding = ExternalDependencyBinding.objects.create(
        consumer_item=item,
        consumer_organization_id=_item_org_id(item),
        wait_generation=generation,
        consumer_state_generation=int(item.state_generation or 0),
        token_hash=token_hash,
        token_expires_at=timezone.now() + timedelta(seconds=ttl),
        release_policy=release_policy,
        local_display_reference=str(local_display_reference or '')[:200],
        prepared_by_id=user_id,
        prepared_by_email=email,
    )
    return {
        'handoff_token': raw_token,
        'receipt': str(binding.receipt_id),
        'expires_at': binding.token_expires_at.isoformat(),
        'release_policy': binding.release_policy,
    }


def _provider_is_escalation_child(provider: Item) -> bool:
    tags = {part.strip() for part in str(provider.tags or '').split(',') if part.strip()}
    return 'PLAT-4215' in tags or (
        provider.mode == 'task'
        and isinstance(provider.metadata, dict)
        and bool(provider.metadata.get('auto_blocker_intent'))
    )


@transaction.atomic
def accept_cross_org_wait(
    provider: Item,
    *,
    user,
    handoff_token: str,
    _skip_authorization: bool = False,
) -> dict:
    if not getattr(settings, 'PULSE_CROSS_ORG_EXTERNAL_WAIT_ENABLED', False):
        raise ValidationError({'detail': GENERIC_ACCEPT_ERROR})
    provider = Item.objects.select_for_update().get(pk=provider.pk)
    if not _skip_authorization:
        _authorize_item(user, provider)
    digest = hashlib.sha256(str(handoff_token or '').encode()).hexdigest()
    binding = ExternalDependencyBinding.objects.select_for_update().filter(
        token_hash=digest,
        state=ExternalDependencyBinding.STATE_PREPARED,
    ).first()
    if not binding or binding.token_expires_at <= timezone.now():
        raise ValidationError({'detail': GENERIC_ACCEPT_ERROR})
    consumer = Item.objects.select_for_update().get(
        pk=binding.consumer_item_id
    )
    provider_org = _item_org_id(provider)
    if (
        provider_org == binding.consumer_organization_id
        or _provider_is_escalation_child(provider)
    ):
        raise ValidationError({'detail': GENERIC_ACCEPT_ERROR})

    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=consumer,
        wait_generation=binding.wait_generation,
        branch=ExternalWaitAuthority.BRANCH_CROSS_ORG,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    needs_entry = authority is None
    if needs_entry:
        if (
            consumer.status == 'waiting_external'
            or int(consumer.state_generation or 0) != binding.consumer_state_generation
        ):
            binding.state = ExternalDependencyBinding.STATE_STALE
            binding.save(update_fields=['state', 'updated_at'])
            raise ValidationError({'detail': GENERIC_ACCEPT_ERROR})
        authority = _create_authority(
            consumer,
            generation=binding.wait_generation,
            branch=ExternalWaitAuthority.BRANCH_CROSS_ORG,
            pre=_pre_state(consumer),
            user=None,
            evidence={'schema': ExternalWaitAuthority.BRANCH_CROSS_ORG},
        )

    user_id, email = _actor(user)
    binding.authority = authority
    binding.provider_item = provider
    binding.provider_organization_id = provider_org
    binding.token_consumed_at = timezone.now()
    binding.accepted_by_id = user_id
    binding.accepted_by_email = email
    binding.state = ExternalDependencyBinding.STATE_ACTIVE
    try:
        binding.save(update_fields=[
            'authority', 'provider_item', 'provider_organization_id',
            'token_consumed_at', 'accepted_by_id', 'accepted_by_email',
            'state', 'updated_at',
        ])
    except IntegrityError as exc:
        raise ValidationError({'detail': GENERIC_ACCEPT_ERROR}) from exc
    if needs_entry:
        _enter_wait(consumer, authority)
    if WorkflowService._status_releases_blocks(provider.status):
        record_provider_release(provider, kind=ExternalDependencyReleaseEvent.KIND_TERMINAL)
    return {'receipt': str(binding.receipt_id), 'accepted': True}


@transaction.atomic
def bind_cross_org_items_privileged(
    consumer: Item,
    provider: Item,
    *,
    release_policy: str,
    local_display_reference: str,
) -> dict:
    """Idempotent exact-pair migration entry point; never exposed over HTTP."""
    existing = ExternalDependencyBinding.objects.filter(
        consumer_item=consumer,
        provider_item=provider,
        state__in={
            ExternalDependencyBinding.STATE_ACTIVE,
            ExternalDependencyBinding.STATE_RELEASED,
        },
    ).order_by('-created_at').first()
    if existing:
        return {'receipt': str(existing.receipt_id), 'accepted': True, 'replayed': True}
    prepared = prepare_cross_org_wait(
        consumer,
        user=None,
        release_policy=release_policy,
        local_display_reference=local_display_reference,
        _skip_authorization=True,
    )
    result = accept_cross_org_wait(
        provider,
        user=None,
        handoff_token=prepared['handoff_token'],
        _skip_authorization=True,
    )
    result['replayed'] = False
    return result


@transaction.atomic
def record_provider_release(provider: Item, *, kind: str, user=None) -> int:
    provider = Item.objects.select_for_update().get(pk=provider.pk)
    if user is not None:
        _authorize_item(user, provider)
    if kind == ExternalDependencyReleaseEvent.KIND_TERMINAL:
        if not WorkflowService._status_releases_blocks(provider.status):
            return 0
        bindings = ExternalDependencyBinding.objects.filter(
            provider_item=provider,
            state=ExternalDependencyBinding.STATE_ACTIVE,
        )
    elif kind == ExternalDependencyReleaseEvent.KIND_EXPLICIT:
        bindings = ExternalDependencyBinding.objects.filter(
            provider_item=provider,
            state=ExternalDependencyBinding.STATE_ACTIVE,
            release_policy=ExternalDependencyBinding.POLICY_TERMINAL_OR_READY,
        )
    else:
        raise ValidationError({'kind': 'invalid_release_kind'})
    generation = max(1, int(provider.state_generation or 0))
    created = 0
    created_ids = []
    for binding in bindings:
        event, was_created = ExternalDependencyReleaseEvent.objects.get_or_create(
            binding=binding,
            release_generation=generation,
            kind=kind,
        )
        created += int(was_created)
        if was_created:
            created_ids.append(str(event.id))
    if created_ids and not getattr(settings, 'TESTING', False):
        def _enqueue(ids=tuple(created_ids)):
            from ..tasks import deliver_external_dependency_release
            for event_id in ids:
                deliver_external_dependency_release.delay(event_id)
        transaction.on_commit(_enqueue)
    return created


@transaction.atomic
def process_release_event(event_id) -> str:
    event = ExternalDependencyReleaseEvent.objects.select_for_update().get(pk=event_id)
    if event.state != ExternalDependencyReleaseEvent.STATE_PENDING:
        return event.state
    binding = ExternalDependencyBinding.objects.select_for_update().get(pk=event.binding_id)
    authority = ExternalWaitAuthority.objects.select_for_update().get(pk=binding.authority_id)
    consumer = Item.objects.select_for_update().get(pk=authority.item_id)
    if binding.state != ExternalDependencyBinding.STATE_ACTIVE:
        event.state = ExternalDependencyReleaseEvent.STATE_STALE
        event.last_error = 'binding_not_active'
        event.processed_at = timezone.now()
        event.save(update_fields=['state', 'last_error', 'processed_at'])
        return event.state

    if binding.release_recorded_at is None:
        binding.release_recorded_at = timezone.now()
        binding.provider_release_generation = event.release_generation
        binding.save(update_fields=[
            'release_recorded_at', 'provider_release_generation', 'updated_at'
        ])
    remaining = ExternalDependencyBinding.objects.filter(
        authority=authority,
        state=ExternalDependencyBinding.STATE_ACTIVE,
        release_recorded_at__isnull=True,
    ).exists()
    if remaining:
        event.state = ExternalDependencyReleaseEvent.STATE_APPLIED
        event.processed_at = timezone.now()
        event.save(update_fields=['state', 'processed_at'])
        return event.state
    if (
        authority.state != ExternalWaitAuthority.STATE_WAITING
        or consumer.external_wait_generation != authority.wait_generation
    ):
        event.state = ExternalDependencyReleaseEvent.STATE_STALE
        event.last_error = 'consumer_generation_stale'
        event.processed_at = timezone.now()
        event.save(update_fields=['state', 'last_error', 'processed_at'])
        return event.state
    if not _restore_wait(consumer, authority, cause='external_provider_release'):
        event.attempts += 1
        event.last_error = 'focus_blocked'
        event.next_attempt_at = timezone.now() + timedelta(minutes=5)
        event.save(update_fields=['attempts', 'last_error', 'next_attempt_at'])
        return 'pending'

    ExternalDependencyBinding.objects.filter(
        authority=authority,
        state=ExternalDependencyBinding.STATE_ACTIVE,
    ).update(state=ExternalDependencyBinding.STATE_RELEASED)
    event.state = ExternalDependencyReleaseEvent.STATE_APPLIED
    event.processed_at = timezone.now()
    event.save(update_fields=['state', 'processed_at'])
    return event.state


@transaction.atomic
def release_human_wait(item: Item, *, user) -> dict:
    item = Item.objects.select_for_update().get(pk=item.pk)
    _authorize_item(user, item)
    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        wait_generation=item.external_wait_generation,
        branch=ExternalWaitAuthority.BRANCH_HUMAN,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    if not authority:
        raise ValidationError({'detail': 'human_external_wait_not_active'})
    if not _restore_wait(item, authority, cause='human_external_wait_release'):
        raise WorkflowValidationError(
            'A higher-impact owned blocker must be handled before this wait resumes.',
            code='BLOCKER_FOCUS_REQUIRED',
        )
    from .mechanical_comments import MechanicalCommentFactory, deterministic_event_id
    receipt = str(authority.receipt_id)
    MechanicalCommentFactory.create(
        item=item,
        origin_kind='WORKFLOW_LIFECYCLE',
        origin_event='external_wait.human_release',
        template_version=1,
        structured_payload={'receipt': receipt},
        origin_event_id=deterministic_event_id(
            'external_wait.human_release', item, receipt,
        ),
        causal_actor=user,
    )
    return {'receipt': receipt, 'released': True}


@transaction.atomic
def settle_human_wait(item: Item, *, user, payload: dict) -> dict:
    """Consume one fulfilled human ask, without admitting its holder to work.

    The recorded pre-wait workflow supplies the ordinary resolution transition;
    its permissions/validators still apply. Only the typed wait exit is special.
    Acceptance evidence may predate a later reminder or re-created blocker link.
    The ordinary parent-resolution guard still requires a recorded answer after
    that link, but does not require re-performing the accepted external action.
    """
    from copy import copy
    from uuid import UUID

    from ..models import Comment
    from .mechanical_comments import MechanicalCommentFactory, deterministic_event_id

    item = Item.objects.select_for_update().get(pk=item.pk)
    _authorize_item(user, item)
    if not isinstance(payload, dict) or set(payload) - {
        'receipt', 'wait_generation', 'transition_id', 'summary', 'resolutions',
        'screen_data',
    }:
        raise ValidationError({'detail': 'invalid_human_wait_settlement'})
    try:
        receipt = str(UUID(str(payload.get('receipt', ''))))
        transition_id = str(UUID(str(payload.get('transition_id', ''))))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError({'detail': 'receipt_and_transition_id_required'})
    generation = payload.get('wait_generation')
    summary = payload.get('summary')
    screen_data = payload.get('screen_data', {})
    refs = payload.get('resolutions')
    if (
        type(generation) is not int or generation <= 0
        or not isinstance(summary, str) or not summary.strip()
        or not isinstance(screen_data, dict)
        or not isinstance(refs, list) or not refs
    ):
        raise ValidationError({'detail': 'settlement_evidence_required'})
    normalized_refs = []
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {'parent_id', 'comment_id', 'sha256'}
            or type(ref['parent_id']) is not int or ref['parent_id'] <= 0
            or type(ref['comment_id']) is not int or ref['comment_id'] <= 0
            or not isinstance(ref['sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256'])
        ):
            raise ValidationError({'detail': 'invalid_resolution_reference'})
        normalized_refs.append(dict(ref))
    normalized_refs.sort(key=lambda ref: (ref['parent_id'], ref['comment_id']))
    if len({ref['parent_id'] for ref in normalized_refs}) != len(normalized_refs):
        raise ValidationError({'detail': 'one_resolution_per_parent_required'})
    evidence = {
        'summary': summary.strip(), 'resolutions': normalized_refs,
        'transition_id': transition_id, 'screen_data': screen_data,
    }
    evidence_digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item, receipt_id=receipt, wait_generation=generation,
        organization_id=_item_org_id(item),
        branch=ExternalWaitAuthority.BRANCH_HUMAN,
    ).first()
    if not authority or item.external_wait_generation != generation:
        raise WorkflowValidationError(
            'The human wait receipt or generation is no longer current.',
            code='human_wait_generation_stale',
        )
    event_id = deterministic_event_id('external_wait.human_settlement', item, receipt)
    result = {
        'receipt': receipt, 'wait_generation': generation, 'released': True,
        'status': 'resolved_elsewhere', 'evidence_sha256': evidence_digest,
    }
    previous = Comment.objects.filter(
        item=item, origin_event_id=event_id,
        origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='external_wait.human_settlement', template_version=1,
    ).first()
    if previous is not None:
        if (
            authority.state == ExternalWaitAuthority.STATE_RELEASED
            and item.status == 'resolved_elsewhere'
            and previous.origin_payload.get('evidence_sha256') == evidence_digest
        ):
            return result
        raise WorkflowValidationError(
            'This receipt already has a different settlement.',
            code='human_wait_settlement_conflict',
        )
    if authority.state != ExternalWaitAuthority.STATE_WAITING or item.status != 'waiting_external':
        raise WorkflowValidationError(
            'This human wait is no longer active.', code='human_wait_generation_stale',
        )

    # Same blocker-first/ordered-parent lock order as execute_transition. Lock
    # link rows and evidence too, so deletion/edit cannot race the status write.
    list(ItemLink.objects.select_for_update().filter(
        Q(link_type='blocks', source_item=item)
        | Q(link_type__in=('is_blocked_by', 'blocked_by'), target_item=item)
    ).order_by('pk'))
    pairs = WorkflowService._resolved_elsewhere_blocker_parents(item)
    parent_ids = {parent.pk for parent, _ in pairs}
    if parent_ids != {ref['parent_id'] for ref in normalized_refs}:
        raise ValidationError({'detail': 'resolution_parent_set_mismatch'})
    parents = list(Item.objects.select_for_update().filter(pk__in=parent_ids).order_by('pk'))
    user_id, email = _actor(user)
    for parent in parents:
        if (
            _item_org_id(parent) != _item_org_id(item)
            or not PermissionService.can_access_item(
                user_id, parent, permission='browse', is_staff=False, user_email=email,
            )
        ):
            raise PermissionDenied('human_wait_settlement_forbidden')
    for ref in normalized_refs:
        comment = Comment.objects.select_for_update().filter(
            pk=ref['comment_id'], item_id=ref['parent_id'],
        ).first()
        if comment is None or hashlib.sha256(comment.content.encode('utf-8')).hexdigest() != ref['sha256']:
            raise ValidationError({'detail': 'resolution_evidence_changed_or_missing'})
    WorkflowService._guard_resolved_elsewhere(item, 'resolved_elsewhere')
    projected = copy(item)
    projected.status = authority.pre_status
    projected.workflow_status = authority.pre_workflow_status
    transition = WorkflowService.validate_transition(
        projected, 'resolved_elsewhere', user, screen_data,
        transition_id=transition_id,
    )
    if (
        not transition or transition is True
        or str(transition.id) != transition_id
        or transition.to_status != 'resolved_elsewhere'
        or transition.post_functions
    ):
        raise ValidationError({'detail': 'settlement_requires_plain_resolution_transition'})

    authority.state = ExternalWaitAuthority.STATE_EXIT_ARMED
    authority.save(update_fields=['state', 'updated_at'])
    item.status = 'resolved_elsewhere'
    item.workflow_status = WorkflowService._workflow_status_display_for_item(item, item.status)
    item._external_wait_authorized = True
    item._activity_actor = _actor(user)
    item._state_mutation_cause = {
        'kind': 'human_external_wait_settlement', 'receipt_id': receipt,
        'wait_generation': str(generation), 'evidence_sha256': evidence_digest,
    }
    item.save(update_fields=['status', 'workflow_status', 'updated_at'])
    authority.state = ExternalWaitAuthority.STATE_RELEASED
    authority.released_at = timezone.now()
    authority.save(update_fields=['state', 'released_at', 'updated_at'])
    MechanicalCommentFactory.create(
        item=item, origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='external_wait.human_settlement', template_version=1,
        structured_payload={
            'receipt': receipt, 'wait_generation': generation,
            'evidence_sha256': evidence_digest, 'evidence': evidence,
        },
        origin_event_id=event_id, causal_actor=user,
    )
    WorkflowService._reconcile_dependency_release(item, user=user, resolved_status=item.status)
    return result


@transaction.atomic
def revoke_cross_org_wait(item: Item, *, user, reason: str) -> dict:
    item = Item.objects.select_for_update().get(pk=item.pk)
    _authorize_item(user, item)
    reason = str(reason or '').strip()
    if not reason:
        raise ValidationError({'reason': 'reason_required'})
    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        wait_generation=item.external_wait_generation,
        branch=ExternalWaitAuthority.BRANCH_CROSS_ORG,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    if not authority:
        raise ValidationError({'detail': 'cross_org_external_wait_not_active'})
    ExternalDependencyBinding.objects.filter(
        authority=authority,
        state__in={
            ExternalDependencyBinding.STATE_PREPARED,
            ExternalDependencyBinding.STATE_ACTIVE,
        },
    ).update(state=ExternalDependencyBinding.STATE_REVOKED, revoked_at=timezone.now())
    if not _restore_wait(item, authority, cause='external_wait_revoked'):
        raise WorkflowValidationError(
            'A higher-impact owned blocker must be handled before this wait resumes.',
            code='BLOCKER_FOCUS_REQUIRED',
        )
    from .mechanical_comments import MechanicalCommentFactory, deterministic_event_id
    receipt = str(authority.receipt_id)
    MechanicalCommentFactory.create(
        item=item,
        origin_kind='WORKFLOW_LIFECYCLE',
        origin_event='external_wait.cross_org_revoked',
        template_version=1,
        structured_payload={'receipt': receipt, 'reason': reason[:500]},
        origin_event_id=deterministic_event_id(
            'external_wait.cross_org_revoked', item, receipt,
        ),
        causal_actor=user,
    )
    return {'receipt': receipt, 'revoked': True}


# ---------------------------------------------------------------------------
# PLS-962 / ADR-0064 branch 3 — bounded external-response holds
# ---------------------------------------------------------------------------

@transaction.atomic
def admit_external_response_hold(
    item: Item,
    *,
    user,
    checkpoint_at,
    source_evidence,
    display_reference,
    correlation_key: str,
) -> dict:
    """Admit one bounded hold through the closed branch-3 contract."""
    if not getattr(settings, 'PULSE_EXTERNAL_RESPONSE_HOLD_ENABLED', False):
        raise ValidationError({'detail': 'external_response_hold_disabled'})
    item = Item.objects.select_for_update(of=('self',)).select_related('project').get(pk=item.pk)
    _authorize_external_response_actor(user, item, action='admit')
    key = _validate_correlation_key(correlation_key)
    if not checkpoint_at or timezone.is_naive(checkpoint_at):
        raise ValidationError({'checkpoint_at': 'timezone_aware_checkpoint_required'})
    now = timezone.now()
    max_days = int(getattr(settings, 'PULSE_EXTERNAL_RESPONSE_MAX_DAYS', 31))
    if checkpoint_at <= now or checkpoint_at > now + timedelta(days=max_days):
        raise ValidationError({'checkpoint_at': 'checkpoint_must_be_future_and_bounded'})
    reference = _validate_display_reference(display_reference)
    snapshot, digest = _canonical_source_evidence(item, user, source_evidence)
    if WorkflowService._status_is_terminal(item.status) or item.resolution_id:
        raise ValidationError({'item': 'external_wait_terminal_item_refused'})

    active = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        state__in={
            ExternalWaitAuthority.STATE_ENTER_ARMED,
            ExternalWaitAuthority.STATE_WAITING,
            ExternalWaitAuthority.STATE_EXIT_ARMED,
        },
    ).first()
    if active:
        if (
            active.branch == ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE
            and (active.evidence or {}).get('admission_correlation_key') == key
        ):
            return {
                'receipt': str(active.receipt_id),
                'wait_generation': active.wait_generation,
                'checkpoint_at': active.checkpoint_at.isoformat(),
                'replayed': True,
            }
        raise ValidationError({'item': 'external_wait_branch_conflict'})

    actor_id, actor_email = _actor(user)
    actor_active = ExternalWaitAuthority.objects.filter(
        branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
        organization_id=_item_org_id(item),
        requested_by_email__iexact=actor_email,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).count()
    org_active = ExternalWaitAuthority.objects.filter(
        branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
        organization_id=_item_org_id(item),
        state=ExternalWaitAuthority.STATE_WAITING,
    ).count()
    if actor_active >= int(getattr(settings, 'PULSE_EXTERNAL_RESPONSE_ACTOR_ACTIVE_LIMIT', 20)):
        raise ValidationError({'detail': 'external_response_actor_active_limit'})
    if org_active >= int(getattr(settings, 'PULSE_EXTERNAL_RESPONSE_ORG_ACTIVE_LIMIT', 500)):
        raise ValidationError({'detail': 'external_response_org_active_limit'})
    recent = ExternalWaitAuthority.objects.filter(
        branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
        organization_id=_item_org_id(item),
        requested_by_email__iexact=actor_email,
        created_at__gte=now - timedelta(minutes=1),
    ).count()
    if recent >= int(getattr(settings, 'PULSE_EXTERNAL_RESPONSE_ACTOR_RATE_PER_MINUTE', 10)):
        raise ValidationError({'detail': 'external_response_actor_rate_limit'})

    generation = _next_wait_generation(item)
    before = _external_response_state(item)
    authority = _create_authority(
        item,
        generation=generation,
        branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
        pre=_pre_state(item),
        user=user,
        evidence={
            'schema': ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
            'source_evidence': snapshot,
            'admission_correlation_key': key,
        },
    )
    authority.checkpoint_at = checkpoint_at
    authority.source_evidence_digest = digest
    authority.display_reference = reference
    authority.revalidate_after = checkpoint_at
    authority.save(update_fields=[
        'checkpoint_at', 'source_evidence_digest', 'display_reference',
        'revalidate_after', 'updated_at',
    ])
    _enter_wait(item, authority)
    _audit_external_response(
        authority,
        event_type='enter',
        correlation_key=key,
        actor=user,
        evidence_digest=digest,
        before=before,
        after=_external_response_state(item),
        reason_code='admitted',
    )
    return {
        'receipt': str(authority.receipt_id),
        'wait_generation': generation,
        'checkpoint_at': checkpoint_at.isoformat(),
        'replayed': False,
    }


@transaction.atomic
def enqueue_external_response_wake(
    item: Item,
    *,
    generation: int,
    kind: str,
    correlation_key: str,
    user=None,
    evidence=None,
    service_identity: str = '',
) -> dict:
    """Commit a generation-scoped wake before best-effort broker enqueue."""
    item = Item.objects.select_for_update(of=('self',)).select_related('project').get(pk=item.pk)
    if service_identity and kind != ExternalResponseHoldWake.KIND_CHECKPOINT:
        raise ValidationError({'service_identity': 'caller_supplied_identity_forbidden'})
    key = _validate_correlation_key(correlation_key)
    if kind == ExternalResponseHoldWake.KIND_CHECKPOINT:
        if user is not None or service_identity != 'pulse-checkpoint-scheduler':
            raise PermissionDenied('checkpoint_system_scheduler_only')
    elif kind in {
        ExternalResponseHoldWake.KIND_EVIDENCE,
        ExternalResponseHoldWake.KIND_REVOKE,
    }:
        _authorize_external_response_actor(
            user, item, action='wake' if kind == ExternalResponseHoldWake.KIND_EVIDENCE else 'revoke',
        )
        actor_email = _actor(user)[1]
        if kind == ExternalResponseHoldWake.KIND_EVIDENCE and actor_email in {
            value.strip().lower()
            for value in str(
                getattr(settings, 'PULSE_EXTERNAL_RESPONSE_EVIDENCE_PRODUCERS', '')
            ).split(',')
            if value.strip()
        }:
            # Service identity is derived from the authenticated principal;
            # callers never get to stamp an arbitrary service name.
            service_identity = actor_email
    else:
        raise ValidationError({'kind': 'invalid_external_response_wake_kind'})

    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
        wait_generation=generation,
    ).first()
    if authority is None:
        raise ValidationError({'detail': 'external_response_hold_not_active'})

    evidence_digest = ''
    if kind == ExternalResponseHoldWake.KIND_EVIDENCE:
        _snapshot, evidence_digest = _canonical_source_evidence(item, user, evidence)
        if evidence_digest == authority.source_evidence_digest:
            raise ValidationError({'source_evidence': 'wake_requires_new_evidence'})

    actor_id, actor_email = _actor(user)
    wake, created = ExternalResponseHoldWake.objects.get_or_create(
        authority=authority,
        correlation_key=key,
        defaults={
            'organization_id': authority.organization_id,
            'item': item,
            'wait_generation': generation,
            'kind': kind,
            'actor_id': actor_id,
            'actor_email': actor_email,
            'service_identity': service_identity[:128],
            'evidence_digest': evidence_digest,
        },
    )
    if not created and (
        wake.kind != kind
        or wake.wait_generation != int(generation)
        or wake.actor_id != actor_id
        or wake.actor_email != actor_email
        or wake.service_identity != service_identity[:128]
        or wake.evidence_digest != evidence_digest
    ):
        _audit_external_response(
            authority,
            event_type='conflict',
            correlation_key=key,
            actor=user,
            service_identity=service_identity,
            evidence_digest=evidence_digest,
            before=_external_response_state(item),
            after=_external_response_state(item),
            reason_code='correlation_payload_conflict',
        )
        raise ValidationError({'correlation_key': 'correlation_payload_conflict'})

    if created and not getattr(settings, 'TESTING', False):
        transaction.on_commit(
            lambda wake_id=str(wake.id):
                __import__(
                    'tasks.tasks', fromlist=['deliver_external_response_hold_wake']
                ).deliver_external_response_hold_wake.delay(wake_id)
        )
    return {
        'wake_id': str(wake.id),
        'accepted': True,
        'replayed': not created,
        'state': wake.state,
    }


@transaction.atomic
def process_external_response_wake(wake_id) -> str:
    """Locked generation-fenced reducer for evidence/checkpoint/revoke wakes."""
    wake = ExternalResponseHoldWake.objects.select_for_update().get(pk=wake_id)
    if wake.state != ExternalResponseHoldWake.STATE_PENDING:
        return wake.state
    authority = ExternalWaitAuthority.objects.select_for_update().get(pk=wake.authority_id)
    item = Item.objects.select_for_update().get(pk=authority.item_id)
    before = _external_response_state(item)

    stale = (
        authority.branch != ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE
        or authority.state != ExternalWaitAuthority.STATE_WAITING
        or item.external_wait_generation != wake.wait_generation
        or authority.wait_generation != wake.wait_generation
    )
    if stale:
        wake.state = ExternalResponseHoldWake.STATE_STALE
        wake.reason_code = 'generation_stale'
        wake.processed_at = timezone.now()
        wake.save(update_fields=['state', 'reason_code', 'processed_at', 'updated_at'])
        _audit_external_response(
            authority,
            event_type='stale',
            correlation_key=wake.correlation_key,
            service_identity=wake.service_identity,
            evidence_digest=wake.evidence_digest,
            before=before,
            after=before,
            reason_code='generation_stale',
        )
        return wake.state
    if (
        wake.kind in {
            ExternalResponseHoldWake.KIND_EVIDENCE,
            ExternalResponseHoldWake.KIND_REVOKE,
        }
        and not (
            _email_matches(wake.actor_email, item.assignee_email or '')
            or (
                not (item.assignee_email or '').strip()
                and bool(item.assignee_id)
                and wake.actor_id == item.assignee_id
            )
            or (
                item.assignment_group
                and WorkflowService._is_team_member(
                    wake.actor_email, item.assignment_group, item=item,
                )
            )
            or (
                wake.kind == ExternalResponseHoldWake.KIND_EVIDENCE
                and wake.actor_email in {
                    value.strip().lower()
                    for value in str(
                        getattr(
                            settings,
                            'PULSE_EXTERNAL_RESPONSE_EVIDENCE_PRODUCERS',
                            '',
                        )
                    ).split(',')
                    if value.strip()
                }
            )
        )
    ):
        wake.state = ExternalResponseHoldWake.STATE_REFUSED
        wake.reason_code = 'actor_no_longer_eligible'
        wake.processed_at = timezone.now()
        wake.save(update_fields=['state', 'reason_code', 'processed_at', 'updated_at'])
        _audit_external_response(
            authority,
            event_type='refusal',
            correlation_key=wake.correlation_key,
            actor=None,
            evidence_digest=wake.evidence_digest,
            before=before,
            after=before,
            reason_code='actor_no_longer_eligible',
        )
        return wake.state
    if (
        wake.kind == ExternalResponseHoldWake.KIND_CHECKPOINT
        and authority.checkpoint_at
        and authority.checkpoint_at > timezone.now()
    ):
        wake.state = ExternalResponseHoldWake.STATE_REFUSED
        wake.reason_code = 'checkpoint_not_due'
        wake.processed_at = timezone.now()
        wake.save(update_fields=['state', 'reason_code', 'processed_at', 'updated_at'])
        _audit_external_response(
            authority,
            event_type='refusal',
            correlation_key=wake.correlation_key,
            service_identity=wake.service_identity,
            before=before,
            after=before,
            reason_code='checkpoint_not_due',
        )
        return wake.state
    if not _restore_wait(
        item,
        authority,
        cause='external_response_hold_' + wake.kind,
    ):
        wake.attempts += 1
        wake.last_error = 'focus_blocked'
        wake.next_attempt_at = timezone.now() + timedelta(minutes=5)
        wake.save(update_fields=[
            'attempts', 'last_error', 'next_attempt_at', 'updated_at',
        ])
        return 'pending'

    if wake.kind == ExternalResponseHoldWake.KIND_REVOKE:
        authority.state = ExternalWaitAuthority.STATE_REVOKED
        authority.revoked_at = timezone.now()
        authority.save(update_fields=['state', 'revoked_at', 'updated_at'])
        event_type = 'revoke'
    else:
        event_type = 'resume'
    wake.state = ExternalResponseHoldWake.STATE_APPLIED
    wake.reason_code = 'applied'
    wake.processed_at = timezone.now()
    wake.save(update_fields=['state', 'reason_code', 'processed_at', 'updated_at'])
    _audit_external_response(
        authority,
        event_type=event_type,
        correlation_key=wake.correlation_key,
        service_identity=wake.service_identity,
        evidence_digest=wake.evidence_digest,
        before=before,
        after=_external_response_state(item),
        reason_code=wake.kind,
    )
    return wake.state
