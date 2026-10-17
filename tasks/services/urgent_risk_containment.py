"""PLS-620 event-driven urgent risk ownership and containment gate.

The Item write and this reconciliation share the caller's database transaction.
That gives rollback-safe decisions/comments without a periodic sweep, while the
one-to-one projection plus versioned events makes replays idempotent and auditable.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import (
    Comment,
    ContainmentDecision,
    ContainmentDecisionEvent,
    Item,
    Status,
)


RISK_MARKERS = frozenset({'data-loss', 'user-harm'})
RESOLVED_STATES = frozenset({
    ContainmentDecision.STATE_APPLIED,
    ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED,
})
SAFE_PENDING_TARGETS = frozenset({
    'backlog', 'blocked', 'cancelled', 'canceled', 'deferred', 'discarded',
    'needs_triage', 'open', 'pending', 'scheduled', 'todo',
})


def _canonical_marker(value: Any) -> str:
    marker = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    marker = re.sub(r'-+', '-', marker)
    for prefix in ('risk:', 'risk/', 'risk-'):
        if marker.startswith(prefix):
            marker = marker[len(prefix):]
            break
    return marker.strip('-')


def explicit_risk_markers(item: Item) -> list[str]:
    """Return only explicit, exact risk classifications from labels/tags."""

    candidates: list[Any] = []
    labels = getattr(item, 'labels', None)
    if isinstance(labels, dict):
        for key, value in labels.items():
            # A false/disabled label is not an active classification.
            if str(value).strip().lower() not in {'', '0', 'false', 'no', 'off', 'none'}:
                candidates.append(key)
            candidates.append(value)
    elif isinstance(labels, (list, tuple, set)):
        candidates.extend(labels)
    elif labels:
        candidates.append(labels)

    tags = getattr(item, 'tags', None)
    if isinstance(tags, (list, tuple, set)):
        candidates.extend(tags)
    elif tags:
        candidates.extend(re.split(r'[,;\s]+', str(tags)))

    return sorted({
        marker
        for marker in (_canonical_marker(value) for value in candidates)
        if marker in RISK_MARKERS
    })


def requires_containment(item: Item) -> bool:
    return (
        getattr(item, 'mode', None) == 'task'
        and str(getattr(item, 'priority', '') or '').strip().lower() == 'urgent'
        and bool(explicit_risk_markers(item))
    )


def _unsafe_status(status_slug: str | None) -> bool:
    slug = str(status_slug or '').strip().lower().replace('-', '_')
    if not slug or slug in SAFE_PENDING_TARGETS:
        return False
    category = Status.objects.filter(slug__iexact=slug).values_list('category', flat=True).first()
    if category:
        return category in {'in_progress', 'done'}
    # Fail closed for the common workflow execution/review/terminal vocabulary,
    # but do not strand an unknown triage/todo slug merely because it is new.
    return any(token in slug for token in (
        'progress', 'review', 'merge', 'verify', 'verification', 'implement',
        'approved', 'complete', 'done', 'resolved', 'closed',
    ))


def _decision_for_item(item: Item):
    if not getattr(item, 'pk', None):
        return None
    return ContainmentDecision.objects.filter(item_id=item.pk).first()


def containment_gate_message(item: Item, decision: ContainmentDecision | None = None) -> str:
    decision = decision or _decision_for_item(item)
    version = getattr(decision, 'version', 0) or 0
    owner = (
        getattr(decision, 'owner_email', '')
        or getattr(decision, 'owner_group', '')
        or getattr(item, 'assignment_group', '')
        or 'unresolved owning lane'
    )
    return (
        'Urgent data-loss/user-harm containment is pending '
        f'(decision v{version}, owner={owner}). Record `applied` or an '
        'evidence-backed `explicitly_not_required` decision before ordinary '
        'implementation/review/close progression.'
    )


def assert_status_write_allowed(item: Item) -> None:
    """Fail closed at the model writer boundary for unsafe status writes."""

    if not _unsafe_status(getattr(item, 'status', None)):
        return
    decision = _decision_for_item(item)
    if decision is not None and decision.state in RESOLVED_STATES:
        return
    if decision is not None and decision.state == ContainmentDecision.STATE_PENDING:
        raise ValidationError({'status': containment_gate_message(item, decision)})
    if requires_containment(item):
        raise ValidationError({'status': containment_gate_message(item)})


def assert_transition_allowed(item: Item, target_status: str) -> None:
    """Workflow-facing form of the same pending-decision gate."""

    if not _unsafe_status(target_status):
        return
    decision = _decision_for_item(item)
    if decision is not None and decision.state in RESOLVED_STATES:
        return
    if decision is None and not requires_containment(item):
        return
    from .workflow_service import WorkflowValidationError

    raise WorkflowValidationError(
        containment_gate_message(item, decision),
        code='URGENT_RISK_CONTAINMENT_PENDING',
    )


def _actor_tuple(item: Item) -> tuple[int | None, str]:
    actor = getattr(item, '_activity_actor', None)
    if actor:
        return actor[0], str(actor[1] or '').strip().lower()
    return None, ''


def _lead_identity(group: str, item: Item) -> tuple[int | None, str]:
    if not group:
        return None, ''
    from .workflow_service import WorkflowService

    email = str(
        WorkflowService._resolve_team_lead_email(group, item=item) or ''
    ).strip().lower()
    if not email:
        return None, ''
    try:
        from ..user_service import user_service

        row = user_service.get_user_by_email(email) or {}
        uid = row.get('id')
        return (int(uid) if uid and str(uid).isdigit() else None), email
    except Exception:
        return None, email


def _owner_is_compatible(item: Item) -> bool:
    email = str(getattr(item, 'assignee_email', '') or '').strip().lower()
    group = str(getattr(item, 'assignment_group', '') or '').strip()
    if not email or not group:
        return False
    from .workflow_service import WorkflowService

    verdict = WorkflowService._seat_routability_verdict(
        item,
        group,
        email,
        intent='new_assignment',
        enforce_cap=True,
        # PLS-639: urgent data-loss/user-harm ownership is a safety control,
        # not a fleet-wide shadow rollout.  Missing liveness authority must not
        # turn a provisional seat into the nominal containment owner.
        require_live_evidence=True,
        user_id=getattr(item, 'assignee_id', None),
    )
    return verdict.ok


def _assign_compatible_owner(item: Item) -> bool:
    """Resolve through the accepted PLS-277 selector; never widen the lane."""

    if _owner_is_compatible(item):
        return False
    had_concrete_owner = bool(
        getattr(item, 'assignee_id', None)
        or str(getattr(item, 'assignee_email', '') or '').strip()
    )
    group = str(getattr(item, 'assignment_group', '') or '').strip()
    if not group:
        return False
    from .workflow_service import WorkflowService

    uid, email = WorkflowService._resolve_team_assignee(
        group,
        enforce_cap=True,
        require_live_evidence=True,
        item=item,
        project_key=WorkflowService._item_project_key(item),
    )
    if not email:
        if not had_concrete_owner:
            return False
        # The retained owner already failed the canonical new-assignment
        # predicate.  If the exact lane has no replacement, preserve the lane
        # but clear that incompatible concrete owner through the guarded
        # group-queue command.  Leaving it attached would make the loud HOLD
        # informational only and violate the PLS-277 no-candidate invariant.
        item.unassign(save=False, reason='no_candidate')
        item._suppress_urgent_risk_reconcile = True
        try:
            item.save(update_fields=['assignee_id', 'assignee_email'])
        finally:
            item._suppress_urgent_risk_reconcile = False
        return True
    update_fields: list[str] = []
    WorkflowService._apply_resolved_assignee(item, uid, email, update_fields)
    item._suppress_urgent_risk_reconcile = True
    try:
        item.save(update_fields=update_fields)
    finally:
        item._suppress_urgent_risk_reconcile = False
    return bool(item.assignee_email)


def _snapshot(decision: ContainmentDecision) -> dict[str, Any]:
    return {
        'state': decision.state,
        'owner_id': decision.owner_id,
        'owner_email': str(decision.owner_email or '').strip().lower(),
        'owner_group': str(decision.owner_group or '').strip(),
        'risk_markers': sorted(decision.risk_markers or []),
        'requirement_active': bool(decision.requirement_active),
        'reason': str(decision.reason or ''),
        'evidence': decision.evidence or {},
    }


def _persist_projection(
    decision: ContainmentDecision,
    *,
    state: str,
    owner_id: int | None,
    owner_email: str,
    owner_group: str,
    risk_markers: list[str],
    requirement_active: bool,
    reason: str,
    evidence: dict[str, Any],
    trigger: str,
    actor_id: int | None,
    actor_email: str,
) -> tuple[ContainmentDecision, bool]:
    target = {
        'state': state,
        'owner_id': owner_id,
        'owner_email': str(owner_email or '').strip().lower(),
        'owner_group': str(owner_group or '').strip(),
        'risk_markers': sorted(risk_markers or []),
        'requirement_active': bool(requirement_active),
        'reason': str(reason or ''),
        'evidence': evidence or {},
    }
    if _snapshot(decision) == target:
        return decision, False

    decision.state = target['state']
    decision.owner_id = target['owner_id']
    decision.owner_email = target['owner_email']
    decision.owner_group = target['owner_group']
    decision.risk_markers = target['risk_markers']
    decision.requirement_active = target['requirement_active']
    decision.reason = target['reason']
    decision.evidence = target['evidence']
    decision.version += 1
    decision.decided_at = (
        timezone.now() if state in RESOLVED_STATES else None
    )
    decision.save(update_fields=[
        'state', 'owner_id', 'owner_email', 'owner_group', 'risk_markers',
        'requirement_active',
        'reason', 'evidence', 'version', 'decided_at', 'updated_at',
    ])
    ContainmentDecisionEvent.objects.create(
        decision=decision,
        version=decision.version,
        state=decision.state,
        owner_id=decision.owner_id,
        owner_email=decision.owner_email,
        owner_group=decision.owner_group,
        risk_markers=decision.risk_markers,
        requirement_active=decision.requirement_active,
        reason=decision.reason,
        evidence=decision.evidence,
        trigger=trigger,
        actor_id=actor_id,
        actor_email=actor_email,
    )
    return decision, True


def _create_pending(
    item: Item,
    *,
    markers: list[str],
    owner_id: int | None,
    owner_email: str,
    owner_group: str,
    trigger: str,
    actor_id: int | None,
    actor_email: str,
) -> ContainmentDecision:
    decision = ContainmentDecision.objects.create(
        item=item,
        state=ContainmentDecision.STATE_PENDING,
        owner_id=owner_id,
        owner_email=owner_email,
        owner_group=owner_group,
        risk_markers=markers,
        requirement_active=True,
        reason='Urgent explicit risk classification requires a containment decision.',
        evidence={},
        version=1,
    )
    ContainmentDecisionEvent.objects.create(
        decision=decision,
        version=1,
        state=decision.state,
        owner_id=owner_id,
        owner_email=owner_email,
        owner_group=owner_group,
        risk_markers=markers,
        requirement_active=True,
        reason=decision.reason,
        evidence={},
        trigger=trigger,
        actor_id=actor_id,
        actor_email=actor_email,
    )
    return decision


def _comment_once(item: Item, token: str, content: str) -> None:
    if Comment.objects.filter(item=item, content__contains=token).exists():
        return
    Comment.objects.create(
        item=item,
        author_id=0,
        author_email='system@pulse',
        content=content,
    )


def reconcile_item(item_id: int, *, trigger: str = 'item_write') -> dict[str, Any]:
    """Reconcile one item synchronously inside the triggering transaction."""

    with transaction.atomic():
        item = (
            Item.objects.select_for_update()
            .get(pk=item_id)
        )
        markers = explicit_risk_markers(item)
        required = requires_containment(item)
        decision = (
            ContainmentDecision.objects.select_for_update()
            .filter(item=item)
            .first()
        )
        if not required and decision is None:
            return {'decision': None, 'owner_changed': False}

        owner_changed = _assign_compatible_owner(item) if required else False
        group = str(item.assignment_group or '').strip()
        owner_id = item.assignee_id
        owner_email = str(item.assignee_email or '').strip().lower()
        compatible = _owner_is_compatible(item)
        if not compatible:
            lead_id, lead_email = _lead_identity(group, item)
            owner_id = lead_id
            owner_email = lead_email

        actor_id, actor_email = _actor_tuple(item)
        created = False
        changed = False
        if decision is None:
            decision = _create_pending(
                item,
                markers=markers,
                owner_id=owner_id,
                owner_email=owner_email,
                owner_group=group,
                trigger=trigger,
                actor_id=actor_id,
                actor_email=actor_email,
            )
            created = True
            changed = True
        else:
            # A containment epoch is keyed by the requirement's own false -> true
            # edge, not merely by marker changes.  Priority may deactivate and
            # reactivate the requirement while the same explicit marker remains.
            was_required = bool(decision.requirement_active)
            activated_markers = sorted(
                set(markers) - set(decision.risk_markers or [])
            )
            state = decision.state
            reason = decision.reason
            evidence = decision.evidence or {}
            fresh_epoch = required and not was_required
            if required and state in RESOLVED_STATES and (fresh_epoch or activated_markers):
                state = ContainmentDecision.STATE_PENDING
                reason = 'A new urgent risk epoch requires a fresh containment decision.'
                if activated_markers:
                    reason += ' Activated classifications: ' + ', '.join(activated_markers)
                evidence = {'supersedes_version': decision.version}
            decision, changed = _persist_projection(
                decision,
                state=state,
                owner_id=owner_id,
                owner_email=owner_email,
                owner_group=group,
                risk_markers=markers,
                requirement_active=required,
                reason=reason,
                evidence=evidence,
                trigger=trigger,
                actor_id=actor_id,
                actor_email=actor_email,
            )

        if decision.state == ContainmentDecision.STATE_PENDING:
            _comment_once(
                item,
                f'[PLS-620 containment pending v{decision.version}]',
                (
                    f'[PLS-620 containment pending v{decision.version}] '
                    f'Risk={",".join(decision.risk_markers)}; '
                    f'owner={decision.owner_email or decision.owner_group or "unresolved"}. '
                    'Ordinary implementation/review/close progression is gated until '
                    '`applied` or evidence-backed `explicitly_not_required` is recorded.'
                ),
            )
            if not compatible:
                _comment_once(
                    item,
                    f'[PLS-620 exact-lane hold v{decision.version}]',
                    (
                        f'[PLS-620 exact-lane hold v{decision.version}] No staffed, live, '
                        f'under-cap compatible owner is available in exact lane '
                        f'`{group or "missing"}`. The task remains on that lane; no '
                        'cross-team or over-cap fallback was assigned.'
                    ),
                )

        return {
            'decision': decision,
            'owner_changed': owner_changed,
            'created': created,
            'changed': changed,
            'assignee_id': item.assignee_id,
            'assignee_email': item.assignee_email,
            'assignment_group': item.assignment_group,
        }


def record_decision(
    item_id: int,
    *,
    state: str,
    reason: str,
    evidence: dict[str, Any],
    actor_id: int | None,
    actor_email: str,
    trigger: str = 'decision_api',
) -> ContainmentDecision:
    """Apply an explicit evidence-backed decision without deleting history."""

    if state not in {
        ContainmentDecision.STATE_PENDING,
        ContainmentDecision.STATE_APPLIED,
        ContainmentDecision.STATE_EXPLICITLY_NOT_REQUIRED,
    }:
        raise ValidationError({'state': 'Invalid containment decision state.'})
    if not str(reason or '').strip():
        raise ValidationError({'reason': 'A concrete reason is required.'})
    if not isinstance(evidence, dict) or not evidence:
        raise ValidationError({'evidence': 'A non-empty evidence object is required.'})

    with transaction.atomic():
        item = Item.objects.select_for_update().get(pk=item_id)
        decision = (
            ContainmentDecision.objects.select_for_update()
            .filter(item=item)
            .first()
        )
        if decision is None:
            raise ValidationError({
                'state': 'No urgent-risk containment requirement exists for this item.'
            })
        decision, changed = _persist_projection(
            decision,
            state=state,
            owner_id=item.assignee_id or decision.owner_id,
            owner_email=item.assignee_email or decision.owner_email,
            owner_group=item.assignment_group or decision.owner_group,
            risk_markers=list(decision.risk_markers or []),
            requirement_active=requires_containment(item),
            reason=str(reason).strip(),
            evidence=evidence,
            trigger=trigger,
            actor_id=actor_id,
            actor_email=str(actor_email or '').strip().lower(),
        )
        if changed:
            Comment.objects.create(
                item=item,
                author_id=actor_id or 0,
                author_email=actor_email or 'system@pulse',
                content=(
                    f'[PLS-620 containment decision v{decision.version}] '
                    f'`{decision.state}` — {decision.reason} '
                    f'(evidence keys: {", ".join(sorted(decision.evidence))})'
                ),
            )
        return decision


def serialize_decision(item: Item) -> dict[str, Any] | None:
    try:
        decision = item.containment_decision
    except ContainmentDecision.DoesNotExist:
        return None
    return {
        'state': decision.state,
        'owner_id': decision.owner_id,
        'owner_email': decision.owner_email,
        'owner_group': decision.owner_group,
        'risk_markers': list(decision.risk_markers or []),
        'requirement_active': bool(decision.requirement_active),
        'reason': decision.reason,
        'evidence': decision.evidence or {},
        'version': decision.version,
        'required_at': decision.required_at,
        'decided_at': decision.decided_at,
        'updated_at': decision.updated_at,
    }
