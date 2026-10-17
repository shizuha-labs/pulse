"""PLAT-5928 — post-commit wake (accelerator ONLY, never acceptance).

The durable acceptance lives in Postgres. These helpers wake the relay after a
commit and are explicitly non-authoritative: broker/Redis loss, flush or TTL
expiry has zero correctness effect — the due-row recovery relay drains from
Postgres regardless.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def post_commit_wake(report: dict) -> None:
    """Fire a best-effort wake after a durable acceptance commit."""
    if not report or not report.get('outbox'):
        return
    try:
        from django.db import transaction

        def _wake():
            _publish_wake(len(report.get('outbox', 0)))

        transaction.on_commit(_wake)
    except Exception:  # noqa: BLE001 — bounded telemetry
        logger.debug('alert wake registration failed', exc_info=True)


def _publish_wake(count: int) -> None:
    try:
        import os
        url = os.environ.get('PULSE_REDIS_URL') or 'redis://localhost:6379/0'
        import redis
        client = redis.Redis.from_url(url, decode_responses=True,
                                      socket_timeout=2, socket_connect_timeout=2)
        client.publish('pulse:alert_delivery', str(count))
    except Exception:  # noqa: BLE001 — best effort only
        logger.debug('alert wake publish failed', exc_info=True)
