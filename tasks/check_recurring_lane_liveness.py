"""PLS-1286: meta-watchdog for the recurring-cycle lane itself.

The PLAT-528 failure shape: the recurring scheduler stopped firing entirely
(suspected scheduler/cron lane outage during the 09-10 -> 09-17 fleet turmoil)
and six consecutive periods accrued with no owner-visible signal — the only
recovery was the period-6 escalation bounce. The per-item escalation lane
cannot see a DEAD lane (it runs inside the lane), so the watchdog must live
outside it: this command reads the lane liveness watermark written by
``tasks.services.recurring_cycle_state.record_lane_liveness`` on every cycle
processing (fire or skip) and alarms when the lane has been silent for longer
than the configured window.

Run periodically (registered as an hourly celery-beat periodic task from
``tasks.apps.ready``). Fail-loud: a dead recurring lane is a fleet-wide
reliability defect, not a per-item warning.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from tasks.services.recurring_cycle_state import LANE_LIVENESS_CACHE_KEY

logger = logging.getLogger(__name__)

# Default: 8 days — longer than the longest common cadence (weekly) so a
# healthy lane never alarms, but a dead lane alarms within ~1 period of the
# next weekly boundary after the watermark goes stale.
DEFAULT_MAX_AGE_HOURS = 192


def _max_age_hours() -> int:
    raw = getattr(settings, 'RECURRING_LANE_LIVENESS_MAX_AGE_HOURS', None)
    try:
        return max(1, int(raw)) if raw is not None else DEFAULT_MAX_AGE_HOURS
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_HOURS


def _alarm(message: str) -> None:
    """Fail-loud alarm: log + Connect system message to the platform owner."""
    logger.error('metric:recurring_cycle_lane_dead %s', message)
    try:
        import requests as req
        # PLS-1291 review fix: system_message_headers lives in
        # tasks.services.connect_origin (the hardened internal-system-message
        # client), not notifications — the wrong-module import made every
        # watchdog alarm fail-soft into a dead alarm, the exact failure mode
        # this watchdog exists to prevent.
        from tasks.services.connect_origin import system_message_headers
        connect_base_url = getattr(
            settings, 'SHIZUHA_CONNECT_API_URL',
            'http://shizuha-connect:8019/api',
        ).rstrip('/')
        body = {
            'recipient_username': 'goro',
            'content': (
                '[recurring-cycle lane watchdog] ' + message +
                ' — the recurring scheduler lane appears dead (PLS-1286 / '
                'PLAT-528 shape). Inspect the pulse scheduler/cron lane; '
                'per-item escalations cannot fire while the lane is down.'
            ),
            'sender_name': 'Pulse',
            'sender_service': 'shizuha-pulse',
        }
        req.post(
            f'{connect_base_url}/internal/system-message/',
            json=body,
            headers=system_message_headers(body),
            timeout=5,
        )
    except Exception:
        logger.exception('recurring-cycle lane watchdog alarm delivery failed')


def check_recurring_lane_liveness(*, now: datetime | None = None) -> dict:
    """Check the lane watermark; alarm when the lane is silent past the window.

    Returns a diagnostic dict (also used by tests): {'state', 'age_hours',
    'max_age_hours', 'watermark'}.
    """
    now = now or timezone.now()
    watermark = cache.get(LANE_LIVENESS_CACHE_KEY)
    max_age = _max_age_hours()

    if not watermark:
        # No watermark yet: either a fresh deploy (the next cycle populates it)
        # or the cache was flushed. Warn, don't page — a missing watermark is
        # not proof of a dead lane.
        logger.warning(
            'metric:recurring_cycle_lane_liveness_missing — no watermark at %s; '
            'the next recurring cycle processing populates it', now.isoformat(),
        )
        return {'state': 'missing', 'age_hours': None,
                'max_age_hours': max_age, 'watermark': None}

    try:
        last_at = datetime.fromisoformat(watermark['at'])
        if timezone.is_naive(last_at):
            last_at = timezone.make_aware(last_at)
    except (KeyError, TypeError, ValueError):
        logger.warning('recurring-cycle lane watermark malformed: %r', watermark)
        return {'state': 'malformed', 'age_hours': None,
                'max_age_hours': max_age, 'watermark': watermark}

    age_hours = (now - last_at).total_seconds() / 3600.0
    if age_hours > max_age:
        _alarm(
            f'lane silent for {age_hours:.1f}h (max {max_age}h); last processed '
            f"{watermark.get('at')} kind={watermark.get('kind')} "
            f"item={watermark.get('item')}"
        )
        return {'state': 'dead', 'age_hours': age_hours,
                'max_age_hours': max_age, 'watermark': watermark}

    return {'state': 'alive', 'age_hours': age_hours,
            'max_age_hours': max_age, 'watermark': watermark}


def setup_recurring_lane_watchdog_schedule() -> None:
    """Register the hourly watchdog periodic task (called from apps.ready)."""
    try:
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
        hourly, _ = CrontabSchedule.objects.get_or_create(minute=15, hour='*')
        PeriodicTask.objects.get_or_create(
            name='Recurring Cycle Lane Watchdog (PLS-1286)',
            defaults={
                'crontab': hourly,
                'task': 'tasks.check_recurring_lane_liveness.check_recurring_lane_liveness_task',
                'enabled': True,
            },
        )
    except Exception:
        logger.exception('failed to register the recurring-cycle lane watchdog schedule')


@shared_task
def check_recurring_lane_liveness_task() -> dict:
    """Celery entrypoint for the hourly watchdog beat."""
    return check_recurring_lane_liveness()
