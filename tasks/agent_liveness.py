"""PLS-494 (aoi Architecture contract): the single canonical agent-liveness path.

The failover-drain protection (PLS-204) reads a short-TTL Redis marker
``pulse:agent_hb:<alias>`` as proof that a task owner made a *committed
authenticated Pulse request* recently, and uses it to hold a currently-owned
item against a soft provider/capacity/offline drain for a bounded grace.

For that evidence to be trustworthy the WRITE must be as reliable as the
authenticated action itself:

* fired at the request/activity boundary for the **resolved actor**, NOT gated
  on the best-effort JWT ``is_agent`` claim (a real authenticated agent mutation
  can exist while that claim is missing -> the split-brain aoi identified);
* **alias-folded** with the SAME helper the read side uses, so a write under one
  address form is found under the other;
* bounded-TTL and idempotent (a plain ``SETEX`` per alias);
* on storage failure, **diagnosably observable** (WARNING log + process counter)
  rather than silently swallowed -- a missing marker must degrade to hard-off
  drain semantics, never silently contradict a committed authenticated action.

This module has NO Django-model / service imports so it is safe to import from
the authentication boundary without a circular import.
"""
import json
import logging
import os
import threading
import time

try:
    import fcntl
except ImportError:  # non-POSIX; the file counter degrades to process-local.
    fcntl = None

logger = logging.getLogger(__name__)

# Marker-write failures are recorded in a Redis-INDEPENDENT, file-backed counter
# (the failure being measured is Redis itself being unreachable, so the durable
# health evidence must NOT live in Redis). A cross-worker flock keeps the count
# accurate across gunicorn workers; a process-local integer is the last-ditch
# fallback if the file is unusable. Surfaced at /metrics via
# ``render_agent_liveness_metrics`` (revi PLAT-1254 / aoi PLS-494 contract).
_metrics_lock = threading.Lock()
_HB_WRITE_FAILURES = 0  # process-local fallback only
_HB_METRICS_PATH = os.environ.get(
    'PULSE_AGENT_LIVENESS_METRICS_PATH', '/tmp/pulse-agent-liveness-metrics.json')
_HB_METRICS_LOCK_PATH = _HB_METRICS_PATH + '.lock'


def _bump_hb_write_failure():
    """Durably record one marker-write failure. Never raises."""
    global _HB_WRITE_FAILURES
    with _metrics_lock:
        if fcntl is None:
            _HB_WRITE_FAILURES += 1
            return
        try:
            with open(_HB_METRICS_LOCK_PATH, 'a+', encoding='utf-8') as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    with open(_HB_METRICS_PATH, encoding='utf-8') as f:
                        snap = json.load(f)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    snap = {}
                if not isinstance(snap, dict):
                    snap = {}
                snap['write_failures'] = int(snap.get('write_failures', 0)) + 1
                snap['last_failure_ts'] = time.time()
                tmp = f'{_HB_METRICS_PATH}.{os.getpid()}.{threading.get_ident()}'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(snap, f, sort_keys=True, separators=(',', ':'))
                os.replace(tmp, _HB_METRICS_PATH)
            _HB_WRITE_FAILURES += 1
        except (OSError, TypeError, ValueError):
            # Telemetry may degrade to a process-local counter, but a marker-write
            # failure must never change auth behavior.
            _HB_WRITE_FAILURES += 1


def _hb_metrics_snapshot():
    with _metrics_lock:
        if fcntl is not None:
            try:
                with open(_HB_METRICS_LOCK_PATH, 'a+', encoding='utf-8') as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                    with open(_HB_METRICS_PATH, encoding='utf-8') as f:
                        snap = json.load(f)
                if isinstance(snap, dict):
                    return snap
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                pass
        return {'write_failures': _HB_WRITE_FAILURES, 'last_failure_ts': 0}


def render_agent_liveness_metrics() -> str:
    """Prometheus text for the agent-liveness marker-write failure path — the
    durable, queriable health surface (Redis-independent, since Redis being
    unreachable is the failure being counted). AlertManager fires the
    deduplicated owner notification off this series."""
    snap = _hb_metrics_snapshot()
    fails = int(snap.get('write_failures', 0) or 0)
    last = float(snap.get('last_failure_ts', 0) or 0)
    return (
        '# HELP pulse_agent_hb_write_failures_total Agent-liveness marker writes'
        ' that failed (e.g. Redis unreachable).\n'
        '# TYPE pulse_agent_hb_write_failures_total counter\n'
        f'pulse_agent_hb_write_failures_total {fails}\n'
        '# HELP pulse_agent_hb_write_last_failure_timestamp_seconds Unix time of'
        ' the last marker-write failure.\n'
        '# TYPE pulse_agent_hb_write_last_failure_timestamp_seconds gauge\n'
        f'pulse_agent_hb_write_last_failure_timestamp_seconds {last}\n'
    )


