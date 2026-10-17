"""PLAT-2962: QA false-pass detector.

Detective control for the QA operating model: find work that was marked
terminal/verified and then reopened or re-filed as a linked/mentioned regression
inside a short rolling window.  The output is data-backed (report item metadata +
comments) so PLAT-1149 can cite it instead of sampling manually.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import re
from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from ..models import Comment, Item, ItemActivity, ItemLink, Project, Status
from .workflow_service import WorkflowService as W

logger = logging.getLogger(__name__)

REPORT_SOURCE = 'qa-false-pass-detector'
REPORT_ITEM_TYPE = 'task.qa_false_pass_metric'
REPORT_AUTHOR_ID = 0
REPORT_AUTHOR_EMAIL = 'qa-false-pass-detector@pulse.shizuha.com'
DEFAULT_WINDOW_DAYS = 14
DEFAULT_LOOKBACK_DAYS = 14
FALSE_PASS_FLOOR_PER_20 = 1.0

# PLS-718: the rolling 14d scan streams tens of thousands of comments; the
# global 15s statement_timeout (taskmanager.settings) is too tight for the
# sweep's own queries. Raise it only for this session with a bounded value so a
# runaway query still fails loudly, and restore in all paths. Postgres-only;
# SQLite (tests) has no statement_timeout GUC.
_SWEEP_STATEMENT_TIMEOUT_MS = 120_000

_STATIC_TERMINAL_STATUSES = {
    'completed', 'done', 'closed', 'resolved', 'merged', 'rejected', 'duplicate',
    'wont_fix', 'cancelled', 'canceled', 'failed', 'expired', 'deferred',
    'applied', 'silenced',
}
_NON_VERIFICATION_TERMINAL_STATUSES = {
    'rejected', 'duplicate', 'wont_fix', 'cancelled', 'canceled', 'failed',
    'expired', 'deferred', 'silenced',
}
_QA_PASS_RE = re.compile(
    r'\b(?:qa\s+pass(?:ed)?|verified(?:\s+by\s+qa)?|verification\s+pass(?:ed)?|live\s+verification\s+pass(?:ed)?)\b',
    re.IGNORECASE,
)
_QA_FAIL_RE = re.compile(
    r'\b(?:fail(?:ed|ure)?|blocked|not\s+pass(?:ed)?|cannot\s+verify|not\s+(?:yet\s+)?verified|unverified|verification\s+(?:pending|waiting)|waiting\s+on\s+(?:verification|evidence))\b',
    re.IGNORECASE,
)


@contextmanager
def _raise_statement_timeout(ms: int = _SWEEP_STATEMENT_TIMEOUT_MS):
    """Temporarily raise statement_timeout for this connection (bounded).

    PLS-718: the detector's own queries (streaming the 14d comment window) must
    not be killed by the global 15s default. Raise only for this session with a
    bounded value and restore in all paths so the setting never leaks to other
    work on the same pooled connection. No-op on non-Postgres backends.
    """
    if connection.vendor != 'postgresql':
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute('SET statement_timeout = %s', [ms])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('RESET statement_timeout')


@dataclass
class VerificationEvent:
    item: Item
    verified_at: object
    source: str


@dataclass
class FalsePassPair:
    origin: Item
    related: Item
    detected_at: object
    reasons: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.origin.pk}:{self.related.pk}"

    def as_dict(self) -> dict:
        return {
            'origin_key': self.origin.item_key or f'#{self.origin.pk}',
            'origin_title': self.origin.title,
            'related_key': self.related.item_key or f'#{self.related.pk}',
            'related_title': self.related.title,
            'detected_at': self.detected_at.isoformat() if hasattr(self.detected_at, 'isoformat') else str(self.detected_at),
            'reasons': sorted(self.reasons),
        }


def _norm(value) -> str:
    return str(value or '').strip().lower().replace('-', '_').replace(' ', '_')


def _terminal_statuses() -> set[str]:
    statuses = set(_STATIC_TERMINAL_STATUSES)
    try:
        for row in Status.objects.filter(category='done').values('slug', 'name'):
            statuses.add(_norm(row.get('slug')))
            statuses.add(_norm(row.get('name')))
    except Exception:
        logger.debug('false_pass_detector: could not read done-category statuses', exc_info=True)
    return statuses


def _is_focused_qa_item(item: Item) -> bool:
    """Focused-QA lineage artifacts are not genuine regressions for detector purposes."""
    source = str(item.source or '').strip().lower()
    if source.startswith('focused-qa'):
        return True
    workflow = str(item.workflow_name or '').strip().lower()
    if workflow == 'focused-qa':
        return True
    item_type = str(item.item_type or '').strip().lower()
    if item_type == 'task.focused_qa':
        return True
    title = str(item.title or '').strip().lower()
    if title.startswith('[focused qa') or title.startswith('focused qa'):
        return True
    tags = str(item.tags or '')
    if 'focused-qa' in tags.lower() or 'focused_qa' in tags.lower():
        return True
    labels = item.labels or {}
    if isinstance(labels, dict):
        if str(labels.get('source', '')).strip().lower().startswith('focused-qa'):
            return True
        if any(str(k).startswith('focused_qa_') for k in labels.keys()):
            return True
    metadata = item.metadata or {}
    if isinstance(metadata, dict) and any(str(k).startswith('focused_qa_') for k in metadata.keys()):
        return True
    return False
def _qa_pass_comment(comment: Comment) -> bool:
    text = comment.content or ''
    return bool(_QA_PASS_RE.search(text)) and not _QA_FAIL_RE.search(text)


# PLS-718: coarse SQL pre-filter so the detector never materializes the full 14d
# comment set (~87k rows / 45MB of content). These substrings are exactly the
# literals in _QA_PASS_RE, so the filter is a strict superset of the Python
# match; the precise _qa_pass_comment() check below remains authoritative.
_QA_PASS_PREFILTER_KEYWORDS = ('qa pass', 'verified', 'verification pass', 'live verification')


def _verification_events(period_start, period_end) -> list[VerificationEvent]:
    terminal = _terminal_statuses()
    events_by_item: dict[int, VerificationEvent] = {}

    close_activities = (
        ItemActivity.objects
        .filter(field_name='Status', created_at__gte=period_start, created_at__lte=period_end)
        .filter(Q(action__in=('status_changed', 'completed')) | Q(new_value__in=terminal))
        .select_related('item')
        .order_by('created_at')
        .iterator(chunk_size=2000)
    )
    for act in close_activities:
        new_status = _norm(act.new_value)
        if new_status not in terminal or new_status in _NON_VERIFICATION_TERMINAL_STATUSES:
            continue
        item = act.item
        if item.mode != 'task':
            continue
        if _is_focused_qa_item(item):
            continue
        events_by_item[item.pk] = VerificationEvent(item=item, verified_at=act.created_at, source='terminal_status')

    # PLS-718: push the QA-pass text match into SQL (icontains pre-filter) and
    # stream with .iterator() + .only() instead of materializing every comment
    # as a full Comment + select_related Item instance. This keeps both the
    # statement cost and the peak RSS inside the serving pod's envelope.
    prefilter = Q()
    for kw in _QA_PASS_PREFILTER_KEYWORDS:
        prefilter |= Q(content__icontains=kw)
    comments = (
        Comment.objects
        .filter(created_at__gte=period_start, created_at__lte=period_end, item__mode='task')
        .filter(prefilter)
        .select_related('item')
        .only(
            'id', 'item_id', 'content', 'created_at',
            'item__status', 'item__source', 'item__workflow_name',
            'item__item_type', 'item__title', 'item__tags',
            'item__labels', 'item__metadata', 'item__mode',
        )
        .order_by('created_at')
        .iterator(chunk_size=2000)
    )
    for comment in comments:
        if not _qa_pass_comment(comment):
            continue
        if _norm(comment.item.status) in _NON_VERIFICATION_TERMINAL_STATUSES:
            continue
        if _is_focused_qa_item(comment.item):
            # Focused-QA lineage artifacts are not counted as source verifications.
            continue
        # A later explicit QA pass supersedes an earlier terminal transition as
        # the verification timestamp for the same item.
        existing = events_by_item.get(comment.item_id)
        if existing is not None and existing.verified_at >= comment.created_at:
            continue
        events_by_item[comment.item_id] = VerificationEvent(item=comment.item, verified_at=comment.created_at, source='qa_pass_comment')

    return list(events_by_item.values())


def _record_pair(pairs: dict[str, FalsePassPair], origin: Item, related: Item, detected_at, reason: str) -> None:
    key = f'{origin.pk}:{related.pk}'
    pair = pairs.get(key)
    if pair is None:
        pair = FalsePassPair(origin=origin, related=related, detected_at=detected_at, reasons=set())
        pairs[key] = pair
    if detected_at and detected_at < pair.detected_at:
        pair.detected_at = detected_at
    pair.reasons.add(reason)


def _find_pairs_for_event(event: VerificationEvent, *, lookback_days: int, terminal: set[str], now=None) -> dict[str, FalsePassPair]:
    origin = event.item
    deadline = event.verified_at + timezone.timedelta(days=lookback_days)
    now = now or timezone.now()
    end = min(deadline, now)
    pairs: dict[str, FalsePassPair] = {}

    # Same item reopened after terminal/verified.
    reopen_activities = (
        ItemActivity.objects
        .filter(item=origin, field_name='Status', created_at__gt=event.verified_at, created_at__lte=end)
        .order_by('created_at')
    )
    for act in reopen_activities:
        old_status = _norm(act.old_value)
        new_status = _norm(act.new_value)
        explicit_reopen = act.action == 'reopened' and bool(new_status) and new_status not in terminal
        terminal_to_active = old_status in terminal and bool(new_status) and new_status not in terminal
        if explicit_reopen or terminal_to_active:
            _record_pair(pairs, origin, origin, act.created_at, 'reopened_after_verification')
            break

    # Only an explicit duplicate/refile relationship is evidence that later
    # work represents the verified origin failing again.  Generic relates-to /
    # blocker edges are context, not a regression event.
    links = (
        ItemLink.objects
        .filter(
            Q(source_item=origin) | Q(target_item=origin),
            link_type__in=('duplicates', 'is_duplicated_by'),
            created_at__gt=event.verified_at,
            created_at__lte=end,
        )
        .select_related('source_item', 'target_item')
    )
    for link in links:
        other = link.target_item if link.source_item_id == origin.pk else link.source_item
        if other.pk == origin.pk:
            continue
        if _is_focused_qa_item(other):
            continue
        _record_pair(pairs, origin, other, link.created_at, f'linked_{link.link_type}')

    return pairs


def compute_false_pass_metrics(*, window_days: int = DEFAULT_WINDOW_DAYS, lookback_days: int = DEFAULT_LOOKBACK_DAYS, now=None) -> dict:
    """Compute rolling false-pass metrics.

    ``window_days`` controls the denominator (verification events in the rolling
    period). ``lookback_days`` controls how long after each verification a
    reopen/refile/regression link counts as a false pass.
    """
    now = now or timezone.now()
    period_start = now - timezone.timedelta(days=max(1, int(window_days)))
    with _raise_statement_timeout():
        terminal = _terminal_statuses()
        events = _verification_events(period_start, now)

        pairs: dict[str, FalsePassPair] = {}
        for event in events:
            for key, pair in _find_pairs_for_event(event, lookback_days=max(1, int(lookback_days)), terminal=terminal, now=now).items():
                if key in pairs:
                    pairs[key].reasons.update(pair.reasons)
                    if pair.detected_at < pairs[key].detected_at:
                        pairs[key].detected_at = pair.detected_at
                else:
                    pairs[key] = pair

    verification_count = len(events)
    false_pass_count = len(pairs)
    rate_per_20 = (false_pass_count * 20.0 / verification_count) if verification_count else 0.0
    breach = rate_per_20 > FALSE_PASS_FLOOR_PER_20

    pair_rows = sorted((p.as_dict() for p in pairs.values()), key=lambda r: (r['origin_key'], r['related_key']))
    reason_counts: dict[str, int] = {}
    for pair in pair_rows:
        for reason in pair['reasons']:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        'period_start': period_start.isoformat(),
        'period_end': now.isoformat(),
        'window_days': int(window_days),
        'lookback_days': int(lookback_days),
        'verification_count': verification_count,
        'false_pass_count': false_pass_count,
        'false_pass_rate_per_20': round(rate_per_20, 3),
        'floor_per_20': FALSE_PASS_FLOOR_PER_20,
        'breach': breach,
        'reason_counts': dict(sorted(reason_counts.items())),
        'pairs': pair_rows,
    }


def format_false_pass_report(metrics: dict) -> str:
    rate = metrics.get('false_pass_rate_per_20', 0)
    lines = [
        f"🔎 QA false-pass detector — rolling {metrics['window_days']}d",
        f"- verifications sampled: {metrics['verification_count']}",
        f"- false-pass pairs: {metrics['false_pass_count']}",
        f"- rate: {rate:g} per 20 verifications (floor: <= {metrics['floor_per_20']:g})",
        f"- breach: {'YES' if metrics.get('breach') else 'no'}",
    ]
    reason_counts = metrics.get('reason_counts') or {}
    if reason_counts:
        lines.append('- reason counts: ' + ', '.join(f'{reason}={count}' for reason, count in sorted(reason_counts.items())))
    else:
        lines.append('- reason counts: none')
    pairs = metrics.get('pairs') or []
    if pairs:
        lines.append('- offending pairs:')
        for pair in pairs[:20]:
            reasons = ', '.join(pair.get('reasons') or [])
            lines.append(f"  - {pair['origin_key']} → {pair['related_key']} ({reasons})")
        if len(pairs) > 20:
            lines.append(f"  - … +{len(pairs) - 20} more")
    else:
        lines.append('- offending pairs: none')
    return '\n'.join(lines)


def _get_report_project() -> Project | None:
    return Project.objects.filter(key='PLAT').first() or Project.objects.filter(key='PLS').first() or Project.objects.first()


def _get_or_create_report_item(metrics: dict) -> tuple[Item, bool]:
    project = _get_report_project()
    item, created = Item.objects.get_or_create(
        source=REPORT_SOURCE,
        source_id=f"rolling-{metrics['window_days']}d",
        defaults={
            'mode': 'task',
            'item_type': REPORT_ITEM_TYPE,
            'project': project,
            'title': f"[QA metric] Rolling {metrics['window_days']}d false-pass detector",
            'description': format_false_pass_report(metrics),
            'status': 'scheduled',
            'workflow_name': 'simple',
            'assignment_group': 'qa',
            'created_by_id': REPORT_AUTHOR_ID,
            'created_by_email': REPORT_AUTHOR_EMAIL,
            'labels': {'qa': True, 'quality-metrics': True, 'false-pass': True, 'automation': True},
            'tags': 'qa,quality-metrics,false-pass,automation',
            'metadata': {'false_pass_detector': metrics},
        },
    )
    return item, created


def _pair_signature(metrics: dict) -> list[str]:
    return [f"{p['origin_key']}->{p['related_key']}:{'|'.join(p.get('reasons') or [])}" for p in metrics.get('pairs') or []]


def _link_report_to_monthly_audit(report_item: Item) -> None:
    audit = Item.objects.filter(item_key='PLAT-1149').first()
    if not audit:
        return
    ItemLink.objects.get_or_create(
        source_item=report_item,
        target_item=audit,
        link_type='relates_to',
        defaults={'created_by_id': REPORT_AUTHOR_ID, 'created_by_email': REPORT_AUTHOR_EMAIL},
    )


def _notify_qa_lead(metrics: dict, report_item: Item) -> bool:
    try:
        from tasks.services.connect_origin import system_message_headers
        from tasks.user_service import user_service
        import requests

        lead_email = W._resolve_team_lead_email('qa') or 'zen@shizuha.com'
        user = user_service.get_user_by_email(lead_email)
        user_id = user.get('id') if user else None
        if not user_id:
            logger.warning('false_pass_detector: no QA lead user_id for %s', lead_email)
            return False
        item_key = report_item.item_key or f'#{report_item.pk}'
        content = (
            f"[QA False-Pass Breach] {metrics['false_pass_count']} pair(s), "
            f"{metrics['false_pass_rate_per_20']:g} per 20 verifications over {metrics['window_days']}d.\n"
            f"Report: {item_key} — {report_item.title}\n"
            f"Use pulse_get_task(\"{item_key}\") for offending pairs."
        )
        body = {
            'recipient_user_id': user_id,
            'content': content,
            'sender_name': 'Pulse QA False-Pass Detector',
            'sender_service': 'shizuha-pulse',
        }
        connect_url = f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/"
        response = requests.post(connect_url, json=body, headers=system_message_headers(body), timeout=5)
        response.raise_for_status()
        return True
    except Exception as exc:  # pragma: no cover - best-effort notification
        logger.warning('false_pass_detector: QA lead notification failed: %s', exc)
        return False


def run_false_pass_detector(*, window_days: int = DEFAULT_WINDOW_DAYS, lookback_days: int = DEFAULT_LOOKBACK_DAYS, alert: bool = True, now=None) -> dict:
    """Compute, persist, and optionally alert on the false-pass metric."""
    now = now or timezone.now()
    metrics = compute_false_pass_metrics(window_days=window_days, lookback_days=lookback_days, now=now)
    report_item, created = _get_or_create_report_item(metrics)

    metadata = dict(report_item.metadata or {})
    previous_sig = metadata.get('false_pass_pair_signature') or []
    current_sig = _pair_signature(metrics)
    previous_breach = bool(metadata.get('false_pass_breach'))
    previous_notified_sig = metadata.get('false_pass_notified_signature') or []
    changed = created or previous_sig != current_sig or previous_breach != bool(metrics.get('breach'))

    # PLS-718: surface run provenance so a reader can distinguish "no change"
    # from "did not run" — the report must never silently serve stale numbers.
    now_iso = now.isoformat()
    next_metadata = {
        **metadata,
        'false_pass_detector': metrics,
        'false_pass_pair_signature': current_sig,
        'false_pass_breach': bool(metrics.get('breach')),
        'last_run_status': 'ok',
        'last_run_error': None,
        'last_attempted_at': now_iso,
    }
    if not metrics.get('breach'):
        next_metadata.update({
            'false_pass_notified_signature': [],
            'false_pass_notification_delivered': False,
            'false_pass_notification_failed': False,
        })

    report_item.description = f"{format_false_pass_report(metrics)}\n- last run: ok ({now_iso})"
    report_item.metadata = next_metadata
    report_item._activity_actor = (REPORT_AUTHOR_ID, REPORT_AUTHOR_EMAIL)
    report_item.save(update_fields=['description', 'metadata', 'updated_at'])
    _link_report_to_monthly_audit(report_item)

    if changed:
        Comment.objects.create(
            item=report_item,
            author_id=REPORT_AUTHOR_ID,
            author_email=REPORT_AUTHOR_EMAIL,
            content=format_false_pass_report(metrics),
        )

    notified = False
    needs_notification = alert and metrics.get('breach') and current_sig != previous_notified_sig
    if needs_notification:
        notified = _notify_qa_lead(metrics, report_item)
        notification_metadata = dict(report_item.metadata or {})
        if notified:
            notification_metadata.update({
                'false_pass_notified_signature': current_sig,
                'false_pass_notification_delivered': True,
                'false_pass_notification_failed': False,
            })
        else:
            notification_metadata.update({
                'false_pass_notification_delivered': False,
                'false_pass_notification_failed': True,
            })
        report_item.metadata = notification_metadata
        report_item._activity_actor = (REPORT_AUTHOR_ID, REPORT_AUTHOR_EMAIL)
        report_item.save(update_fields=['metadata', 'updated_at'])
        Comment.objects.create(
            item=report_item,
            author_id=REPORT_AUTHOR_ID,
            author_email=REPORT_AUTHOR_EMAIL,
            content=f"Breach notification to QA lead {'sent' if notified else 'failed/skipped'}.",
        )

    return {
        **metrics,
        'report_item_key': report_item.item_key or f'#{report_item.pk}',
        'report_item_id': str(report_item.pk),
        'report_changed': changed,
        'qa_lead_notified': notified,
    }


def _mark_report_run_failed(exc: Exception, *, window_days: int = DEFAULT_WINDOW_DAYS) -> None:
    """Record a failed sweep on the report item so staleness is visible.

    PLS-718: when the recurring sweep dies (statement timeout / OOM / logic
    error), the report item must not keep serving its last successful numbers
    as if nothing changed. Stamp last_run_status='failed' + the error + the
    attempt time into metadata and description.
    """
    try:
        report_item = Item.objects.filter(
            source=REPORT_SOURCE,
            source_id=f"rolling-{int(window_days)}d",
        ).first()
        if report_item is None:
            return
        metadata = dict(report_item.metadata or {})
        now_iso = timezone.now().isoformat()
        error = f'{type(exc).__name__}: {exc}'
        metadata.update({
            'last_run_status': 'failed',
            'last_run_error': error,
            'last_attempted_at': now_iso,
        })
        report_item.metadata = metadata
        report_item.description = f"{report_item.description or ''}\n- last run: FAILED ({error}) at {now_iso}"
        report_item._activity_actor = (REPORT_AUTHOR_ID, REPORT_AUTHOR_EMAIL)
        report_item.save(update_fields=['description', 'metadata', 'updated_at'])
    except Exception:  # pragma: no cover - must never mask the original failure
        logger.exception('false_pass_detector: could not record failed run state')
