"""Mechanism-liveness heartbeats (Redis-backed).

PLAT-1295 (PLAT-1279 follow-up; PLAT-1254 fail-loud). The mechanism-liveness
meta-probe seeded the `recurring-engine-andon` and `pulse-invariant-validator`
entries on `k8s-deployment-available` — a host-up proxy that CANNOT see a
silently-dead mechanism on a healthy pod. That is exactly the PLS-210 failure
class: a 451-hour silent gap while shizuha-pulse itself was UP, so the proxy
would not have caught it.

These heartbeats emit a real, per-mechanism last-fired timestamp so the probe
can alert on staleness even when the pod is healthy. The recurring-engine loop
and the write-time invariant validator run in DIFFERENT processes (scheduler vs
web/worker), so the store must be shared: Redis, not an in-process gauge. The
timestamps are exposed as JSON at ``/statusz`` for an http-json-age signal.

Every write/read is best-effort and never raises — a liveness-store hiccup must
never break a task write or stall the scheduler loop.
"""
import json
import logging
import os
import time

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = 'pulse:liveness'

# Mechanism identifiers — stable strings; the /statusz keys and the deploy
# registry (mechanism-liveness-probe.yaml) reference these.
RECURRING_ANDON = 'recurring_andon'
INVARIANT_VALIDATOR = 'invariant_validator'
DEPLOY_VERIFY_GATE = 'deploy_verify_gate'
# PLS-675 / Rei P1: SUCCESS-path only. Pending/failed merge-event retries must
# NOT refresh this key — they write the per-task failure hash instead (see
# record_merge_event_retry_failure). A heartbeat here means the gate path ran
# a successful apply. Clean no-op sweeps with zero pending failures may also
# refresh it; pending failures keep the failure artifact authoritative.
DEPLOY_VERIFY_MERGE_EVENT = 'deploy_verify_merge_event'
STALE_SWEEP = 'stale_sweep'
OPERATOR_WAIT = 'operator_wait'
WIP_ADMISSION = 'wip_admission'
ROSTER_RECONCILIATION = 'roster_reconciliation'
ZERO_MEMBER_TEAM_DETECTOR = 'zero_member_team_detector'
MECHANISMS = (
    RECURRING_ANDON,
    INVARIANT_VALIDATOR,
    DEPLOY_VERIFY_GATE,
    DEPLOY_VERIFY_MERGE_EVENT,
    STALE_SWEEP,
    OPERATOR_WAIT,
    WIP_ADMISSION,
    ROSTER_RECONCILIATION,
    ZERO_MEMBER_TEAM_DETECTOR,
)

# Per-task failure hash (Rei P2): field = item_key, value = JSON payload.
# Unrelated successes must NOT clear other tasks' pending failures.
MERGE_EVENT_RETRY_FAILURES_HASH = '%s:merge_event_retry_failures' % KEY_PREFIX
# Legacy single-key artifacts (migrated on read, deleted on clear-all paths).
MERGE_EVENT_RETRY_FAILURE_KEY = '%s:merge_event_retry_last_failure' % KEY_PREFIX
MERGE_EVENT_RETRY_PENDING_KEY = '%s:merge_event_retry_pending_count' % KEY_PREFIX

_client = None


def _redis_client():
    global _client
    if _client is None:
        url = (getattr(settings, 'CELERY_BROKER_URL', None)
               or os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
        _client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
        )
    return _client


def _key(name):
    return '%s:%s' % (KEY_PREFIX, name)


def record_heartbeat(name, *, client=None, now=None):
    """Best-effort: store the last-fired unix ts for mechanism ``name``.

    Returns the timestamp written. Never raises: a liveness-store failure must
    not break the mechanism whose liveness we are recording.
    """
    ts = time.time() if now is None else now
    try:
        (client or _redis_client()).set(_key(name), repr(ts))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('liveness heartbeat write failed for %s: %s', name, exc)
    return ts


def read_heartbeats_status(names=MECHANISMS, *, client=None):
    """Return ``({name: float ts or None}, store_error_or_None)``. Never raises.

    PLS-778. ``read_heartbeats`` alone cannot express the difference between
    "no mechanism has ever fired" and "the liveness store is unreachable" —
    both render as an all-null dict with identical keys. That ambiguity made
    the PLS-766 all-null ``/statusz`` reading unattributable after the fact,
    inverting this module's purpose: a silently-dead STORE was reported as
    silently-dead MECHANISMS.

    The second element is ``None`` when the store was read successfully (so an
    all-null result genuinely means nothing has fired) and a non-empty string
    describing the failure otherwise. Callers that surface liveness to an
    operator or a probe MUST use this and report the store state alongside the
    timestamps.
    """
    result = {name: None for name in names}
    try:
        c = client or _redis_client()
        for name in names:
            raw = c.get(_key(name))
            if raw is None:
                continue
            try:
                result[name] = float(raw)
            except (TypeError, ValueError):
                result[name] = None
    except Exception as exc:
        # Fail loud: an unreachable store is an operator-visible condition, not
        # a debug detail. Downgrading this to debug is what made the all-null
        # reading silent (PLS-778).
        logger.warning('liveness heartbeat read failed: %s: %s', type(exc).__name__, exc)
        return result, '%s: %s' % (type(exc).__name__, exc)
    return result, None