def canonical_agent_email(email):
    """Fold an agent address to its canonical ``@shizuha.com`` form."""
    email = (email or '').strip().lower()
    if email.endswith('@agents.shizuha.io'):
        return email[:-len('@agents.shizuha.io')] + '@shizuha.com'
    return email


def agent_email_aliases(email):
    """Every marker address form for ``email`` (canonical + agents alias)."""
    canonical = canonical_agent_email(email)
    if not canonical:
        return set()
    aliases = {canonical}
    if canonical.endswith('@shizuha.com'):
        aliases.add(canonical[:-len('@shizuha.com')] + '@agents.shizuha.io')
    return aliases


def heartbeat_ttl_seconds():
    # TTL must exceed the SLOWEST agent heartbeat interval so an idle-but-alive
    # agent never expires between heartbeats (codex-bridge fires hourly).
    try:
        from django.conf import settings
        return int(os.environ.get('AGENT_HEARTBEAT_TTL_SEC')
                   or getattr(settings, 'AGENT_HEARTBEAT_TTL_SEC', 5400))
    except Exception:
        return 5400


def hb_write_failure_count():
    """Durable observed marker-write failure count (file-backed, Redis-independent)."""
    return int(_hb_metrics_snapshot().get('write_failures', 0) or 0)


# Marker writes ride the auth hot path: keep timeouts tight, but not so tight
# that a single Redis RTT blip (GC pause, brief failover) pages PLS-494 and
# drops a LIVE owner's liveness evidence. Match tasks/liveness.py (2s) rather
# than the historical 1s, and retry once before counting a durable failure.
_HB_REDIS_SOCKET_TIMEOUT = 2
_HB_WRITE_ATTEMPTS = 2
_HB_WRITE_RETRY_SLEEP_S = 0.05
# PLS-494: a pooled/idle connection that died silently (NAT expiry, Redis HA
# failover, conntrack drop) otherwise surfaces as network:TimeoutError on the
# auth hot path and drops a LIVE owner's liveness marker. health_check_interval
# makes redis-py PING a connection idle longer than this before reuse, so dead
# connections are detected and replaced instead of failing the write;
# socket_keepalive keeps long-lived pool entries alive through NAT; the
# module-level singleton reuses one pool instead of opening a fresh TCP
# connection per marker write.
_HB_REDIS_HEALTH_CHECK_INTERVAL_S = 30

_HB_REDIS_CLIENT = None


def _redis_client():
    global _HB_REDIS_CLIENT
    if _HB_REDIS_CLIENT is not None:
        return _HB_REDIS_CLIENT
    import redis as _redis
    from django.conf import settings
    _HB_REDIS_CLIENT = _redis.Redis.from_url(
        settings.CELERY_BROKER_URL, decode_responses=True,
        socket_connect_timeout=_HB_REDIS_SOCKET_TIMEOUT,
        socket_timeout=_HB_REDIS_SOCKET_TIMEOUT,
        socket_keepalive=True,
        health_check_interval=_HB_REDIS_HEALTH_CHECK_INTERVAL_S,
        retry_on_timeout=True,
    )
    return _HB_REDIS_CLIENT


def _write_agent_hb_markers(aliases, ttl, now):
    """SETEX pulse:agent_hb:<alias> for every alias. Raises on Redis errors."""
    rc = _redis_client()
    pipe = rc.pipeline()
    for alias in aliases:
        pipe.setex(f'pulse:agent_hb:{alias}', ttl, now)
    pipe.execute()


