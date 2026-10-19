"""In-boundary attachment-storage canary runner (PLS-483 option-b).

Runs the create/stat/read/validate/delete canary **inside the existing Pulse
trust boundary** (this worker already legitimately holds the AttachmentStorage
credentials), replacing the standalone exporter Deployment that received the
whole Django env (POSTGRES_PASSWORD / JWT_SECRET_KEY / MinIO / Connect origin —
ADR/HLD security review P1 #1). No self-minted token, no external identity, no
HTTP round-trip: the periodic task calls `run_attachment_storage_canary()`
directly and exposes bounded phase/reason metrics through Pulse's *existing*
`/metrics/` endpoint.

Safety properties (ichi's requirements):
- **Singleton / leader-safe across replicas** and **non-re-entrant**: a Redis
  `SET NX EX` lock (the repo convention, cf. tasks/scheduler.py, tasks/tasks.py)
  admits exactly one run at a time; a still-running tick holds the lock so the
  next tick is skipped and counted. Redis errors fail closed: no replica may
  execute the storage transaction without shared singleton ownership.
- **Hard bounded per-run timeout** with the typed `timeout` reason: the canary
  runs in a worker thread joined with a deadline; a timeout yields
  `reason="timeout"` (the service `_reason()` already maps `TimeoutError`).
- Metrics carry only bounded phase/reason — never keys, tokens, or object names.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from typing import Optional

from celery import shared_task
from django.conf import settings

from tasks.services.attachment_canary import (
    AttachmentCanaryFailure,
    PHASES,
    run_attachment_storage_canary,
)

LOG = logging.getLogger(__name__)

# Bounded reason set — anything else collapses to "unexpected" so a raw error
# string can never reach a metric label.
BOUNDED_REASONS = frozenset({
    "none", "configuration", "signature_mismatch", "invalid_access_key",
    "access_denied", "endpoint_mismatch", "bucket_mismatch", "bucket_missing",
    "region_mismatch", "storage_error", "timeout", "size_mismatch",
    "content_type_mismatch", "metadata_mismatch", "validation_failed",
    "content_mismatch", "cleanup_failed", "unexpected", "http_error",
})

_STATE_PATH = os.environ.get(
    "ATTACHMENT_CANARY_METRICS_PATH", "/tmp/pulse-attachment-canary-metrics.json")
_STATE_LOCK_PATH = _STATE_PATH + ".lock"
_state_lock = threading.Lock()

_LOCK_KEY = "pulse:attachment-canary:run-lock"

# PLS-483 (review P1 #1): the metric state must be AUTHORITATIVE ACROSS
# REPLICAS. Pulse runs 2–5 HPA pods and the ServiceMonitor scrapes every one;
# a pod-local file made only the writer pod emit real values while every other
# replica served defaults (last_run=0), so healthy production paged
# Failing/Stale on the non-writer pods. The single canary run (already
# serialized by the Redis run-lock) publishes its result to a shared Redis key
# that EVERY pod's /metrics renders, so all replicas expose identical
# authoritative values. If Redis is unreachable, the renderer omits the family
# so the independent Missing watchdog fires; it never serves a stale local value.
_STATE_REDIS_KEY = "pulse:attachment-canary:state"
# Keep the shared state well beyond the scrape/run cadence so it never expires
# between runs (a genuine outage surfaces via last_run staleness, not absence).
_STATE_REDIS_TTL_SECONDS = 3600


def _run_timeout_seconds() -> float:
    return float(getattr(settings, "ATTACHMENT_CANARY_RUN_TIMEOUT_SECONDS", 30))


def _fault_mode() -> str:
    # Bounded Git-owned drill control.  The direct setting is deployed through
    # the Pulse chart and can be changed/reverted by CI without mutating the live
    # MinIO credential.  A mounted fixture remains supported for local drills.
    configured = str(getattr(settings, "ATTACHMENT_CANARY_FAULT_MODE", "") or "").strip()
    if configured in ("", "none"):
        configured = ""
    if configured == "signature":
        return configured
    path = getattr(settings, "ATTACHMENT_CANARY_FAULT_MODE_PATH", "") or os.environ.get(
        "ATTACHMENT_CANARY_FAULT_MODE_PATH", "")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return ""
    return value if value in ("", "signature") else ""


def _empty_state() -> dict:
    return {
        "last_run": 0.0,
        "last_success": 0.0,
        "success": 0,
        "phase_success": {phase: 0 for phase in PHASES},
        "failure_phase": "none",
        "failure_reason": "none",
        "stale_removed": 0,
    }


def _state_from_result(result: dict, now: float) -> dict:
    ok = bool(result.get("ok"))
    phase_values = result.get("phases") if isinstance(result.get("phases"), dict) else {}
    phase = str(result.get("phase") or "none")
    reason = str(result.get("reason") or "none")
    if phase not in PHASES and phase != "none":
        phase = "none"
    if reason not in BOUNDED_REASONS:
        reason = "unexpected"
    return {
        "last_run": now,
        "last_success": now if ok else _read_state().get("last_success", 0.0),
        "success": int(ok),
        "phase_success": {p: int(bool(phase_values.get(p))) for p in PHASES},
        "failure_phase": "none" if ok else phase,
        "failure_reason": "none" if ok else reason,
        "stale_removed": max(0, int(result.get("stale_objects_removed") or 0)),
    }


def _redis_read_state() -> Optional[dict]:
    """Return the shared authoritative state from Redis, or None if unavailable."""
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_STATE_REDIS_KEY)
    except Exception:  # noqa: BLE001 — renderer treats this as unavailable
        return None
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _redis_write_state(state: dict) -> bool:
    """Publish the shared authoritative state to Redis. Returns success."""
    client = _get_redis()
    if client is None:
        return False
    try:
        client.set(
            _STATE_REDIS_KEY,
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            ex=_STATE_REDIS_TTL_SECONDS,
        )
        return True
    except Exception:  # noqa: BLE001 — caller records only local continuity
        return False


def _read_file_state() -> dict:
    with _state_lock:
        try:
            with open(_STATE_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                with open(_STATE_PATH, encoding="utf-8") as fh:
                    snapshot = json.load(fh)
            if isinstance(snapshot, dict):
                return snapshot
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
    return _empty_state()


def _read_state() -> dict:
    # Redis is the only externally rendered authority. The local file is merely
    # an internal continuity seed for preserving last_success if a later write
    # can reach Redis; render_attachment_canary_metrics never serves this file.
    shared = _redis_read_state()
    if shared is not None:
        return shared
    return _read_file_state()


def _write_state(state: dict) -> None:
    # Publish to the shared Redis key (authoritative for every replica's
    # /metrics) and mirror locally only as the non-rendered continuity seed.
    _redis_write_state(state)
    with _state_lock:
        try:
            with open(_STATE_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                tmp = f"{_STATE_PATH}.{os.getpid()}.{threading.get_ident()}"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, sort_keys=True, separators=(",", ":"))
                os.replace(tmp, _STATE_PATH)
        except (OSError, TypeError, ValueError):
            # Telemetry may degrade; it must never break the canary itself.
            LOG.warning("attachment canary: failed to persist metric state", exc_info=True)


def _run_once(fault_mode: str, budget: float) -> tuple[dict, bool]:
    """Run the canary in a worker thread bounded by `budget`.

    Returns ``(result, timed_out)``. On timeout the worker thread may still be
    executing the underlying storage operation (a Python thread cannot be
    force-killed); the bounded MinIO socket timeouts abort it shortly after, but
    the caller MUST keep holding the run-lock until then so no second tick can
    start a concurrent transaction (review P1 #2 — "no overlapping probes").
    """
    box: dict = {}

    def _target():
        try:
            box["result"] = run_attachment_storage_canary(fault_mode=fault_mode)
        except AttachmentCanaryFailure as failure:
            box["result"] = {
                "ok": False, "phase": failure.phase, "reason": failure.reason,
                "phases": failure.phases,
            }
        except Exception as error:  # noqa: BLE001 — bounded to a reason below
            box["result"] = {"ok": False, "phase": "none", "reason": "unexpected",
                             "phases": {}, "_error": type(error).__name__}

    worker = threading.Thread(target=_target, name="attachment-canary", daemon=True)
    worker.start()
    worker.join(timeout=budget)
    if worker.is_alive():
        # Hard bounded timeout — the run overran its budget.
        return {"ok": False, "phase": "none", "reason": "timeout", "phases": {}}, True
    return (
        box.get("result", {"ok": False, "phase": "none", "reason": "unexpected", "phases": {}}),
        False,
    )


def _get_redis():
    try:
        import redis  # noqa: PLC0415
        url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL", "")
        if not url:
            return None
        return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:  # noqa: BLE001 — caller fails closed without ownership
        return None



def _lock_ttl_seconds(budget: float) -> int:
    """How long a live probe may hold the singleton lock.

    Must outlive the join budget (so a timed-out thread is still covered) and
    stay far under the 300s beat interval (so one stuck holder cannot eat
    multiple schedules).
    """
    return max(int(budget) + 15, 1)


def _lock_token(now: float) -> str:
    # Unix seconds, not a pid. Pid-sized values must not parse as acquire times.
    return f"{now:.3f}"


def _parse_lock_acquired(raw) -> Optional[float]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 1_000_000_000:
        return None
    return value


def _lock_pttl_ms(client) -> Optional[int]:
    pttl = getattr(client, "pttl", None)
    if not callable(pttl):
        return None
    try:
        return int(pttl(_LOCK_KEY))
    except Exception:  # noqa: BLE001 — unknown TTL; caller must not guess
        return None


def _lock_is_stale(client, now: float, max_age: float) -> bool:
    """True when the held lock can no longer be a live in-budget probe."""
    try:
        raw = client.get(_LOCK_KEY)
    except Exception:  # noqa: BLE001
        return False
    if raw is None:
        return False
    acquired = _parse_lock_acquired(raw)
    if acquired is not None:
        return (now - acquired) > max_age
    # Legacy pid value: only an immortal key (PTTL < 0) is safe to steal.
    # A positive TTL still belongs to a live pre-PLS-1412 holder.
    pttl = _lock_pttl_ms(client)
    return pttl is not None and pttl < 0


def _acquire_run_lock(client, budget: float) -> bool:
    """SET NX the singleton lock, stealing a stale holder once.

    Raises on the initial SET so the tick can fail closed as
    redis_unavailable. A stale-steal error is treated as "still locked"
    rather than running unlocked.
    """
    ttl = _lock_ttl_seconds(budget)
    now = time.time()
    if client.set(_LOCK_KEY, _lock_token(now), nx=True, ex=ttl):
        return True
    if not _lock_is_stale(client, now, ttl):
        return False
    try:
        client.delete(_LOCK_KEY)
        return bool(client.set(
            _LOCK_KEY, _lock_token(time.time()), nx=True, ex=ttl))
    except Exception:  # noqa: BLE001
        return False


@shared_task(bind=True, max_retries=0, name="tasks.attachment_storage_canary")
def attachment_storage_canary_tick(self):  # noqa: ARG001 — celery bind
    """Periodic in-boundary canary tick. Singleton + non-re-entrant + bounded."""
    if not getattr(settings, "ATTACHMENT_CANARY_ENABLED", True):
        return {"skipped": "disabled"}

    budget = _run_timeout_seconds()
    redis_client = _get_redis()
    # PLS-483 (review P1 #1): FAIL CLOSED without Redis. The Redis SET NX EX lock
    # is the ONLY cross-replica singleton; running when Redis is unavailable would
    # let every one of the 2-5 HPA pods execute a storage transaction with no
    # single-flight ownership. Skip instead — the independent Missing/Stale
    # watchdog surfaces the gap fail-loud.
    if redis_client is None:
        return {"skipped": "redis_unavailable"}
    try:
        # TTL a touch beyond the run budget so an overrunning run keeps the lock
        # (no overlap) but a crashed run auto-releases. With the bounded canary
        # HTTP client the actual op aborts within a few seconds, inside this TTL.
        # PLS-1412: a lock with no expiry, or one whose acquire-time is already
        # older than that TTL, is stale. Leaving it in place skips every later
        # tick without writing last_run, so success=1 stays stuck for many
        # 300s intervals (schedule starvation) even though beat is healthy.
        lock_acquired = _acquire_run_lock(redis_client, budget)
    except Exception:  # noqa: BLE001 — no verifiable singleton ownership on a
        # Redis error; fail closed rather than run unlocked.
        return {"skipped": "redis_unavailable"}
    if not lock_acquired:
        # Another replica/tick is already running — skip, non-re-entrant.
        return {"skipped": "locked"}

    timed_out = False
    try:
        result, timed_out = _run_once(_fault_mode(), budget)
        _write_state(_state_from_result(result, time.time()))
        return {"ok": bool(result.get("ok")), "reason": result.get("reason", "none")}
    finally:
        # Release the singleton lock ONLY on a clean finish. On a timeout the
        # worker thread may still be running the storage operation, so we keep
        # the lock and let its TTL (budget+15s, > the MinIO socket timeout)
        # expire — this guarantees no second tick starts a concurrent
        # transaction while the timed-out one is still live (review P1 #2).
        if lock_acquired and redis_client is not None and not timed_out:
            try:
                redis_client.delete(_LOCK_KEY)
            except Exception:  # noqa: BLE001
                pass  # TTL will release it


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_attachment_canary_metrics() -> str:
    """Render the bounded canary series for Pulse's existing /metrics/ endpoint.

    When the producer is disabled/unregistered the family is OMITTED entirely so
    the Prometheus `absent(...)` Missing alert fires and the detector cannot
    silently self-disable (review P1 #3). A disabled canary must be *loud*, not a
    stale gauge that reads healthy while nothing is exercising storage.
    """
    if not getattr(settings, "ATTACHMENT_CANARY_ENABLED", True):
        return ""
    # PLS-483 (review P1 #2): render from the SHARED AUTHORITATIVE Redis state so
    # every replica emits identical values (no per-pod local snapshot to diverge,
    # so the rules need no cross-instance aggregation and a real success=0 can
    # never be masked by a stale success=1 replica). If the authoritative state
    # is unavailable (Redis unreachable / no run yet) OMIT the family so the
    # Missing watchdog surfaces it fail-loud instead of a possibly-stale file.
    s = _redis_read_state()
    if s is None:
        return ""
    phase_success = s.get("phase_success", {})
    failure_phase = str(s.get("failure_phase", "none"))
    failure_reason = str(s.get("failure_reason", "none"))
    if failure_reason not in BOUNDED_REASONS:
        failure_reason = "unexpected"
    lines = [
        "# HELP pulse_attachment_storage_canary_success Whether the latest full canary succeeded.",
        "# TYPE pulse_attachment_storage_canary_success gauge",
        f"pulse_attachment_storage_canary_success {int(s.get('success', 0))}",
        "# HELP pulse_attachment_storage_canary_phase_success Whether a phase succeeded in the latest run.",
        "# TYPE pulse_attachment_storage_canary_phase_success gauge",
    ]
    lines.extend(
        f'pulse_attachment_storage_canary_phase_success{{phase="{phase}"}} {int(phase_success.get(phase, 0))}'
        for phase in PHASES
    )
    lines.extend([
        "# HELP pulse_attachment_storage_canary_last_run_timestamp_seconds Last completed run time.",
        "# TYPE pulse_attachment_storage_canary_last_run_timestamp_seconds gauge",
        f"pulse_attachment_storage_canary_last_run_timestamp_seconds {float(s.get('last_run', 0.0)):.3f}",
        "# HELP pulse_attachment_storage_canary_last_success_timestamp_seconds Last successful run time.",
        "# TYPE pulse_attachment_storage_canary_last_success_timestamp_seconds gauge",
        f"pulse_attachment_storage_canary_last_success_timestamp_seconds {float(s.get('last_success', 0.0)):.3f}",
        "# HELP pulse_attachment_storage_canary_last_failure Latest bounded failure phase and reason.",
        "# TYPE pulse_attachment_storage_canary_last_failure gauge",
        'pulse_attachment_storage_canary_last_failure{phase="%s",reason="%s"} %d'
        % (_escape(failure_phase), _escape(failure_reason), int(not s.get("success", 0))),
        "# HELP pulse_attachment_storage_canary_stale_objects_removed Objects removed by the reserved-prefix TTL sweep.",
        "# TYPE pulse_attachment_storage_canary_stale_objects_removed gauge",
        f"pulse_attachment_storage_canary_stale_objects_removed {int(s.get('stale_removed', 0))}",
    ])
    return "\n".join(lines) + "\n"
