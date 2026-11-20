"""PLS-1507 shadow counters for the assignment sweeps. No behaviour change.

The legacy sweeps still shed and re-push exactly as before. Beside each
decision this records what queue-mode (PLS-1484) would have done instead, so
the P4 pilot has a baseline before any team is flipped.

Queue-mode keeps claimed ownership: a sweep never sheds a live owner, and a
push onto a seat already at its ready cap is refused (``claim_at_capacity``).
Urgent stays cap-exempt on the pull side (operator rule 2026-09-27), matching
``_urgent_is_protected``.

Counts live in the cache, not Prometheus client state, because the sweeps run
in Celery workers and ``/api/metrics/`` is rendered by the web process.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CACHE_KEY = 'pulse:assignment_shadow:counts'

# Closed vocabulary. A new cause is a code change, never a free-form label.
CAUSES = (
    'wip_shed',
    'queue_assign',
)
VERDICTS = (
    'keep_ownership',
    'admit',
    'claim_at_capacity',
)


def _cache():
    from django.core.cache import cache
    return cache


def record_shadow_verdict(cause: str, verdict: str, team: str = '') -> None:
    """Increment one shadow counter. Never raises into the sweep."""
    if cause not in CAUSES or verdict not in VERDICTS:
        logger.warning('assignment shadow: ignored unknown cause=%s verdict=%s', cause, verdict)
        return
    team_label = (team or 'none').strip().lower() or 'none'
    field = f'{cause}|{verdict}|{team_label}'
    try:
        cache = _cache()
        current = cache.get(CACHE_KEY) or {}
        if not isinstance(current, dict):
            current = {}
        current[field] = int(current.get(field, 0)) + 1
        cache.set(CACHE_KEY, current, timeout=None)
    except Exception:
        logger.warning('assignment shadow: record failed', exc_info=True)


def render_assignment_shadow_metrics() -> str:
    """Prometheus exposition. Zero-filled for the closed vocabulary so a
    missing series is distinguishable from a zero one; team labels stay
    whatever the sweeps actually touched."""
    try:
        current = _cache().get(CACHE_KEY) or {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        logger.warning('assignment shadow: read failed', exc_info=True)
        current = {}
    lines = [
        '# HELP pulse_shadow_would_keep_owner_total Queue-mode verdict for a legacy sweep decision (PLS-1507). No action taken.',
        '# TYPE pulse_shadow_would_keep_owner_total counter',
    ]
    seen = set()
    for field, count in sorted(current.items()):
        try:
            cause, verdict, team = field.split('|', 2)
        except ValueError:
            continue
        if cause not in CAUSES or verdict not in VERDICTS:
            continue
        team = team.replace('"', '').replace('\\', '')
        lines.append(
            f'pulse_shadow_would_keep_owner_total{{cause="{cause}",verdict="{verdict}",team="{team}"}} {int(count)}'
        )
        seen.add((cause, verdict))
    for cause in CAUSES:
        for verdict in VERDICTS:
            if (cause, verdict) not in seen:
                lines.append(
                    f'pulse_shadow_would_keep_owner_total{{cause="{cause}",verdict="{verdict}",team="none"}} 0'
                )
    return '\n'.join(lines) + '\n'
