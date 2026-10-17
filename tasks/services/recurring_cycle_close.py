"""PLAT-1353 recurring-cycle close helpers.

A recurring parent that produced an explicit cycle-completion marker should not
stay active after its linked deliverable PR/task lands. This module centralizes
the event hook used by PR webhooks, PR-status reconciliation, and linked-task
terminal transitions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tasks.services.recurring import (
    calculate_next_future_due_date,
    parse_interval_seconds,
)


logger = logging.getLogger(__name__)


# Explicit cycle-completion evidence accepted on the recurring parent. This is
# intentionally narrow: a PR/task merging alone is not enough; the actor must say
# the current cycle is complete.
#
# PLS-1384: prose matches are additionally gated by
# _affirmative_completion_matches() — negated ("PARTIAL, not cycle complete"),
# quoted (inside code fences/backticks), and example-context ("e.g. cycle
# complete") mentions are rejected. Only explicit affirmative, current-cycle
# completion intent may drive automatic state writes. The structured marker
# "[recurring-cycle complete]" is exempt from the prose guards (it is an
# explicit action, not fuzzy prose) and is the strongly preferred trigger.
_COMPLETION_EVIDENCE_RE = re.compile(
    r"(\[recurring-cycle complete\]|\bcurrent\s+cycle\s+complete\b|\bcycle\s+[^\n]{0,80}?\bcomplete\b)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:not|never|cannot|can't|isn't|aren't|wasn't|hasn't|haven't|"
    r"pending|partial|awaiting|yet\s+to)\b",
    re.IGNORECASE,
)
_EXAMPLE_CONTEXT_RE = re.compile(
    r"(?:\b(?:for\s+example|such\s+as)\b|\b(?:e\.g\.|i\.e\.))[^.!?\n]{0,60}$",
    re.IGNORECASE,
)
_AUDIT_MARKER = "[recurring-cycle auto-close]"
_TERMINAL_STATUSES = {
    'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'applied',
}
_STOP_STATUSES = {'cancelled', 'canceled', 'deferred', 'expired', 'rejected', 'duplicate', 'wont_fix', 'failed'}


@dataclass(frozen=True)
class RecurringCycleCloseResult:
    closed: bool
    reason: str
    next_due: Optional[object] = None


def _item_label(item) -> str:
    return getattr(item, 'item_key', None) or f"#{getattr(item, 'pk', getattr(item, 'id', '?'))}"


def _recurring_cycle_close_owner_team(item) -> str:
    return (
        (getattr(item, 'assignment_group', None) or '')
        or (getattr(item, 'team', None) or '')
        or (getattr(item, 'workflow_name', None) or '')
        or 'engineering'
    )


def _record_recurring_cycle_close_liveness(state: str, *, item=None, context: str = '', error: str = '') -> None:
    payload = {
        'state': state,
        'item': _item_label(item) if item is not None else '',
        'context': context,
        'error': error[:500],
        'at': timezone.now().isoformat(),
        'invariant': 'recurring-cycle-completed-parent-must-leave-active-rebalance-eligible-status',
    }
    try:
        cache.set('pulse:plat1353_recurring_cycle_close:last', payload, timeout=None)
        cache.set(f'pulse:plat1353_recurring_cycle_close:last_{state}', payload, timeout=None)
    except Exception:
        logger.exception('PLAT-1353 recurring-cycle liveness cache write failed')
    logger.warning(
        'metric:recurring_cycle_auto_close_%s PLAT-1353 item=%s context=%s error=%s invariant=%s',
        state,
        payload['item'],
        context,
        error[:240],
        payload['invariant'],
    )


def alert_recurring_cycle_auto_close_failure(item, *, context: str, exc: Exception, owner_team: str | None = None) -> bool:
    """Fail-loud path for PLAT-1353 mechanism failures.

    A recurring-cycle close failure must be visible to a named owner instead of
    remaining a warning log that lets rebalance churn recur. This records a
    liveness marker, comments on the affected item, creates an activity row, and
    notifies the owning team lead when one can be resolved.
    """
    error = f'{type(exc).__name__}: {exc}'
    team = owner_team or _recurring_cycle_close_owner_team(item)
    _record_recurring_cycle_close_liveness('failure', item=item, context=context, error=error)
    from tasks.models import Comment, ItemActivity
    # PLS-373: each fail-loud write gets its OWN savepoint (nested transaction.atomic()).
    # This is the fail-loud path for a cycle-close failure and it may run inside a
    # caller's transaction (e.g. the terminal-child status-change signal fires within
    # the transition's atomic block). A DB error here (previously an over-length
    # `action` value -> DataError) must NOT poison the caller's transaction — that
    # abort is what turned a cycle-close failure into a TransactionManagementError 500
    # on the transition (KOT-40/41). Separate savepoints also keep the ANDON comment
    # (the primary fail-loud signal, asserted by the CI test) from being rolled back if
    # the sibling activity write fails. Fail-loud must never be self-fatal (PLAT-1254).
    try:
        with transaction.atomic():
            Comment.objects.create(
                item=item,
                author_id=0,
                author_email='system@pulse',
                content=(
                    '🔴 ANDON [PLAT-1353 recurring-cycle auto-close failure]\n'
                    f'- item: `{_item_label(item)}`\n'
                    f'- context: `{context}`\n'
                    f'- owner_team: `{team}`\n'
                    f'- error: `{error[:500]}`\n'
                    '- invariant: completed recurring cycle must be parked/forward-scheduled so rebalance does not churn it\n'
                    '- liveness: `pulse:plat1353_recurring_cycle_close:last_failure` cache marker + `metric:recurring_cycle_auto_close_failure` log emitted\n'
                    '- need: owning team lead should inspect the linked PR/task event and either close the cycle manually or fix the hook.'
                ),
            )
    except Exception:
        logger.exception('PLAT-1353 recurring-cycle ANDON comment write failed for %s', _item_label(item))
    try:
        with transaction.atomic():
            ItemActivity.objects.create(
                item=item,
                action='recurring_cycle_auto_close_failed',
                field_name='recurring_cycle',
                old_value=context,
                new_value=error[:500],
                user_id=0,
                user_email='system@pulse',
            )
    except Exception:
        logger.exception('PLAT-1353 recurring-cycle activity write failed for %s', _item_label(item))

    notified = False
    try:
        from tasks.services.workflow_service import WorkflowService
        from tasks.services.notifications import UserNotificationService
        from tasks.user_service import user_service
        lead_email = WorkflowService._resolve_team_lead_email(team, item=item)
        if lead_email:
            info = user_service.get_user_by_email(lead_email)
            user_id = info.get('id') if isinstance(info, dict) else None
            if user_id:
                UserNotificationService().notify_user(
                    int(user_id),
                    'Recurring-cycle auto-close failed',
                    (
                        f'PLAT-1353 failed for {_item_label(item)} in {context}: {error[:300]}. '
                        'Invariant: completed recurring cycles must be parked/forward-scheduled so rebalance does not churn them.'
                    ),
                    event_type='alert',
                    severity='error',
                    item=item,
                )
                notified = True
    except Exception:
        logger.exception('PLAT-1353 recurring-cycle failure owner notification failed for %s', _item_label(item))
    return notified


def _is_terminal_status(slug: str) -> bool:
    if not slug:
        return False
    if slug in _TERMINAL_STATUSES:
        return True
    try:
        from tasks.models import Status
        return Status.objects.filter(slug=slug, category='done').exists()
    except Exception:
        return False


def _scheduled_status_slug() -> str:
    try:
        from tasks.models import Status
        if Status.objects.filter(slug='scheduled').exists():
            return 'scheduled'
    except Exception:
        pass
    return 'done'


def _sync_workflow_status(item, slug: str) -> None:
    try:
        from tasks.models import Status
        status = Status.objects.filter(slug=slug).first()
        if status:
            item.workflow_status = status.name
    except Exception:
        pass


def _strip_non_authorial_regions(content: str) -> str:
    """PLS-1384: blank out code fences, inline backticks, and blockquote lines
    so completion phrases inside examples/quotes never count as evidence.
    Returns a same-length mask string (structure preserved for span math)."""
    masked = list(content)

    def _mask(start: int, end: int) -> None:
        for i in range(max(0, start), min(len(masked), end)):
            if masked[i] not in '\n':
                masked[i] = ' '

    # Fenced code blocks (``` ... ```) — ```-line to closing ```-line.
    fence_positions = [m.start() for m in re.finditer(r'^\s*```', content, re.MULTILINE)]
    for idx in range(0, len(fence_positions) - 1, 2):
        _mask(fence_positions[idx], fence_positions[idx + 1])
    # Inline backticks.
    for m in re.finditer(r'`[^`\n]+`', content):
        _mask(m.start(), m.end())
    # Blockquote lines (quoted history).
    for m in re.finditer(r'^\s*>[^\n]*$', content, re.MULTILINE):
        _mask(m.start(), m.end())
    return ''.join(masked)


def _affirmative_completion_matches(content: str) -> list:
    """PLS-1384: every _COMPLETION_EVIDENCE_RE match in `content` that is
    affirmative prose (or the structured marker). A prose match is rejected
    when a negation token appears inside the matched span or in the same
    sentence immediately before it — "PARTIAL, not cycle complete" must never
    close a cycle."""
    masked = _strip_non_authorial_regions(content)
    accepted = []
    for match in _COMPLETION_EVIDENCE_RE.finditer(masked):
        # Structured marker: explicit action, exempt from the negation and
        # example guards — but ONLY outside quoting/backticks ("posting
        # `[recurring-cycle complete]`" is intent, not the act; a masked span
        # never counts, fail-safe).
        if match.group(0).lower() == '[recurring-cycle complete]':
            if match.group(0).lower() == content[match.start():match.end()].lower():
                accepted.append(match)
            continue
        # Negation inside the matched span ("cycle is not complete").
        span_text = match.group(0)
        if _NEGATION_RE.search(span_text):
            continue
        # Negation in the run-up to the match (fixed window — sentence
        # splitting is unreliable because 'e.g.'/'i.e.' dots read as
        # terminators; the window errs toward rejection, and the structured
        # marker remains the unambiguous path).
        runup = masked[max(0, match.start() - 80):match.start()]
        if _NEGATION_RE.search(runup):
            continue
        # Example/instructional context ("e.g. cycle complete").
        if _EXAMPLE_CONTEXT_RE.search(runup):
            continue
        accepted.append(match)
    return accepted


def _has_completion_evidence(item) -> bool:
    from tasks.models import Comment

    comments = Comment.objects.filter(item=item).only('content').order_by('-created_at')[:50]
    for comment in comments:
        content = comment.content or ''
        if _AUDIT_MARKER in content:
            continue
        if _affirmative_completion_matches(content):
            return True
    return False


def _trigger_seen(item, trigger_key: str) -> bool:
    cycle = ((item.metadata or {}).get('recurring_cycle') or {})
    closed = cycle.get('auto_closed_triggers') or []
    return trigger_key in closed


def _record_trigger(item, trigger_key: str, when) -> None:
    metadata = dict(item.metadata or {})
    cycle = dict(metadata.get('recurring_cycle') or {})
    closed = list(cycle.get('auto_closed_triggers') or [])
    if trigger_key not in closed:
        closed.append(trigger_key)
    # Keep the metadata bounded while preserving recent idempotency keys.
    cycle['auto_closed_triggers'] = closed[-20:]
    cycle['consecutive_skips'] = 0
    cycle['escalated_at_skips'] = 0
    cycle['last_terminal_completion_at'] = when.isoformat()
    cycle['last_auto_closed_at'] = when.isoformat()
    cycle['last_auto_closed_trigger'] = trigger_key
    metadata['recurring_cycle'] = cycle
    item.metadata = metadata


def _next_due(item, now):
    current_due = item.due_date or now
    computed = calculate_next_future_due_date(
        current_due,
        item.frequency,
        now,
        item.recurrence_pattern,
    )
    if computed is not None and computed > now:
        return computed
    # PLAT-10627: boundary escape hatch (mirror of recurring_cycle_state.
    # _next_due) — a close must never park the item on a stale or
    # uncomputable boundary; advance past `now` on the parsed fixed interval
    # or a conservative daily fallback so the cadence keeps progressing.
    fallback_seconds = parse_interval_seconds(
        item.frequency, item.recurrence_pattern,
    )
    if not fallback_seconds or fallback_seconds <= 0:
        fallback_seconds = 86400
    logger.warning(
        'recurring close boundary fallback engaged for item %s '
        '(frequency=%r): computed=%r, advancing to now+%ss',
        getattr(item, 'item_key', getattr(item, 'pk', '?')),
        item.frequency, computed, fallback_seconds,
    )
    return now + timedelta(seconds=fallback_seconds)


def close_recurring_cycle_if_complete(item, *, trigger_kind: str, trigger_label: str, actor_email: str = 'system@pulse', trigger_at=None) -> RecurringCycleCloseResult:
    """Close/park a recurring task's current cycle after a landed deliverable.

    Preconditions are deliberately strict:
    - ``item`` is a recurring task with a frequency,
    - it is not already terminal/stopped/scheduled,
    - the recurring parent has explicit completion evidence in a comment,
    - the trigger (merged PR URL or terminal linked-task key) has not already
      closed this cycle,
    - PLS-855: the trigger belongs to the CURRENT generation (``trigger_at`` falls
      inside the current period window) — a prior-cycle deliverable finishing late
      must not close a later period's cycle nor reset its missed-period escalation.
    """
    from tasks.models import Comment

    if not getattr(item, 'is_recurring', False) or not getattr(item, 'frequency', None):
        return RecurringCycleCloseResult(False, 'not_recurring')

    status = (item.status or '').strip().lower()
    if status in _STOP_STATUSES:
        return RecurringCycleCloseResult(False, 'stop_status')
    if status == 'scheduled' or _is_terminal_status(status):
        return RecurringCycleCloseResult(False, 'already_in_gap_or_terminal')

    if not _has_completion_evidence(item):
        return RecurringCycleCloseResult(False, 'missing_completion_evidence')

    trigger_key = f"{trigger_kind}:{trigger_label}"
    if _trigger_seen(item, trigger_key):
        return RecurringCycleCloseResult(False, 'already_closed_for_trigger')

    # PLS-419: normal terminal transitions and linked-deliverable auto-close
    # share the same locked completion mutation.  In particular, both preserve
    # an already-future due boundary and both stamp/reset exactly once.
    from tasks.services.recurring_cycle_state import complete_cycle, cycle_snapshot
    expected = cycle_snapshot(item)
    result = complete_cycle(
        item.pk,
        actor_id=0,
        actor_email=actor_email[:254] if actor_email else 'system@pulse',
        trigger_key=trigger_key,
        expected_cycle_revision=expected['cycle_revision'],
        expected_generation=expected['generation'],
        expected_due_at=expected['due_boundary'],
        expected_completion_watermark=expected['completion_watermark'] or '',
        trigger_at=trigger_at,
    )
    if not result.applied:
        # PLS-855: a prior-generation deliverable rejected at window admission is
        # an observable no-op (escalation stays live) — record it so audits of
        # stale auto-close (AC5) and debugging don't mistake it for silence. This
        # is metadata/liveness only; the parent row is untouched.
        if result.reason == 'stale_evidence_prior_generation':
            _record_recurring_cycle_close_liveness(
                'stale_ignored', item=item, context=f'{trigger_kind}:{trigger_label}',
            )
        return RecurringCycleCloseResult(False, result.reason, result.next_due)
    item = result.item
    next_due = result.next_due
    Comment.objects.create(
        item=item,
        author_id=0,
        author_email='system@pulse',
        content=(
            f"{_AUDIT_MARKER} Closed current cycle after linked {trigger_kind} "
            f"`{trigger_label}` completed. Next due: {next_due.date().isoformat()}. "
            "Completion evidence was present on this recurring parent. [PLAT-1353]"
        ),
    )

    _record_recurring_cycle_close_liveness('closed', item=item, context=f'{trigger_kind}:{trigger_label}')
    return RecurringCycleCloseResult(True, 'closed', next_due)


def close_recurring_parents_for_terminal_child(child, *, actor_email: str = 'system@pulse') -> int:
    """Close recurring parents linked to a child task that just became terminal."""
    from tasks.models import ItemLink

    if not _is_terminal_status((child.status or '').strip().lower()):
        return 0

    links = (
        ItemLink.objects
        .filter(Q(target_item=child) | Q(source_item=child))
        .select_related('source_item', 'target_item')
    )
    # PLS-855: bind the deliverable to the period that DIRECTED it — the child's
    # creation time, falling back to completion time. A task directed by a prior
    # cycle that finishes late (e.g. PLAT-1189's VEN-218: due 07-29, landed 08-02)
    # must not be credited to a later period: its creation precedes the later
    # generation's window, so window admission turns the close into a no-op.
    child_directed_at = (
        getattr(child, 'created_at', None)
        or getattr(child, 'completed_at', None)
        or timezone.now()
    )
    count = 0
    for link in links:
        # Treat non-blocking relationship links as implementation/deliverable links.
        if link.link_type in {'blocks', 'is_blocked_by', 'duplicates', 'is_duplicated_by', 'clones', 'is_cloned_by'}:
            continue
        parent = link.source_item if link.target_item_id == child.pk else link.target_item
        if parent.pk == child.pk:
            continue
        label = child.item_key or str(child.pk)
        result = close_recurring_cycle_if_complete(
            parent,
            trigger_kind='task',
            trigger_label=label,
            actor_email=actor_email,
            trigger_at=child_directed_at,
        )
        if result.closed:
            count += 1
    return count
