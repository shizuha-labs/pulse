"""Prometheus exposition: broker queue backlog (PLS-1416 remediation 2).

The ``fire_recurring_due`` enqueue flood (PLS-1416) grew to a 35k-message
backlog and was only detected via snapshot-staleness DMs — pure
queue-position latency starved every 10m-cadence sweep behind it. A read-only
broker depth gauge makes any recurrence visible in one scrape:

- ``pulse_broker_queue_backlog{queue}`` — ``LLEN`` of the default celery
  queue (``settings.CELERY_TASK_DEFAULT_QUEUE``), sampled per exposition.
- ``pulse_broker_redis_error`` — set to 1 when the broker probe itself fails,
  so a blind gauge is distinguishable from a healthy empty queue (same
  blindness convention as ``pulse_recurring_redis_error``).

Read-only by construction (``LLEN`` only); no item keys, payload, or creds in
the exposition.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def collect_broker_backlog_metrics(redis_client=None):
    """Return (queue_name, depth, blind) for the broker backlog gauge."""
    from django.conf import settings

    queue = getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "shizuha_pulse")
    if redis_client is None:
        import redis

        redis_client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL, decode_responses=True
        )
    try:
        depth = int(redis_client.llen(queue) or 0)
    except redis.RedisError:
        logger.warning("broker backlog probe failed for queue %s", queue, exc_info=True)
        return queue, 0, True
    return queue, depth, False


def render_broker_backlog_metrics() -> str:
    try:
        queue, depth, blind = collect_broker_backlog_metrics()
    except Exception:
        logger.warning("broker backlog metrics collection failed", exc_info=True)
        return ''
    if blind:
        return 'pulse_broker_redis_error 1\n'
    return (
        '# broker queue depth (PLS-1416 remediation 2)\n'
        f'pulse_broker_queue_backlog{{queue="{queue}"}} {depth}\n'
        'pulse_broker_redis_error 0\n'
    )
