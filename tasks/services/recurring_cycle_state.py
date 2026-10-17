"""Authoritative recurring-cycle state machine (PLS-419).

Redis timers and Connect notifications are at-least-once delivery mechanisms.
The item row and its ``metadata.recurring_cycle`` document are the authority:
every mutation is serialized with ``select_for_update`` and advances a
monotonic revision.  Callers that cross an asynchronous boundary carry the
revision and due boundary they observed; an older delivery becomes a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tasks.services.recurring import (
    calculate_next_future_due_date,
    parse_interval_seconds,
)

logger = logging.getLogger(__name__)

LANE_LIVENESS_CACHE_KEY = 'pulse:recurring_cycle_lane:last_processed'


def record_lane_liveness(*, kind: str, item=None, now: datetime) -> None:
    """PLS-1286: lane-level liveness watermark for the recurring-cycle scheduler.

    Written on EVERY cycle processing (fire or skip) so a meta-watchdog can
    alarm within one period when the recurring lane stops firing entirely —
    the PLAT-528 failure shape (6 consecutive periods dark, no owner-visible
    signal until the period-6 escalation). Fail-soft by contract: the
    watermark is a diagnostic, never a gate.
    """
    try:
        from django.core.cache import cache
        cache.set(LANE_LIVENESS_CACHE_KEY, {
            'at': now.isoformat(),
            'kind': kind,
            'item': getattr(item, 'item_key', None) or getattr(item, 'id', None),
        }, timeout=None)
    except Exception:  # pragma: no cover - diagnostic only, never self-fatal
        pass


def _notify_assignee_missed_fire(item, *, count: int, boundary: datetime) -> None:
    """PLS-1286: owner-visible signal at the FIRST missed fire.

    The PLAT-528 stall shape: the escalation lane fires only at counts 3/6/9
    and the count-2 marker is a bare Comment, so periods 1-5 accrued with no
    push signal to the seat holding the cadence. Notify the assignee directly
    (fail-soft — a notification failure must never break the cycle advance).
    """
    if not getattr(item, 'assignee_id', None):
        return
    try:
        from tasks.services.notifications import NotificationService
        NotificationService().notify_user(
            item.assignee_id,
            f"{item.item_key or f'#{item.id}'}: recurring cycle missed a fire period",
            (
                f"<b>{item.title}</b><br>"
                f"The recurring cadence missed its fire at boundary "
                f"{boundary.isoformat()} (missed period #{count}). "
                f"Advance the cycle or use Deferred to intentionally stop it. "
                f"[PLS-1286]"
            ),
            event_type='alert',
            severity='warning',
            item=item,
            fields=[
                {'name': 'Missed periods', 'value': str(count), 'inline': True},
                {'name': 'Boundary', 'value': boundary.isoformat(), 'inline': True},
            ],
        )
    except Exception:  # pragma: no cover - fail-soft per PLAT-1254
        pass


STOP_STATUSES = {
    'cancelled', 'canceled', 'deferred', 'expired', 'rejected', 'duplicate',
    'wont_fix', 'failed',
}

# A due team-owned recurrence gets a short, finite opportunity to resolve a
# concrete seat.  Exhaustion advances to the next natural boundary and leaves
# a durable ANDON instead of either hot-looping or losing the timer forever.
ASSIGNMENT_RETRY_LIMIT = 3
ASSIGNMENT_RETRY_DELAY = timedelta(minutes=5)


class RecurringCycleError(Exception):
    """A stable API error raised by recurring-cycle validation."""

    def __init__(self, code: str, message: str, current: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        # PLS-792: the current cycle snapshot carried on stale/required errors so
        # a client can see the real revision and retry without private knowledge.
        self.current = current or {}


@dataclass
class CycleResult:
    item: Any
    applied: bool
    reason: str
    kind: str = ''
    old_status: str = ''
    new_status: str = ''
    next_due: Optional[datetime] = None
    revision: int = 0
    generation: int = 0
    skip_count: int = 0
    lifted: bool = False
    notify_team: str = ''
    event_id: str = ''
    replay: bool = False


@dataclass
class RouteValidation:
    """Result of validating the durable recurring-template route."""

    held: bool = False
    reason: str = ''
    fields: tuple[str, ...] = ()


def _canonical_team(value) -> str:
    from tasks.services.workflow_service import WorkflowService

    return WorkflowService._canonical_team_name(value)


def _route_config(item, metadata: dict) -> tuple[dict, str]:
    routing = metadata.get('recurring_routing')
    routing = dict(routing) if isinstance(routing, dict) else {}
    project_settings = getattr(getattr(item, 'project', None), 'settings', None)
    project_settings = project_settings if isinstance(project_settings, dict) else {}
    fallback = _canonical_team(
        routing.get('fallback_assignment_group')
        or project_settings.get('recurring_routing_fallback_assignment_group')
        or ''
    )
    return routing, fallback


def _team_route_state(item, team_name: str) -> dict:
    """Read one exact org-scoped TeamRoster row and live capability state.

    This deliberately bypasses the cached aggregate roster: recurrence
    generation is the validation boundary, so a deleted/disabled/emptied team
    must be observed on the very next cycle.
    """
    from tasks.models import TeamRoster
    from tasks.services.workflow_service import WorkflowService

    team = _canonical_team(team_name)
    required = WorkflowService._task_required_capabilities(item)
    project = getattr(item, 'project', None)
    org_id = getattr(project, 'org_id', None)
    row = None
    if team and org_id is not None:
        row = (TeamRoster.objects
               .filter(team_name=team, organization_id=org_id)
               .order_by('-is_active', '-updated_at')
               .first())
    members = WorkflowService._flat_roster(getattr(row, 'tiers', None) or []) if row else []
    live = [
        email for email in members
        if WorkflowService._is_agent_available_for_assignment(email)
    ]
    capable = [
        email for email in live
        if WorkflowService._seat_satisfies_required_capability(email, required)
    ]
    if not row:
        reason = 'deleted'
    elif not row.is_active:
        reason = 'disabled'
    elif not members:
        reason = 'zero_member'
    elif not live:
        reason = 'no_live_member'
    elif required and not capable:
        reason = 'capability_incompatible'
    else:
        reason = ''
    signature = {
        'team': team,
        'org_id': org_id,
        'row_id': getattr(row, 'id', None),
        'active': getattr(row, 'is_active', None),
        'tiers': getattr(row, 'tiers', None),
        'updated_at': _iso(getattr(row, 'updated_at', None)),
        'required_capabilities': sorted(required),
        'reason': reason,
    }
    return {
        'team': team,
        'eligible': not reason,
        'reason': reason,
        'members': members,
        'eligible_members': capable if required else live,
        'signature': signature,
    }


def _same_identity(left, right) -> bool:
    from tasks.services.workflow_service import WorkflowService

    return bool(
        WorkflowService._agent_email_aliases(left)
        & WorkflowService._agent_email_aliases(right)
    )


def _current_owner_is_eligible(item, state: dict) -> bool:
    email = (getattr(item, 'assignee_email', '') or '').strip().lower()
    return bool(
        state.get('eligible')
        and email
        and any(_same_identity(email, member) for member in state['eligible_members'])
        and _canonical_team(getattr(item, 'assignment_group', '')) == state['team']
    )


def _condition_fingerprint(invalid_state: dict, fallback_state: dict | None) -> str:
    payload = {
        'invalid': invalid_state.get('signature'),
        'fallback': fallback_state.get('signature') if fallback_state else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _record_route_discrepancy(
        item, metadata: dict, routing: dict, *, invalid_state: dict,
        fallback_state: dict | None, outcome: str, now) -> None:
    """Annotate and alert once per template/group condition fingerprint."""
    from tasks.models import Comment

    invalid_group = invalid_state['team']
    fingerprint = _condition_fingerprint(invalid_state, fallback_state)
    previous = routing.get('discrepancy')
    previous = dict(previous) if isinstance(previous, dict) else {}
    same_condition = bool(
        previous.get('active')
        and previous.get('invalid_group') == invalid_group
    )
    discrepancy = {
        'active': True,
        'invalid_group': invalid_group,
        'reason': invalid_state['reason'],
        'effective_group': (
            fallback_state['team']
            if outcome == 'fallback' and fallback_state else ''
        ),
        'outcome': outcome,
        'condition_fingerprint': fingerprint,
        'first_seen_at': (
            previous.get('first_seen_at') if same_condition
            else now.isoformat()
        ),
        'last_seen_at': now.isoformat(),
    }
    last_alert = previous.get('last_alert_fingerprint') if same_condition else None
    if last_alert != fingerprint:
        fallback = fallback_state['team'] if fallback_state else 'none configured'
        # PLAT-1354 comment-writer guard: system-authored mechanical alerts mint
        # through the factory (per-occurrence origin_event_id so a re-alert of
        # the same fingerprint after a clear still surfaces; a mint retry is a
        # byte-identical no-op replay).
        from tasks.services.mechanical_comments import MechanicalCommentFactory
        MechanicalCommentFactory.create(
            item=item,
            origin_kind=Comment.ORIGIN_RESOLVER_AUDIT,
            origin_event='resolver.recurring_route_discrepancy',
            template_version=1,
            structured_payload={
                'item_id': str(item.pk),
                'invalid_group': str(invalid_group),
                'reason': str(invalid_state['reason']),
                'outcome': str(outcome),
                'fallback': str(fallback),
                'fingerprint': str(fingerprint),
            },
            origin_event_id=(
                f'recurring-route:{item.pk}:{fingerprint}:'
                f'{int(now.timestamp())}'
            ),
        )
        discrepancy['last_alert_fingerprint'] = fingerprint
        discrepancy['last_alert_at'] = now.isoformat()
    elif last_alert:
        discrepancy['last_alert_fingerprint'] = last_alert
        discrepancy['last_alert_at'] = previous.get('last_alert_at')
    routing['discrepancy'] = discrepancy
    metadata['recurring_routing'] = routing


def _clear_route_discrepancy(item, metadata: dict, routing: dict, *, now, reason: str) -> None:
    from tasks.models import Comment

    previous = routing.get('discrepancy')
    previous = dict(previous) if isinstance(previous, dict) else {}
    if not previous.get('active'):
        return
    previous['active'] = False
    previous['cleared_at'] = now.isoformat()
    previous['cleared_reason'] = reason
    routing['discrepancy'] = previous
    metadata['recurring_routing'] = routing
    from tasks.services.mechanical_comments import MechanicalCommentFactory
    MechanicalCommentFactory.create(
        item=item,
        origin_kind=Comment.ORIGIN_RESOLVER_AUDIT,
        origin_event='resolver.recurring_route_cleared',
        template_version=1,
        structured_payload={
            'invalid_group': str(previous.get('invalid_group') or 'unknown'),
            'reason': str(reason),
        },
        origin_event_id=(
            f'recurring-route-clear:{item.pk}:'
            f'{int(now.timestamp())}'
        ),
    )


def _hold_route(item, fields: list[str]) -> None:
    if getattr(item, 'assignee_id', None) or (getattr(item, 'assignee_email', '') or '').strip():
        item.unassign(save=False, reason='routability_hold')
        item.assignee_email = ''
        fields.extend(['assignee_id', 'assignee_email'])


def _apply_route(item, metadata: dict, state: dict, fields: list[str]) -> bool:
    """Route to one already-validated team and resolve a concrete live owner."""
    from tasks.services.workflow_service import WorkflowService

    team = state['team']
    if _current_owner_is_eligible(item, state):
        metadata['recurring_owner_team'] = team
        return True
    old_group = getattr(item, 'assignment_group', None)
    item.assignment_group = team
    uid, email = WorkflowService._resolve_team_assignee(
        team,
        exclude_email=getattr(item, 'created_by_email', None),
        enforce_cap=False,
        project_key=WorkflowService._item_project_key(item),
        item=item,
    )
    if not (uid and email):
        item.assignment_group = old_group
        return False
    # PLS-528 (main interplay): main's _apply_resolved_assignee signals success
    # by mutating the item (assignee_id/email + fields), not by its return
    # value — main's own tests mock it with a None-returning side effect. Judge
    # success by the persisted assignee, never by the return.
    WorkflowService._apply_resolved_assignee(item, uid, email, fields)
    if not getattr(item, 'assignee_id', None):
        item.assignment_group = old_group
        return False
    if _canonical_team(old_group) != team and 'assignment_group' not in fields:
        fields.append('assignment_group')
    metadata['recurring_owner_team'] = team
    # PLS-528 (main interplay): ``metadata`` here is cycle_state's COPY —
    # main's _restore_recurring_owner_team reads item.metadata LIVE inside the
    # same fire transaction and would revert the fallback route (and clear the
    # fallback assignee) because the live dict still names the invalid team.
    # Mirror the new owner into the live metadata so the cadence-owner restore
    # sees the effective route.
    if isinstance(getattr(item, 'metadata', None), dict):
        item.metadata['recurring_owner_team'] = team
    return True


def validate_recurring_route(item, metadata: dict, *, now) -> RouteValidation:
    """Validate/repair the template route at the generation transaction.

    The durable declaration is ``metadata.recurring_owner_team`` (PLS-301).
    On an invalid declaration we move both the live lane and that declaration
    to a configured eligible fallback, while retaining the invalid declaration
    in the discrepancy capsule so a roster repair can restore it later.
    """
    routing, fallback = _route_config(item, metadata)
    fields: list[str] = []
    owner = _canonical_team(
        metadata.get('recurring_owner_team')
        or getattr(item, 'assignment_group', '')
    )
    # PLS-528 (main interplay): a recurring template with NO route declaration
    # (no metadata.recurring_owner_team and no assignment_group — main's run-now
    # fixtures and legacy items) has nothing to validate; the fire-time owner
    # derivation owns it. An empty owner is a no-op, never a hold.
    if not owner:
        return RouteValidation()
    previous = routing.get('discrepancy')
    previous = dict(previous) if isinstance(previous, dict) else {}

    # A fallback remains an effective route, not silent template repair. Keep
    # probing the original declaration so a real roster repair clears the
    # condition and restores the intended lane.
    if previous.get('active'):
        effective = _canonical_team(previous.get('effective_group'))
        invalid_group = _canonical_team(previous.get('invalid_group'))
        if routing.get('accept_fallback') and owner == effective:
            _clear_route_discrepancy(
                item, metadata, routing, now=now,
                reason='configured fallback explicitly accepted as the template route',
            )
        elif owner == effective and invalid_group:
            original_state = _team_route_state(item, invalid_group)
            if original_state['eligible']:
                if _apply_route(item, metadata, original_state, fields):
                    _clear_route_discrepancy(
                        item, metadata, routing, now=now,
                        reason='the original template team is staffed and eligible again',
                    )
                    return RouteValidation(fields=tuple(dict.fromkeys(fields)))
                _record_route_discrepancy(
                    item, metadata, routing,
                    invalid_state={**original_state, 'reason': 'identity_unresolvable'},
                    fallback_state=None, outcome='hold', now=now,
                )
                _hold_route(item, fields)
                return RouteValidation(
                    held=True, reason='identity_unresolvable',
                    fields=tuple(dict.fromkeys(fields)),
                )

            effective_state = _team_route_state(item, effective)
            if effective_state['eligible'] and _apply_route(
                    item, metadata, effective_state, fields):
                _record_route_discrepancy(
                    item, metadata, routing, invalid_state=original_state,
                    fallback_state=effective_state, outcome='fallback', now=now,
                )
                return RouteValidation(fields=tuple(dict.fromkeys(fields)))
            _record_route_discrepancy(
                item, metadata, routing, invalid_state=original_state,
                fallback_state=effective_state, outcome='hold', now=now,
            )
            _hold_route(item, fields)
            return RouteValidation(
                held=True,
                reason=effective_state['reason'] or 'fallback_identity_unresolvable',
                fields=tuple(dict.fromkeys(fields)),
            )
        elif previous.get('outcome') == 'hold' and owner == invalid_group:
            # A held occurrence has no effective fallback group. Revalidate the
            # same declared group without treating the unchanged declaration as
            # a template edit; otherwise every retry clears/re-arms the alert.
            declared_state = _team_route_state(item, owner)
            if declared_state['eligible']:
                if _apply_route(item, metadata, declared_state, fields):
                    _clear_route_discrepancy(
                        item, metadata, routing, now=now,
                        reason='the held template team is staffed and eligible again',
                    )
                    return RouteValidation(fields=tuple(dict.fromkeys(fields)))
                declared_state = {
                    **declared_state,
                    'reason': 'identity_unresolvable',
                }
            fallback_state = _team_route_state(item, fallback) if fallback else None
            if (
                fallback_state
                and fallback != declared_state['team']
                and fallback_state['eligible']
                and _apply_route(item, metadata, fallback_state, fields)
            ):
                _record_route_discrepancy(
                    item, metadata, routing, invalid_state=declared_state,
                    fallback_state=fallback_state, outcome='fallback', now=now,
                )
                return RouteValidation(fields=tuple(dict.fromkeys(fields)))
            _record_route_discrepancy(
                item, metadata, routing, invalid_state=declared_state,
                fallback_state=fallback_state, outcome='hold', now=now,
            )
            _hold_route(item, fields)
            return RouteValidation(
                held=True,
                reason=(fallback_state or {}).get('reason') or declared_state['reason'],
                fields=tuple(dict.fromkeys(fields)),
            )
        else:
            _clear_route_discrepancy(
                item, metadata, routing, now=now,
                reason='template routing declaration changed',
            )

    declared_state = _team_route_state(item, owner)
    if declared_state['eligible']:
        if _apply_route(item, metadata, declared_state, fields):
            return RouteValidation(fields=tuple(dict.fromkeys(fields)))
        # PLS-528 (main interplay): the declared route is VALID (staffed,
        # active, capable roster) — only the concrete assignee failed to
        # resolve. Main's assignment-retry/andon block inside this same fire
        # transaction owns the unassigned-item cadence (bounded retries, then
        # the andon); a routing hold here would freeze that machinery out.
        return RouteValidation(fields=tuple(dict.fromkeys(fields)))

    fallback_state = _team_route_state(item, fallback) if fallback else None
    if (
        fallback_state
        and fallback != declared_state['team']
        and fallback_state['eligible']
        and _apply_route(item, metadata, fallback_state, fields)
    ):
        _record_route_discrepancy(
            item, metadata, routing, invalid_state=declared_state,
            fallback_state=fallback_state, outcome='fallback', now=now,
        )
        return RouteValidation(fields=tuple(dict.fromkeys(fields)))

    _record_route_discrepancy(
        item, metadata, routing, invalid_state=declared_state,
        fallback_state=fallback_state, outcome='hold', now=now,
    )
    _hold_route(item, fields)
    return RouteValidation(
        held=True,
        reason=(fallback_state or {}).get('reason') or declared_state['reason'],
        fields=tuple(dict.fromkeys(fields)),
    )


def _integer(value, default=0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _iso(value) -> Optional[str]:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _same_boundary(left, right) -> bool:
    """Compare ISO/datetime boundaries without losing sub-second precision."""
    if left in (None, '') or right in (None, ''):
        return left in (None, '') and right in (None, '')
    try:
        ldt = left if isinstance(left, datetime) else datetime.fromisoformat(str(left).replace('Z', '+00:00'))
        rdt = right if isinstance(right, datetime) else datetime.fromisoformat(str(right).replace('Z', '+00:00'))
        return ldt == rdt
    except (TypeError, ValueError):
        return str(left) == str(right)


def _as_aware_dt(value) -> Optional[datetime]:
    """Normalize an ISO-string or datetime to an aware UTC datetime, else None."""
    if value is None or value == '':
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.utc)
    return value


def _trigger_in_current_window(cycle: dict, trigger_at) -> bool:
    """PLS-855 generation-bound completion identity.

    A linked deliverable may only close the CURRENT generation when it belongs to
    that generation. The current generation's period is delimited by
    ``(consumed_boundary, next_boundary]`` — ``consumed_boundary`` is the due
    boundary whose timer opened the active period, ``next_boundary`` the upcoming
    due. A trigger dated at-or-before ``consumed_boundary`` was directed by a
    PRIOR generation and must never be credited to the current cycle (no advancing
    ``completed_generation``, no resetting ``consecutive_skips``/``escalated_at_skips``,
    no parking the active generation).

    Legacy-safe: rows that never fired carry no ``consumed_boundary``; without a
    left boundary there is no way to prove the trigger predates the current period,
    so we stay permissive to preserve pre-PLS-855 behavior. A ``None`` trigger (a
    completion with no date binding, e.g. a manual park-as-scheduled) is likewise
    treated as current without a window test.
    """
    if trigger_at is None:
        return True
    trigger_dt = _as_aware_dt(trigger_at)
    if trigger_dt is None:
        return True
    consumed = cycle.get('consumed_boundary')
    if not consumed:
        return True
    consumed_dt = _as_aware_dt(consumed)
    if consumed_dt is None:
        return True
    return trigger_dt > consumed_dt


def cycle_state(item) -> tuple[dict, dict]:
    """Return backwards-compatible metadata/state copies for ``item``."""
    metadata = dict(item.metadata or {})
    raw = metadata.get('recurring_cycle')
    cycle = dict(raw) if isinstance(raw, dict) else {}
    active = _integer(cycle.get('active_generation'))
    completed = min(active, _integer(cycle.get('completed_generation')))
    cycle.update({
        'cycle_revision': _integer(cycle.get('cycle_revision')),
        'active_generation': active,
        'completed_generation': completed,
        'consecutive_skips': _integer(cycle.get('consecutive_skips')),
        'escalated_at_skips': _integer(cycle.get('escalated_at_skips')),
    })
    cycle.setdefault('last_terminal_completion_at', None)
    cycle.setdefault('consumed_boundary', None)
    cycle.setdefault('next_boundary', _iso(getattr(item, 'due_date', None)))
    if not cycle.get('last_event_id'):
        # Stable token for legacy rows.  It lets the first request carry an
        # opaque state identity without a data migration or write-on-read.
        basis = '|'.join([
            str(getattr(item, 'pk', '')),
            str(cycle['cycle_revision']),
            str(cycle.get('next_boundary') or ''),
            str(cycle.get('last_terminal_completion_at') or ''),
        ])
        cycle['last_event_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'pulse-recurring:{basis}'))
    return metadata, cycle


def cycle_snapshot(item) -> dict:
    """Machine-readable freshness payload for notifications and clients."""
    _metadata, cycle = cycle_state(item)
    return {
        'event_id': cycle.get('last_event_id'),
        'generation': cycle['active_generation'],
        'cycle_revision': cycle['cycle_revision'],
        'due_boundary': _iso(getattr(item, 'due_date', None)),
        'status': getattr(item, 'status', None),
        'assignee': getattr(item, 'assignee_email', None) or getattr(item, 'assignee_id', None),
        'completion_watermark': cycle.get('last_terminal_completion_at'),
    }


def notification_state_line(item) -> str:
    return 'recurring_event=' + json.dumps(cycle_snapshot(item), sort_keys=True, separators=(',', ':'))


def _record_event(item, cycle: dict, *, kind: str, now, event_id: str | None = None) -> str:
    event_id = str(event_id or uuid.uuid4())
    cycle['last_event_id'] = event_id
    cycle['last_event'] = {
        'event_id': event_id,
        'kind': kind,
        'cycle_revision': cycle['cycle_revision'],
        'generation': cycle['active_generation'],
        'due_boundary': _iso(getattr(item, 'due_date', None)),
        'status': getattr(item, 'status', None),
        'assignee': getattr(item, 'assignee_email', None) or getattr(item, 'assignee_id', None),
        'completion_watermark': cycle.get('last_terminal_completion_at'),
        'at': now.isoformat(),
    }
    return event_id


def _write_state(item, metadata: dict, cycle: dict) -> None:
    metadata['recurring_cycle'] = cycle
    item.metadata = metadata


def _status_category(slug: str) -> Optional[str]:
    if not slug:
        return None
    from tasks.models import Status
    return Status.objects.filter(slug=slug).values_list('category', flat=True).first()


def _is_ready_to_start(item) -> bool:
    slug = (item.status or '').strip().lower()
    return slug in {'done', 'completed', 'closed', 'resolved', 'scheduled'} or _status_category(slug) in {'done', 'scheduled'}


def _sync_workflow_status(item, slug: str) -> None:
    from tasks.models import Status
    status = Status.objects.filter(slug=slug).first()
    if status:
        item.workflow_status = status.name


def _initial_status(item, default='pending') -> str:
    from tasks.tasks import _workflow_initial_status_slug

    return _workflow_initial_status_slug(item, default=default)


def _next_due(item, now, *, preserve_future: bool) -> Optional[datetime]:
    current = item.due_date or now
    if preserve_future and current > now:
        return current
    computed = calculate_next_future_due_date(
        current, item.frequency, now, item.recurrence_pattern,
    )
    if computed is not None and computed > now:
        return computed
    # PLAT-10627: boundary escape hatch. A recurring fire must ALWAYS advance
    # past `now`. With an unparseable frequency/pattern (computed=None) or a
    # non-advancing interval, the pre-fix behavior handed the fire path either
    # None ('no_next_due' no-op) or a boundary ≤ now — both re-consume the
    # same stale boundary forever while the engine self-reports healthy
    # (observed live: 13 fleet cadences frozen between 09-14 and 09-20).
    # Fall back to the parsed fixed interval, or a conservative daily period
    # for calendar/unparseable shapes, so the visible boundary advances and
    # the overdue spin terminates. The cadence owner can then correct the
    # configuration through the normal fire/visibility channels.
    fallback_seconds = parse_interval_seconds(
        item.frequency, item.recurrence_pattern,
    )
    if not fallback_seconds or fallback_seconds <= 0:
        fallback_seconds = 86400
    logger.warning(
        'recurring boundary fallback engaged for item %s (frequency=%r): '
        'computed=%r, advancing to now+%ss',
        getattr(item, 'item_key', getattr(item, 'pk', '?')),
        item.frequency, computed, fallback_seconds,
    )
    return now + timedelta(seconds=fallback_seconds)


def _expected_mismatch(item, cycle, *, revision=None, generation=None, due_at=None, watermark=None) -> Optional[str]:
    if revision is not None and _integer(revision, -1) != cycle['cycle_revision']:
        return 'cycle_revision'
    if generation is not None and _integer(generation, -1) != cycle['active_generation']:
        return 'generation'
    if due_at is not None and not _same_boundary(due_at, item.due_date):
        return 'due_boundary'
    if watermark is not None and str(watermark or '') != str(cycle.get('last_terminal_completion_at') or ''):
        return 'completion_watermark'
    return None


def _actor_email(user) -> str:
    return str(getattr(user, 'email', None) or getattr(user, 'username', None) or '')


def _is_assignee(item, user) -> bool:
    return bool(
        user and (
            getattr(user, 'id', None) == item.assignee_id
            or (_actor_email(user) and _actor_email(user).lower() == (item.assignee_email or '').lower())
        )
    )


def _has_override_authority(item, user) -> bool:
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    project = getattr(item, 'project', None)
    if project is None:
        return False
    try:
        from tasks.services.permissions import PermissionService
        return PermissionService.can_access_project(
            getattr(user, 'id', None),
            project,
            permission='manage_project',
            user_email=_actor_email(user),
            user=user,
        )
    except Exception:
        return False


def _truthy(value) -> bool:
    return value is True or str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _manual_request_key(expected_revision, actor_email, request_event_id=None) -> str:
    event = f'event:{request_event_id}' if request_event_id else f'revision:{expected_revision}'
    return f'{event}:actor:{actor_email.lower()}'


def manual_start_cycle(item_id, *, user, expected_cycle_revision, expected_generation=None,
                       expected_due_at=None, expected_completion_watermark=None,
                       request_event_id=None, target_status=None, authorized_override=False,
                       reason='') -> CycleResult:
    """Start one generation early, with CAS, authority, and replay protection."""
    if expected_cycle_revision in (None, ''):
        raise RecurringCycleError(
            'expected_cycle_revision_required',
            'Run Now requires expected_cycle_revision from the current recurring_event state.',
        )

    from tasks.models import Item

    with transaction.atomic():
        item = Item.objects.select_for_update().get(pk=item_id, mode='task')
        if not item.is_recurring or not item.frequency or item.frequency == 'one_time':
            raise RecurringCycleError('not_recurring', 'Task is not recurring.')
        metadata, cycle = cycle_state(item)
        current_snapshot = cycle_snapshot(item)
        actor_email = _actor_email(user)
        request_key = _manual_request_key(expected_cycle_revision, actor_email, request_event_id)
        last_request = cycle.get('last_manual_start_request') or {}
        if last_request.get('key') == request_key:
            return CycleResult(
                item=item, applied=False, replay=True, reason='idempotent_replay',
                kind='manual_start', new_status=item.status, next_due=item.due_date,
                revision=cycle['cycle_revision'], generation=cycle['active_generation'],
                event_id=cycle.get('last_event_id') or '',
            )

        mismatch = _expected_mismatch(
            item, cycle,
            revision=expected_cycle_revision,
            generation=expected_generation,
            due_at=expected_due_at,
            watermark=expected_completion_watermark,
        )
        if mismatch:
            raise RecurringCycleError(
                'stale_recurring_cycle',
                f'Run Now state is stale ({mismatch} changed); refresh the task before retrying. '
                f'Current cycle: {json.dumps(current_snapshot, sort_keys=True, separators=(",", ":"))}',
                current=current_snapshot,
            )
        if not _is_ready_to_start(item):
            raise RecurringCycleError(
                'cycle_not_scheduled',
                f'Run Now is only available while Scheduled or Done (currently {item.status!r}).',
            )
        if not _is_assignee(item, user):
            if not (_truthy(authorized_override) and str(reason or '').strip() and _has_override_authority(item, user)):
                raise RecurringCycleError(
                    'run_now_forbidden',
                    'Run Now is assignee-only. An owner/admin override must include authorized_override=true and a reason.',
                )

        now = timezone.now()
        route = validate_recurring_route(item, metadata, now=now)
        if route.held:
            _write_state(item, metadata, cycle)
            item._activity_actor = (
                getattr(user, 'id', None) or 0,
                actor_email or 'scheduler@pulse.shizuha.com',
            )
            item.save(update_fields=list(dict.fromkeys([
                *route.fields, 'metadata', 'updated_at',
            ])))
            return CycleResult(
                item=item, applied=False, reason=route.reason,
                kind='routing_hold', old_status=item.status,
                new_status=item.status, next_due=item.due_date,
                revision=cycle['cycle_revision'],
                generation=cycle['active_generation'],
                event_id=cycle.get('last_event_id') or '',
            )
        next_due = _next_due(item, now, preserve_future=False)
        if next_due is None:
            raise RecurringCycleError('no_next_due', 'Could not compute the next recurring boundary.')

        old_status = item.status
        # Direct legacy terminal writes may bypass the normal park hook.  Keep a
        # conservative completion watermark before opening the next generation.
        if _status_category(old_status) == 'done' and not cycle.get('last_terminal_completion_at'):
            cycle['last_terminal_completion_at'] = now.isoformat()
            cycle['completed_generation'] = cycle['active_generation']
        cycle['consumed_boundary'] = _iso(item.due_date)
        cycle['active_generation'] += 1
        cycle['cycle_revision'] += 1
        cycle['next_boundary'] = next_due.isoformat()
        cycle['last_manual_start_request'] = {
            'key': request_key,
            'expected_cycle_revision': _integer(expected_cycle_revision),
            'actor': actor_email,
            'reason': str(reason or '')[:500],
            'override': not _is_assignee(item, user),
            'at': now.isoformat(),
        }

        item.status = target_status or _initial_status(item)
        _sync_workflow_status(item, item.status)
        item.due_date = next_due
        item.overdue_notified = False
        item.reminder_sent = False
        item.completed_at = None
        event_id = _record_event(item, cycle, kind='manual_start', now=now)
        _write_state(item, metadata, cycle)
        item._activity_actor = (getattr(user, 'id', None) or 0, actor_email or 'scheduler@pulse.shizuha.com')
        item._activity_status_action = 'recurring_cycle_fired_manual'
        item._activity_suppress_fields = ('due_date', 'metadata')
        item.save(update_fields=list(dict.fromkeys([
            'status', 'workflow_status', 'due_date', 'overdue_notified',
            'reminder_sent', 'completed_at', 'metadata', 'updated_at',
            *route.fields,
        ])))
        return CycleResult(
            item=item, applied=True, reason='started', kind='manual_start',
            old_status=old_status, new_status=item.status, next_due=next_due,
            revision=cycle['cycle_revision'], generation=cycle['active_generation'],
            event_id=event_id,
        )


def complete_cycle(item_id, *, actor_id=0, actor_email='scheduler@pulse.shizuha.com',
                   trigger_key=None, expected_cycle_revision=None,
                   expected_generation=None, expected_due_at=None,
                   expected_completion_watermark=None, trigger_at=None) -> CycleResult:
    """Idempotently park a completed cycle and preserve a future boundary."""
    from tasks.models import Item
    from tasks.services.workflow_service import WorkflowService

    with transaction.atomic():
        item = Item.objects.select_for_update().get(pk=item_id, mode='task')
        if not item.is_recurring or not item.frequency or item.frequency == 'one_time':
            return CycleResult(item, False, 'not_recurring')
        if (item.status or '').strip().lower() in STOP_STATUSES:
            return CycleResult(item, False, 'stop_status')
        metadata, cycle = cycle_state(item)
        mismatch = _expected_mismatch(
            item, cycle,
            revision=expected_cycle_revision,
            generation=expected_generation,
            due_at=expected_due_at,
            watermark=expected_completion_watermark,
        )
        if mismatch:
            return CycleResult(
                item, False, f'stale_{mismatch}', kind='stale',
                next_due=item.due_date, revision=cycle['cycle_revision'],
                generation=cycle['active_generation'], event_id=cycle.get('last_event_id') or '',
            )
        triggers = list(cycle.get('auto_closed_triggers') or [])
        if trigger_key and trigger_key in triggers:
            return CycleResult(item, False, 'already_closed_for_trigger', next_due=item.due_date, replay=True)
        if (item.status or '').strip().lower() == 'scheduled' and cycle['completed_generation'] >= cycle['active_generation']:
            return CycleResult(item, False, 'already_in_gap_or_terminal', next_due=item.due_date, replay=True)
        # PLS-855 generation-bound completion identity: a prior-generation
        # deliverable must never close the current cycle. Evidence dated before
        # the current generation's window (consumed_boundary] is a no-op — it
        # must not advance completed_generation, reset skip/escalation counters,
        # or park the active generation. Legacy rows without a window accept
        # (permissive), as do completions without a date binding (trigger_at).
        if trigger_at is not None and not _trigger_in_current_window(cycle, trigger_at):
            return CycleResult(
                item, False, 'stale_evidence_prior_generation', kind='stale',
                next_due=item.due_date, revision=cycle['cycle_revision'],
                generation=cycle['active_generation'], event_id=cycle.get('last_event_id') or '',
            )

        now = timezone.now()
        next_due = _next_due(item, now, preserve_future=True)
        if next_due is None:
            return CycleResult(item, False, 'no_next_due')
        WorkflowService._ensure_scheduled_gap_state_for_workflow(item)
        old_status = item.status
        item.status = 'scheduled'
        _sync_workflow_status(item, 'scheduled')
        item.due_date = next_due
        item.percent_complete = 0
        item.resolution = None
        item.resolution_date = None
        item.overdue_notified = False
        item.reminder_sent = False
        item.completed_at = None

        cycle['completed_generation'] = cycle['active_generation']
        cycle['consecutive_skips'] = 0
        cycle['escalated_at_skips'] = 0
        cycle['last_terminal_completion_at'] = now.isoformat()
        cycle['next_boundary'] = next_due.isoformat()
        cycle['cycle_revision'] += 1
        if trigger_key:
            if trigger_key not in triggers:
                triggers.append(trigger_key)
            cycle['auto_closed_triggers'] = triggers[-20:]
            cycle['last_auto_closed_at'] = now.isoformat()
            cycle['last_auto_closed_trigger'] = trigger_key
        event_id = _record_event(item, cycle, kind='terminal_completion', now=now)
        _write_state(item, metadata, cycle)

        item._activity_actor = (actor_id or 0, actor_email or 'scheduler@pulse.shizuha.com')
        item._activity_status_action = 'recurring_parked_scheduled'
        item._activity_suppress_fields = ('due_date', 'metadata')
        item.save(update_fields=[
            'status', 'workflow_status', 'due_date', 'percent_complete',
            'resolution', 'resolution_date', 'overdue_notified',
            'reminder_sent', 'completed_at', 'metadata', 'updated_at',
        ])
        return CycleResult(
            item=item, applied=True, reason='completed', kind='terminal_completion',
            old_status=old_status, new_status='scheduled', next_due=next_due,
            revision=cycle['cycle_revision'], generation=cycle['active_generation'],
            event_id=event_id,
        )


def fire_due_cycle(item_id, *, expected_cycle_revision=None, expected_due_at=None,
                   expected_generation=None, expected_completion_watermark=None) -> CycleResult:
    """Consume one due timer under the item-row CAS boundary."""
    from tasks.models import Comment, Item, ItemActivity
    from tasks.services.workflow_service import WorkflowService

    with transaction.atomic():
        item = Item.objects.select_for_update().get(pk=item_id, mode='task')
        if not item.is_recurring or not item.frequency or item.frequency == 'one_time':
            return CycleResult(item, False, 'not_recurring')
        if (item.status or '').strip().lower() in STOP_STATUSES:
            return CycleResult(item, False, 'stopped')
        metadata, cycle = cycle_state(item)
        mismatch = _expected_mismatch(
            item, cycle,
            revision=expected_cycle_revision,
            generation=expected_generation,
            due_at=expected_due_at,
            watermark=expected_completion_watermark,
        )
        if mismatch:
            return CycleResult(
                item, False, f'stale_{mismatch}', kind='stale',
                next_due=item.due_date, revision=cycle['cycle_revision'],
                generation=cycle['active_generation'], event_id=cycle.get('last_event_id') or '',
            )

        now = timezone.now()
        if item.due_date and item.due_date > now + timedelta(minutes=5):
            return CycleResult(
                item, False, 'stale_future_due', kind='stale', next_due=item.due_date,
                revision=cycle['cycle_revision'], generation=cycle['active_generation'],
                event_id=cycle.get('last_event_id') or '',
            )

        # PLS-528: validate the durable recurring-template route at generation
        # — the validation boundary observes deleted/disabled/emptied teams on
        # the very next cycle and holds the fire instead of routing into a
        # ghost team (grafted ahead of the cadence-owner restore, which owns
        # the concurrent route/unroute correctness boundary on main).
        route = validate_recurring_route(item, metadata, now=now)
        if route.held:
            _write_state(item, metadata, cycle)
            item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
            item.save(update_fields=list(dict.fromkeys([
                *route.fields, 'metadata', 'updated_at',
            ])))
            return CycleResult(
                item=item, applied=False, reason=route.reason,
                kind='routing_hold', old_status=item.status,
                new_status=item.status, next_due=item.due_date,
                revision=cycle['cycle_revision'],
                generation=cycle['active_generation'],
                event_id=cycle.get('last_event_id') or '',
            )

        # Restore the stable cadence owner inside the same row lock that
        # consumes the timer.  The outer task-level repair is only a fast path;
        # this is the correctness boundary for a concurrent route/unroute.
        assignment_fields = []
        WorkflowService._restore_recurring_owner_team(
            item, fields=assignment_fields, save=False,
        )
        if not item.assignee_id:
            consumed_due = item.due_date
            owner_team = WorkflowService._canonical_team_name(
                WorkflowService._recurring_owner_team(item)
                or item.assignment_group
                or WorkflowService._reporter_group(item)
                or ''
            )
            if owner_team and item.assignment_group != owner_team:
                item.assignment_group = owner_team
                assignment_fields.append('assignment_group')

            attempts = _integer(cycle.get('assignment_retry_attempts')) + 1
            first_exhaustion = (
                attempts >= ASSIGNMENT_RETRY_LIMIT
                and not cycle.get('assignment_andon_at')
            )
            natural_due = _next_due(item, now, preserve_future=False)
            if natural_due is None:
                return CycleResult(item, False, 'no_next_due')
            if attempts < ASSIGNMENT_RETRY_LIMIT:
                next_due = min(natural_due, now + ASSIGNMENT_RETRY_DELAY)
                reason = 'assignment_retry'
                kind = 'assignment_retry'
            else:
                next_due = natural_due
                reason = 'assignment_andon'
                kind = 'assignment_andon'

            WorkflowService._ensure_scheduled_gap_state_for_workflow(item)
            old_status = item.status
            item.status = 'scheduled'
            _sync_workflow_status(item, 'scheduled')
            item.due_date = next_due
            cycle['assignment_retry_attempts'] = attempts
            cycle['assignment_wait_team'] = owner_team
            cycle['assignment_wait_since'] = (
                cycle.get('assignment_wait_since') or now.isoformat()
            )
            cycle['consumed_boundary'] = _iso(consumed_due)
            cycle['next_boundary'] = next_due.isoformat()
            cycle['cycle_revision'] += 1
            if first_exhaustion:
                cycle['assignment_andon_at'] = now.isoformat()
                cycle['assignment_andon_attempts'] = attempts
            event_id = _record_event(item, cycle, kind=kind, now=now)
            _write_state(item, metadata, cycle)
            fields = [
                'status', 'workflow_status', 'due_date', 'metadata', 'updated_at',
                *assignment_fields,
            ]
            item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
            item._activity_status_action = kind
            item._activity_suppress_fields = ('due_date', 'metadata')
            item.save(update_fields=list(dict.fromkeys(fields)))
            if first_exhaustion:
                from tasks.services.mechanical_comments import MechanicalCommentFactory
                MechanicalCommentFactory.create(
                    item=item,
                    origin_kind=Comment.ORIGIN_RESOLVER_AUDIT,
                    origin_event='resolver.recurring_assignment_andon',
                    template_version=1,
                    structured_payload={
                        'team': str(owner_team or ''),
                        'attempts': int(attempts),
                    },
                    origin_event_id=f'recurring-cycle:{event_id}:assignment_andon',
                )
                ItemActivity.objects.create(
                    item=item, action='recurring_assignment_andon',
                    field_name='recurring_cycle', old_value=str(attempts - 1),
                    new_value=str(attempts), user_id=0,
                    user_email='scheduler@pulse.shizuha.com',
                )
            return CycleResult(
                item, True, reason, kind=kind, old_status=old_status,
                new_status='scheduled', next_due=next_due,
                revision=cycle['cycle_revision'],
                generation=cycle['active_generation'], skip_count=attempts,
                notify_team=owner_team if first_exhaustion else '',
                event_id=event_id,
            )

        next_due = _next_due(item, now, preserve_future=False)
        if next_due is None:
            return CycleResult(item, False, 'no_next_due')

        old_status = item.status
        if _is_ready_to_start(item):
            cycle['assignment_retry_attempts'] = 0
            cycle['assignment_wait_team'] = ''
            cycle['assignment_wait_since'] = None
            cycle['assignment_andon_at'] = None
            cycle['assignment_andon_attempts'] = 0
            if _status_category(old_status) == 'done' and (
                cycle['completed_generation'] < cycle['active_generation']
                or not cycle.get('last_terminal_completion_at')
            ):
                cycle['completed_generation'] = cycle['active_generation']
                cycle['last_terminal_completion_at'] = now.isoformat()
                cycle['consecutive_skips'] = 0
                cycle['escalated_at_skips'] = 0
            cycle['consumed_boundary'] = _iso(item.due_date)
            cycle['active_generation'] += 1
            cycle['cycle_revision'] += 1
            cycle['next_boundary'] = next_due.isoformat()
            item.status = _initial_status(item)
            _sync_workflow_status(item, item.status)
            item.due_date = next_due
            item.overdue_notified = False
            item.reminder_sent = False
            item.completed_at = None
            event_id = _record_event(item, cycle, kind='scheduled_start', now=now)
            _write_state(item, metadata, cycle)
            # PLS-1286: lane liveness on every fire — the meta-watchdog's watermark.
            record_lane_liveness(kind='scheduled_start', item=item, now=now)
            item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
            item._activity_status_action = 'recurring_cycle_fired'
            item._activity_suppress_fields = ('due_date', 'metadata')
            item.save(update_fields=list(dict.fromkeys([
                'status', 'workflow_status', 'due_date', 'overdue_notified',
                'reminder_sent', 'completed_at', 'metadata', 'updated_at',
                *assignment_fields,
                *route.fields,
            ])))
            return CycleResult(
                item, True, 'started', kind='scheduled_start', old_status=old_status,
                new_status=item.status, next_due=next_due, revision=cycle['cycle_revision'],
                generation=cycle['active_generation'], event_id=event_id,
            )

        old_count = cycle['consecutive_skips']
        lifted = False
        if (item.status or '').strip().lower() == 'backlog':
            # PLS-1291: a fire that LIFTS the item (Backlog→open) is the
            # cadence WORKING, not a missed period. Run-close hygiene re-parks
            # healthy recurring items to Backlog after each run and the next
            # fire lifts them — counting those as skips accrued false
            # escalations with assignee bounces against actively-run cadences
            # (PLAT-528 item 2014: sora bounced at skips=3, kumo at skips=6
            # while nova/hiro/kumo ran the cadence throughout). Reset the
            # counters exactly as the ready-to-start branch does; the owner
            # signal for this fire is the lift notification below.
            cycle['consecutive_skips'] = 0
            cycle['escalated_at_skips'] = 0
            count = 0
            cycle['consumed_boundary'] = _iso(item.due_date)
            cycle['next_boundary'] = next_due.isoformat()
            cycle['cycle_revision'] += 1
            item.due_date = next_due
            item.status = _initial_status(item, default='open')
            _sync_workflow_status(item, item.status)
            lifted = True
        else:
            # Genuine skip: the item is not ready to start and no lift
            # occurred (active-but-blocked, or parked per PLAT-8821 — the
            # escalation bounce below stays suppressed for parked items).
            count = old_count + 1
            cycle['consecutive_skips'] = count
            cycle['last_skip_at'] = now.isoformat()
            cycle['consumed_boundary'] = _iso(item.due_date)
            cycle['next_boundary'] = next_due.isoformat()
            cycle['cycle_revision'] += 1
            item.due_date = next_due
        # PLS-1286: lane liveness on every fire — the meta-watchdog's watermark.
        record_lane_liveness(
            kind='backlog_lift' if lifted else 'active_skip', item=item, now=now,
        )
        if not lifted and count == 1:
            # PLS-1286: owner-visible signal at the FIRST missed fire (the
            # escalation lane only fires at 3/6/9; PLAT-528 went 6 periods
            # dark before any push signal reached the assignee).
            try:
                from tasks.services.mechanical_comments import MechanicalCommentFactory
                MechanicalCommentFactory.create(
                    item=item,
                    origin_kind=Comment.ORIGIN_RESOLVER_AUDIT,
                    origin_event='recurring.missed_fire_period1',
                    template_version=1,
                    structured_payload={'boundary': next_due.isoformat()},
                    origin_event_id=(
                        f'recurring-cycle:{item.id}:missed_fire_p1:'
                        f'{next_due.isoformat()}'
                    ),
                )
            except Exception:  # pragma: no cover - fail-soft per PLAT-1254
                pass
            _notify_assignee_missed_fire(item, count=count, boundary=next_due)

        notify_team = ''
        assignment_fields = []
        if count >= 3 and count % 3 == 0 and cycle['escalated_at_skips'] < count:
            # PLAT-8821: a parked item (backlog/deferred, or carrying a recent
            # operator/lead park disposition) must never have its assignee
            # bounced by the missed-period escalation — the CON-389
            # anti-pattern (5 failover hops in one day against an explicit
            # operator park directive). Record the suppression; the park
            # stands until a human/lead lifts it.
            from tasks.services.workflow_service import WorkflowService as _WS
            if (
                (item.status or '').strip().lower() in _WS.PARKED_WORKFLOW_STATUSES
                or _WS._item_park_dispositioned(item)
            ):
                cycle['escalated_at_skips'] = count
                Comment.objects.create(
                    item=item, author_id=0, author_email='system@pulse',
                    content=(
                        "[recurring-cycle escalation] Suppressed: item is parked "
                        f"(status={item.status or 'unknown'} or operator/lead park "
                        "disposition) — no assignee bounce, no notification churn. "
                        "Lift the park to resume escalation. [PLS-214/PLAT-8821]"
                    ),
                )
                event_id = _record_event(item, cycle, kind='active_skip', now=now)
                _write_state(item, metadata, cycle)
                fields = ['due_date', 'metadata', 'updated_at']
                item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
                item._activity_suppress_fields = ('due_date', 'metadata')
                item.save(update_fields=list(dict.fromkeys(fields)))
                return CycleResult(
                    item, True, 'skipped', kind='active_skip', old_status=old_status,
                    new_status=item.status, next_due=next_due, revision=cycle['cycle_revision'],
                    generation=cycle['active_generation'], skip_count=count, lifted=False,
                    notify_team=None, event_id=event_id,
                )
            notify_team = WorkflowService._canonical_team_name(
                item.assignment_group or WorkflowService._reporter_group(item) or ''
            )
            resolved_email = None
            if notify_team:
                try:
                    uid, resolved_email = WorkflowService._resolve_team_assignee(
                        notify_team, exclude_email=item.assignee_email, enforce_cap=False,
                        project_key=getattr(getattr(item, 'project', None), 'key', None),
                        item=item,
                    )
                    if resolved_email:
                        WorkflowService._apply_resolved_assignee(
                            item, uid, resolved_email, assignment_fields,
                        )
                except Exception:
                    resolved_email = None
            cycle['escalated_at_skips'] = count
            Comment.objects.create(
                item=item, author_id=0, author_email='system@pulse',
                content=(f"[recurring-cycle escalation] Missed {count} consecutive periods. "
                         f"Team={notify_team or 'unknown'}; bounced assignee to "
                         f"{resolved_email or 'no resolvable alternate'}. [PLS-214]"),
            )

        event_id = _record_event(item, cycle, kind='active_skip', now=now)
        _write_state(item, metadata, cycle)
        fields = ['due_date', 'metadata', 'updated_at']
        fields.extend(route.fields)
        if lifted:
            fields.extend(['status', 'workflow_status'])
        fields.extend(assignment_fields)
        item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
        item._activity_suppress_fields = ('due_date', 'metadata')
        item.save(update_fields=list(dict.fromkeys(fields)))
        if lifted:
            ItemActivity.objects.create(
                item=item, action='recurring_cycle_fired', field_name='status',
                old_value=old_status, new_value=item.status, user_id=0,
                user_email='scheduler@pulse.shizuha.com',
            )
            Comment.objects.create(
                item=item, author_id=0, author_email='system@pulse',
                content=(f"[recurring-cycle] Backlog recurring item auto-lifted to **{item.status}** "
                         "at its fire time; use Deferred to intentionally stop cadence. [PLS-214]"),
            )
            # PLS-1286: the lift is an owner-visible state change — push it,
            # don't rely on the assignee noticing the Comment.
            if getattr(item, 'assignee_id', None):
                try:
                    from tasks.services.notifications import NotificationService
                    NotificationService().notify_user(
                        item.assignee_id,
                        f"{item.item_key or f'#{item.id}'}: recurring item auto-lifted to {item.status}",
                        (
                            f"<b>{item.title}</b><br>"
                            f"The recurring cadence fired and the item was auto-lifted "
                            f"from Backlog to <b>{item.status}</b> at its fire time. "
                            f"[PLS-1286]"
                        ),
                        event_type='action',
                        severity='info',
                        item=item,
                    )
                except Exception:  # pragma: no cover - fail-soft per PLAT-1254
                    pass
        if count == 2:
            ItemActivity.objects.create(
                item=item, action='recurring_cycle_stale', field_name='recurring_cycle',
                old_value=str(old_count), new_value=str(count), user_id=0,
                user_email='scheduler@pulse.shizuha.com',
            )
            Comment.objects.create(
                item=item, author_id=0, author_email='system@pulse',
                content='[recurring-cycle] recurring cycle missed 2 consecutive periods — stale. [PLS-214]',
            )
        return CycleResult(
            item, True, 'skipped', kind='active_skip', old_status=old_status,
            new_status=item.status, next_due=next_due, revision=cycle['cycle_revision'],
            generation=cycle['active_generation'], skip_count=count, lifted=lifted,
            notify_team=notify_team, event_id=event_id,
        )