def record_agent_heartbeat(email, *, source='auth'):
    """Refresh the canonical liveness marker for every alias of an agent email.

    Returns True on a successful write, False otherwise. NEVER raises (auth must
    not break), but a failure is logged AND counted so the split-brain is
    observable instead of silent. Only agent-domain (``@shizuha.com`` canonical)
    identities are recorded -- external/human accounts are never drain subjects,
    so writing an ``agent_hb`` marker for them would be inert noise.

    Transient Redis errors (timeouts, brief disconnects) get one short retry
    before the durable PLS-494 failure counter is bumped — a single blip must
    not drop a LIVE owner's marker or page on-call.
    """
    canonical = canonical_agent_email(email)
    if not canonical or not canonical.endswith('@shizuha.com'):
        return False
    aliases = agent_email_aliases(canonical)
    ttl = heartbeat_ttl_seconds()
    # Float timestamp preserves sub-second event ordering against cached
    # lifecycle snapshots; the read side accepts historical integer values
    # as floats too.
    now = repr(time.time())
    last_exc = None
    for attempt in range(1, _HB_WRITE_ATTEMPTS + 1):
        try:
            _write_agent_hb_markers(aliases, ttl, now)
            if attempt > 1:
                logger.info(
                    'pulse_agent_hb_write_recovered source=%s email=%s '
                    'attempt=%s/%s',
                    source, canonical, attempt, _HB_WRITE_ATTEMPTS,
                )
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < _HB_WRITE_ATTEMPTS:
                logger.debug(
                    'pulse_agent_hb_write_retry source=%s email=%s '
                    'attempt=%s/%s err=%r',
                    source, canonical, attempt, _HB_WRITE_ATTEMPTS, exc,
                )
                time.sleep(_HB_WRITE_RETRY_SLEEP_S)
                continue
            break
    _bump_hb_write_failure()
    logger.warning(
        'pulse_agent_hb_write_failed source=%s email=%s aliases=%s '
        'attempts=%s err=%r -- liveness marker NOT refreshed; the failover '
        'drain falls back to hard-off semantics for this actor '
        '(PLS-494 observability contract)',
        source, canonical, sorted(aliases), _HB_WRITE_ATTEMPTS, last_exc,
    )
    return False


_FAILBACK_PENDING_PREFIX = 'pulse:agent_failback_pending:'
_FAILBACK_PROBE_PREFIX = 'pulse:agent_failback_probe:'


def mark_agent_failback_pending(email):
    """Arm the recovery event for one source owner after an auto-failover.

    The marker is deliberately Redis-ephemeral: it is only a rendezvous between
    the durable failover receipt on the Item and the owner's *next* successful
    authenticated request.  The Item receipt remains the authoritative CAS
    record; losing this hint can at worst defer failback to an explicit healthy
    report, never corrupt ownership.
    """
    canonical = canonical_agent_email(email)
    if not canonical or not canonical.endswith('@shizuha.com'):
        return False
    try:
        grace = int(os.environ.get('PULSE_FAILOVER_DRAIN_GRACE_SEC', '1200'))
    except (TypeError, ValueError):
        grace = 1200
    try:
        client = _redis_client()
        # A new failover generation must not inherit the one-minute claim from
        # an earlier, already-restored generation of the same owner.
        client.delete(f'{_FAILBACK_PROBE_PREFIX}{canonical}')
        client.setex(
            f'{_FAILBACK_PENDING_PREFIX}{canonical}',
            max(60, grace),
            '1',
        )
        return True
    except Exception as exc:
        logger.warning(
            'pulse_agent_failback_pending_write_failed email=%s err=%r; '
            'the durable receipt remains available to an explicit recovery event',
            canonical, exc,
        )
        return False


def claim_agent_failback_probe(email):
    """Claim one throttled, event-driven failback probe when work is pending.

    Multiple concurrent requests by a recovered agent may all observe the same
    pending marker.  ``SET NX`` bounds them to one Celery dispatch per minute;
    the restoration task itself is row-locked and idempotent as the final guard.
    """
    canonical = canonical_agent_email(email)
    if not canonical or not canonical.endswith('@shizuha.com'):
        return False
    try:
        client = _redis_client()
        if not client.get(f'{_FAILBACK_PENDING_PREFIX}{canonical}'):
            return False
        return bool(client.set(
            f'{_FAILBACK_PROBE_PREFIX}{canonical}',
            '1',
            nx=True,
            ex=60,
        ))
    except Exception as exc:
        logger.warning(
            'pulse_agent_failback_probe_claim_failed email=%s err=%r; '
            'a later authenticated request may retry',
            canonical, exc,
        )
        return False


def clear_agent_failback_pending(email):
    """Clear the ephemeral rendezvous marker after a successful failback."""
    canonical = canonical_agent_email(email)
    if not canonical:
        return False
    try:
        _redis_client().delete(
            f'{_FAILBACK_PENDING_PREFIX}{canonical}',
            f'{_FAILBACK_PROBE_PREFIX}{canonical}',
        )
        return True
    except Exception:
        return False