def read_heartbeats(names=MECHANISMS, *, client=None):
    """Return ``{name: float ts or None}`` for each mechanism. Never raises.

    Retained for callers that only need the timestamps. Anything that reports
    liveness outward should call :func:`read_heartbeats_status` instead, so an
    unreachable store is not silently rendered as dead mechanisms (PLS-778).
    """
    result, _error = read_heartbeats_status(names, client=client)
    return result


def record_merge_event_retry_failure(*, item_key='', error='', client=None, now=None):
    """Record a stranded merge-event retry WITHOUT refreshing success liveness.

    PLS-675 / Rei P2: failures are per-task. An unrelated success on task C must
    not erase pending failures for A/B. Returns the failure payload dict.

    PLAT-8195/PLAT-8342 andon: this write IS the SWEEP-1 fail-loud evidence leg
    (the artifact ``read_merge_event_retry_failure`` asserts on). A silent loss
    here is exactly the false-pass the detector exists to catch, so a transient
    store failure is retried with backoff and a final failure escalates to
    WARNING — never silent debug. The write is a pure store op (no DB
    transaction coupling); callers invoke it before the typed receipt mint so
    the evidence lands even if later steps raise.
    """
    ts = time.time() if now is None else now
    key = (item_key or '').strip()[:128] or '_unknown'
    payload = {
        'at': ts,
        'item_key': key,
        'error': (error or '')[:500],
        'state': 'pending',
    }
    last_exc = None
    for attempt in range(3):
        try:
            c = client or _redis_client()
            c.hset(MERGE_EVENT_RETRY_FAILURES_HASH, key, json.dumps(payload))
            # Keep a "latest" pointer for statusz summary compatibility.
            c.set(MERGE_EVENT_RETRY_FAILURE_KEY, json.dumps(payload))
            c.set(
                MERGE_EVENT_RETRY_PENDING_KEY,
                str(c.hlen(MERGE_EVENT_RETRY_FAILURES_HASH)),
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(0.2 * (attempt + 1))
    if last_exc is not None:
        logger.warning(
            'merge-event retry failure evidence WRITE failed after retries:'
            ' %s: %s (item_key=%s)',
            type(last_exc).__name__, last_exc, key,
        )
    return payload


def clear_merge_event_retry_failure(*, item_key=None, client=None):
    """Clear failure artifact(s) after a successful merge-event apply.

    When ``item_key`` is provided, only that task's pending failure is cleared.
    When omitted, clears the entire pending set (legacy callers / tests).
    """
    try:
        c = client or _redis_client()
        if item_key:
            key = (item_key or '').strip()[:128]
            if key:
                c.hdel(MERGE_EVENT_RETRY_FAILURES_HASH, key)
        else:
            c.delete(MERGE_EVENT_RETRY_FAILURES_HASH)
        pending = int(c.hlen(MERGE_EVENT_RETRY_FAILURES_HASH) or 0)
        if pending <= 0:
            c.delete(MERGE_EVENT_RETRY_FAILURE_KEY)
            c.delete(MERGE_EVENT_RETRY_PENDING_KEY)
            c.delete(MERGE_EVENT_RETRY_FAILURES_HASH)
        else:
            c.set(MERGE_EVENT_RETRY_PENDING_KEY, str(pending))
            # Refresh latest pointer from remaining hash fields.
            try:
                remaining = c.hgetall(MERGE_EVENT_RETRY_FAILURES_HASH) or {}
                latest = None
                latest_at = -1.0
                for raw in remaining.values():
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    at = float(data.get('at') or 0)
                    if at >= latest_at:
                        latest_at = at
                        latest = data
                if latest is not None:
                    c.set(MERGE_EVENT_RETRY_FAILURE_KEY, json.dumps(latest))
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('merge-event retry failure clear failed: %s', exc)


def read_merge_event_retry_failure(*, client=None):
    """Return a summary of pending merge-event retry failures, or None.

    Shape (statusz-compatible):
      {at, item_key, error, state, pending_count, pending_item_keys}
    Never raises.
    """
    try:
        c = client or _redis_client()
        pending_map = c.hgetall(MERGE_EVENT_RETRY_FAILURES_HASH) or {}
        # Migrate legacy single-key payload into the hash once.
        if not pending_map:
            raw = c.get(MERGE_EVENT_RETRY_FAILURE_KEY)
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get('item_key'):
                        key = str(data['item_key'])[:128]
                        c.hset(MERGE_EVENT_RETRY_FAILURES_HASH, key, raw)
                        pending_map = {key: raw}
                except Exception:
                    pass
        if not pending_map:
            return None
        latest = None
        latest_at = -1.0
        keys = []
        for field, raw in pending_map.items():
            keys.append(field)
            try:
                data = json.loads(raw)
            except Exception:
                data = {'item_key': field, 'state': 'pending', 'at': 0, 'error': ''}
            at = float(data.get('at') or 0)
            if at >= latest_at:
                latest_at = at
                latest = data
        if latest is None:
            return None
        latest = dict(latest)
        latest['pending_count'] = len(keys)
        latest['pending_item_keys'] = sorted(keys)
        latest.setdefault('state', 'pending')
        return latest
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            'merge-event retry failure READ failed: %s: %s',
            type(exc).__name__, exc,
        )
        return None
