"""PLS-621 — routing source-health + pre-triage starvation signals.

Correct local dispositions (false_positive / not_a_bug / cancel of a detector
artifact) must never erase systemic detector evidence. This module keeps a
bounded, keyed aggregate series and emits one deduplicated Program/Platform
signal (Connect DM via the existing org-health sink — never a new Pulse task
per firing).

Also tracks needs_triage enter/exit so urgent unowned pre-triage work signals
immediately while normal-priority work arms a delayed age deadline.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Resolutions that mean "this detector artifact was wrong / not actionable".
FALSE_DISPOSITION_SLUGS = frozenset({
    'false_positive',
    'false-positive',
    'not_a_bug',
    'not-a-bug',
    'notabug',
    'wont_fix',
    'wontfix',
    "won't_fix",
    'invalid',
    'rejected',
    'cancelled',
    'canceled',
})

# Terminal status slugs that close a detector artifact without a real fix.
FALSE_DISPOSITION_STATUSES = frozenset({
    'done',
    'cancelled',
    'canceled',
    'rejected',
    'closed',
})

# One series per detector+org+subject. Reason is recorded on samples only so
# mixed false_positive/not_a_bug/cancel dispositions share one aggregate.
_SERIES_KEY = 'pulse:routing_source_health:series:{detector}:{org}:{subject}'
_SERIES_INDEX = 'pulse:routing_source_health:index'
_PRETRIAGE_KEY = 'pulse:routing_source_health:pretriage:{item_id}'
_PRETRIAGE_DEPTH = 'pulse:routing_source_health:pretriage_depth:{org}:{team}'
_ALERT_CLAIM = 'pulse:routing_source_health:alert:{kind}:{key}'

_MAX_SAMPLES = 12
_SERIES_TTL = None  # durable until explicit healthy clear


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def false_disposition_window_seconds() -> int:
    return max(60, _env_int('PULSE_SOURCE_HEALTH_WINDOW_SECONDS', 7 * 24 * 3600))


def false_disposition_alert_threshold() -> int:
    return max(2, _env_int('PULSE_SOURCE_HEALTH_FALSE_DISPOSITION_THRESHOLD', 3))


def pretriage_age_threshold_hours() -> float:
    return max(0.25, float(os.environ.get('PULSE_PRETRIAGE_AGE_HOURS', '24')))


def pretriage_depth_threshold() -> int:
    return max(1, _env_int('PULSE_PRETRIAGE_DEPTH_THRESHOLD', 15))


def alert_reping_seconds() -> int:
    return max(300, _env_int('PULSE_SOURCE_HEALTH_REPING_SECONDS', 6 * 3600))


def _is_detector_artifact(item) -> bool:
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        return False
    if meta.get('org_health_signal') or meta.get('auto_filed'):
        return True
    title = (getattr(item, 'title', None) or '').lower()
    return any(
        marker in title
        for marker in (
            '[team outage]',
            'queue starving',
            'capacity-starve',
            'zero-member',
            'whole-team outage',
            '[program mgmt]',
        )
    )


def _detector_key(item) -> str:
    meta = getattr(item, 'metadata', None) or {}
    sig = str(meta.get('org_health_signal') or '').strip()
    if sig:
        # capacity-starve:1:engineering → capacity-starve
        return sig.split(':', 1)[0] or 'org-health'
    title = (getattr(item, 'title', None) or '').lower()
    if 'team outage' in title or 'zero-member' in title:
        return 'zero-member-outage'
    if 'starv' in title or 'capacity' in title:
        return 'capacity-starve'
    return 'org-health'


def _subject_key(item) -> str:
    meta = getattr(item, 'metadata', None) or {}
    sig = str(meta.get('org_health_signal') or '')
    parts = [p for p in sig.split(':') if p]
    if len(parts) >= 2:
        return parts[-1][:80]
    group = (getattr(item, 'assignment_group', None) or '').strip()
    return group[:80] or 'unknown'


def _org_key(item) -> str:
    oid = getattr(item, 'organization_id', None)
    if oid is not None:
        return str(oid)
    project = getattr(item, 'project', None)
    if project is not None and getattr(project, 'organization_id', None) is not None:
        return str(project.organization_id)
    return '0'


def _reason_class(resolution_slug: str, status_slug: str) -> str:
    slug = (resolution_slug or status_slug or 'unknown').strip().lower()
    if slug in {'false_positive', 'false-positive'}:
        return 'false_positive'
    if slug in {'not_a_bug', 'not-a-bug', 'notabug', 'invalid'}:
        return 'not_a_bug'
    if slug in {'wont_fix', 'wontfix', "won't_fix"}:
        return 'wont_fix'
    if slug in {'cancelled', 'canceled'}:
        return 'cancelled'
    if slug in {'rejected'}:
        return 'rejected'
    return slug or 'unknown'


def _series_cache_key(detector: str, org: str, subject: str) -> str:
    return _SERIES_KEY.format(
        detector=detector[:80],
        org=org[:40],
        subject=subject[:80],
    )


def get_series(detector: str, org: str, subject: str, reason: str = '') -> dict:
    key = _series_cache_key(detector, org, subject)
    data = cache.get(key) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('count', 0)
    data.setdefault('samples', [])
    data.setdefault('first_at', None)
    data.setdefault('last_at', None)
    data.setdefault('healthy', False)
    data.setdefault('detector', detector)
    data.setdefault('organization', org)
    data.setdefault('subject', subject)
    data.setdefault('reason_class', reason or 'mixed')
    data.setdefault('reason_counts', {})
    return data


def _index_add(cache_key: str) -> None:
    try:
        idx = cache.get(_SERIES_INDEX) or []
        if not isinstance(idx, list):
            idx = []
        if cache_key not in idx:
            idx = (idx + [cache_key])[-500:]
            cache.set(_SERIES_INDEX, idx, timeout=_SERIES_TTL)
    except Exception:
        logger.exception('PLS-621: series index update failed')


def record_false_disposition(
    item,
    *,
    resolution_slug: str = '',
    status_slug: str = '',
    actor: str = '',
) -> Optional[dict]:
    """Increment source-health on a false disposition of a detector artifact.

    Individual cancellation closes that artifact but never clears the series.
    Returns the updated series payload (or None if not a detector artifact /
    not a false disposition).
    """
    if not _is_detector_artifact(item):
        return None

    reason = _reason_class(resolution_slug, status_slug)
    res_l = (resolution_slug or '').strip().lower()
    st_l = (status_slug or '').strip().lower()
    if res_l not in FALSE_DISPOSITION_SLUGS and st_l not in {
        'cancelled', 'canceled', 'rejected',
    }:
        return None

    detector = _detector_key(item)
    org = _org_key(item)
    subject = _subject_key(item)
    key = _series_cache_key(detector, org, subject)
    now = timezone.now()
    now_iso = now.isoformat()

    series = get_series(detector, org, subject, reason)
    series['count'] = int(series.get('count') or 0) + 1
    series['healthy'] = False
    series['first_at'] = series.get('first_at') or now_iso
    series['last_at'] = now_iso
    series['reason_class'] = reason  # last-seen reason; counts keep the mix
    rc = dict(series.get('reason_counts') or {})
    rc[reason] = int(rc.get(reason) or 0) + 1
    series['reason_counts'] = rc
    samples = list(series.get('samples') or [])
    samples.append({
        'item_key': getattr(item, 'item_key', None) or str(getattr(item, 'pk', '')),
        'at': now_iso,
        'resolution': (resolution_slug or '')[:40],
        'status': (status_slug or '')[:40],
        'reason_class': reason,
        'actor': (actor or '')[:120],
        'title': (getattr(item, 'title', None) or '')[:160],
    })
    series['samples'] = samples[-_MAX_SAMPLES:]
    series['class'] = 'source_unhealthy'

    try:
        cache.set(key, series, timeout=_SERIES_TTL)
        _index_add(key)
    except Exception:
        logger.exception('PLS-621: failed to persist source-health series')

    logger.warning(
        'metric:routing_source_health class=source_unhealthy detector=%s org=%s '
        'subject=%s reason=%s count=%s item_key=%s',
        detector, org, subject, reason, series['count'],
        getattr(item, 'item_key', ''),
    )

    if series['count'] >= false_disposition_alert_threshold():
        _emit_source_unhealthy_alert(series)

    return series


def mark_source_healthy(
    detector: str,
    org: str,
    subject: str,
    reason: str = '',
) -> dict:
    """Explicit healthy recovery — the only path that clears the aggregate."""
    key = _series_cache_key(detector, org, subject)
    series = get_series(detector, org, subject, reason)
    series['healthy'] = True
    series['recovered_at'] = timezone.now().isoformat()
    series['class'] = 'source_healthy'
    cache.set(key, series, timeout=_SERIES_TTL)
    logger.info(
        'metric:routing_source_health class=source_healthy detector=%s org=%s '
        'subject=%s reason=%s',
        detector, org, subject, reason,
    )
    return series


def list_unhealthy_series() -> list[dict]:
    idx = cache.get(_SERIES_INDEX) or []
    out = []
    if not isinstance(idx, list):
        return out
    for key in idx:
        data = cache.get(key)
        if isinstance(data, dict) and not data.get('healthy') and int(data.get('count') or 0) > 0:
            out.append(data)
    return out


def _emit_source_unhealthy_alert(series: dict) -> bool:
    """One deduplicated DM-only signal — never a new Pulse task per firing."""
    detector = series.get('detector') or 'org-health'
    org = series.get('organization') or '0'
    subject = series.get('subject') or 'unknown'
    reason = series.get('reason_class') or 'unknown'
    signal_key = f"source-unhealthy:{org}:{detector}:{subject}"
    samples = series.get('samples') or []
    sample_md = '\n'.join(
        f"- {s.get('item_key')} @ {s.get('at')} ({s.get('resolution') or s.get('status')})"
        for s in samples[-5:]
    ) or '- (no samples)'
    reason_counts = series.get('reason_counts') or {}
    reasons_md = ', '.join(f"{k}={v}" for k, v in sorted(reason_counts.items())) or reason
    title = (
        f"[source_unhealthy] detector={detector} subject={subject} "
        f"false-dispositions={series.get('count')}"
    )
    body = (
        f"**Class:** `source_unhealthy` (not a team outage, not queue starvation).\n\n"
        f"Repeated correct local cancellations of detector artifacts are accumulating "
        f"for detector `{detector}` / subject `{subject}` (org={org}). "
        f"Count: **{series.get('count')}** "
        f"(threshold {false_disposition_alert_threshold()}). "
        f"Reasons: {reasons_md}.\n\n"
        f"Individual artifact disposition does **not** clear this aggregate. "
        f"Clear only after an explicit healthy source signal.\n\n"
        f"Evidence samples:\n{sample_md}\n\n"
        f"Owners: Program Management + Platform (routing detector health). "
        f"Do not file another Pulse task per firing (PLS-621)."
    )
    return _emit_dm_once(
        signal_key,
        title=title,
        body=body,
        kind='source_unhealthy',
        organization_id=org,
        subject_key=subject,
    )


def _emit_dm_once(
    signal_key: str,
    *,
    title: str,
    body: str,
    kind: str,
    organization_id=None,
    subject_key: str = '',
) -> bool:
    try:
        from django.core.cache import cache as _c
        claim_key = _ALERT_CLAIM.format(kind=kind[:40], key=signal_key[:180])
        if not _c.add(claim_key, timezone.now().isoformat(), timeout=alert_reping_seconds()):
            return False
    except Exception:
        logger.exception('PLS-621: alert claim failed; delivering anyway')

    try:
        from ..tasks import _dm_program_management_lead
        from .workflow_service import WorkflowService

        text = f"🩺 [routing-source-health] {title}\n\n{body}\n\n(signal: {signal_key})"
        return bool(_dm_program_management_lead(
            WorkflowService,
            text,
            organization_id=organization_id,
            subject_key=subject_key,
        ))
    except Exception:
        logger.exception('PLS-621: source-health DM failed for %s', signal_key)
        return False


# ── needs_triage / pre-triage starvation ─────────────────────────────────────


def on_pretriage_enter(item) -> Optional[dict]:
    """Called when an item enters needs_triage (or equivalent pre-triage)."""
    item_id = str(getattr(item, 'pk', '') or '')
    if not item_id:
        return None
    org = _org_key(item)
    team = (getattr(item, 'assignment_group', None) or 'unassigned').strip() or 'unassigned'
    priority = (getattr(item, 'priority', None) or 'normal').strip().lower()
    now = timezone.now()
    state = {
        'item_id': item_id,
        'item_key': getattr(item, 'item_key', None) or item_id,
        'org': org,
        'team': team,
        'priority': priority,
        'entered_at': now.isoformat(),
        'deadline_armed': False,
    }
    cache.set(_PRETRIAGE_KEY.format(item_id=item_id), state, timeout=None)

    depth_key = _PRETRIAGE_DEPTH.format(org=org, team=team[:80])
    try:
        depth = int(cache.get(depth_key) or 0) + 1
    except (TypeError, ValueError):
        depth = 1
    cache.set(depth_key, depth, timeout=None)

    logger.info(
        'metric:routing_source_health class=pretriage_enter item=%s org=%s team=%s '
        'priority=%s depth=%s',
        state['item_key'], org, team, priority, depth,
    )

    if priority in {'urgent', 'critical', 'highest'}:
        _emit_queue_starving_alert(
            org=org,
            team=team,
            item=item,
            reason='urgent_unowned_pretriage',
            depth=depth,
            age_h=0.0,
        )
    else:
        # Arm delayed deadline via Celery (event-driven, not a full-queue scan).
        try:
            from ..tasks import evaluate_pretriage_age_deadline
            delay_s = int(pretriage_age_threshold_hours() * 3600)
            evaluate_pretriage_age_deadline.apply_async(
                args=[item_id],
                countdown=max(1, delay_s),
            )
            state['deadline_armed'] = True
            cache.set(_PRETRIAGE_KEY.format(item_id=item_id), state, timeout=None)
        except Exception:
            logger.exception('PLS-621: failed to arm pretriage age deadline for %s', item_id)

    if depth >= pretriage_depth_threshold():
        _emit_queue_starving_alert(
            org=org,
            team=team,
            item=item,
            reason='pretriage_depth',
            depth=depth,
            age_h=0.0,
        )
    return state


def on_pretriage_exit(item) -> None:
    item_id = str(getattr(item, 'pk', '') or '')
    if not item_id:
        return
    key = _PRETRIAGE_KEY.format(item_id=item_id)
    state = cache.get(key) or {}
    cache.delete(key)
    if isinstance(state, dict):
        org = state.get('org') or _org_key(item)
        team = state.get('team') or (getattr(item, 'assignment_group', None) or 'unassigned')
        depth_key = _PRETRIAGE_DEPTH.format(org=org, team=str(team)[:80])
        try:
            depth = max(0, int(cache.get(depth_key) or 0) - 1)
        except (TypeError, ValueError):
            depth = 0
        cache.set(depth_key, depth, timeout=None)
        logger.info(
            'metric:routing_source_health class=pretriage_exit item=%s org=%s team=%s depth=%s',
            getattr(item, 'item_key', item_id), org, team, depth,
        )


def evaluate_pretriage_deadline_for_item(item_id: str) -> bool:
    """Celery deadline callback — fire only if still in pre-triage."""
    key = _PRETRIAGE_KEY.format(item_id=str(item_id))
    state = cache.get(key)
    if not isinstance(state, dict):
        return False
    try:
        from ..models import Item
        item = Item.objects.filter(pk=item_id).first()
    except Exception:
        logger.exception('PLS-621: pretriage deadline load failed for %s', item_id)
        return False
    if item is None:
        cache.delete(key)
        return False
    status = (getattr(item, 'status', None) or getattr(item, 'workflow_status', None) or '')
    status = str(status).strip().lower()
    if status not in {'needs_triage', 'awaiting_triage'}:
        on_pretriage_exit(item)
        return False
    entered = state.get('entered_at')
    age_h = 0.0
    if entered:
        try:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(entered)
            if dt is not None:
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.utc)
                age_h = (timezone.now() - dt).total_seconds() / 3600.0
        except Exception:
            age_h = pretriage_age_threshold_hours()
    if age_h + 1e-6 < pretriage_age_threshold_hours():
        return False
    _emit_queue_starving_alert(
        org=state.get('org') or _org_key(item),
        team=state.get('team') or (item.assignment_group or 'unassigned'),
        item=item,
        reason='pretriage_age',
        depth=1,
        age_h=age_h,
    )
    return True


def _emit_queue_starving_alert(
    *,
    org: str,
    team: str,
    item,
    reason: str,
    depth: int,
    age_h: float,
) -> bool:
    signal_key = f"queue-starving:{org}:{team}:{reason}"
    title = (
        f"[queue_starving] team={team} reason={reason} "
        f"item={getattr(item, 'item_key', '')} depth={depth}"
    )
    body = (
        f"**Class:** `queue_starving` (not source_unhealthy, not whole-team outage).\n\n"
        f"Pre-triage / unowned work needs attention on team `{team}` (org={org}).\n"
        f"- reason: `{reason}`\n"
        f"- sample item: `{getattr(item, 'item_key', '')}` "
        f"priority=`{getattr(item, 'priority', '')}`\n"
        f"- depth≈{depth}, age≈{age_h:.1f}h "
        f"(age threshold {pretriage_age_threshold_hours():.1f}h, "
        f"depth threshold {pretriage_depth_threshold()})\n\n"
        f"Route via normal Pulse workflow. Do not create a new Pulse task for this "
        f"signal (PLS-621 DM-only)."
    )
    return _emit_dm_once(
        signal_key,
        title=title,
        body=body,
        kind='queue_starving',
        organization_id=org,
        subject_key=str(team),
    )


# ── transition hook ──────────────────────────────────────────────────────────


def observe_transition(
    item,
    *,
    original_status: str,
    resolved_status: str,
    resolution_slug: str = '',
    actor: str = '',
) -> None:
    """Best-effort hook from WorkflowService.execute_transition."""
    try:
        old = (original_status or '').strip().lower()
        new = (resolved_status or '').strip().lower()
        res = (resolution_slug or '').strip().lower()
        if not res and getattr(item, 'resolution', None) is not None:
            res = (getattr(item.resolution, 'slug', None) or '').strip().lower()

        # Pre-triage enter/exit
        if new in {'needs_triage', 'awaiting_triage'} and old not in {
            'needs_triage', 'awaiting_triage',
        }:
            on_pretriage_enter(item)
        elif old in {'needs_triage', 'awaiting_triage'} and new not in {
            'needs_triage', 'awaiting_triage',
        }:
            on_pretriage_exit(item)

        # False disposition of detector artifacts (never counts a real fix).
        is_false_res = res in FALSE_DISPOSITION_SLUGS
        is_cancel_status = new in {'cancelled', 'canceled', 'rejected'}
        if _is_detector_artifact(item) and (is_false_res or is_cancel_status):
            record_false_disposition(
                item,
                resolution_slug=res or new,
                status_slug=new,
                actor=actor,
            )
    except Exception:
        logger.exception(
            'PLS-621: observe_transition failed for %s',
            getattr(item, 'item_key', getattr(item, 'pk', '?')),
        )


def prometheus_lines() -> list[str]:
    """Optional exposition fragments for /metrics consumers."""
    lines = []
    for series in list_unhealthy_series():
        det = str(series.get('detector') or '').replace('"', '')
        org = str(series.get('organization') or '').replace('"', '')
        sub = str(series.get('subject') or '').replace('"', '')
        count = int(series.get('count') or 0)
        lines.append(
            f'pulse_routing_source_unhealthy_dispositions{{detector="{det}",'
            f'organization="{org}",subject="{sub}"}} {count}'
        )
    return lines
