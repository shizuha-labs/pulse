"""Project-membership projection outcome counter (PLS-728).

``/api/internal/users/<id>/project-memberships/`` is the authoritative producer
of a value that becomes a 15-minute JWT authorization claim (``project_ids`` /
``project_roles``) stamped by shizuha-id. A projection that is empty or short
for a reason internal to Pulse is served with a 200 and is indistinguishable
downstream from a user who genuinely has no projects — the same failure class
PLAT-5338 was (a confident wrong answer, not an outage).

This module records, at the source where it is load-bearing, which of the three
serve outcomes happened:

- ``served``             — a non-empty projection was served.
- ``authoritative_empty``— Pulse holds no memberships for this user; the empty
                           projection is genuine (counted, never alerted on by
                           itself, so an operator can tell "Pulse said none"
                           apart from "Pulse could not ask").
- ``unavailable``        — the org-membership check was *unknown* (shizuha-admin
                           unreachable) and the endpoint failed closed with a
                           typed non-200 rather than serving a short answer.

The counter is file-backed (mirroring ``probe_views``) so every backend pod
renders the same aggregate at ``/api/metrics/``; the family is always rendered
with all three outcomes (zero-filled), so a detector can alert on ``absent()``
of the family as well as on ``unavailable`` increments.
"""
import fcntl
import json
import os
import threading

_metrics_lock = threading.Lock()
_metrics_path = os.environ.get(
    "PULSE_PROJECT_MEMBERSHIP_METRICS_PATH",
    "/tmp/pulse-project-membership-metrics.json",
)
_metrics_lock_path = _metrics_path + ".lock"
_local: dict = {}

OUTCOMES = ("served", "authoritative_empty", "unavailable")


def record_projection_outcome(outcome: str) -> None:
    """Record one serve outcome. Only fixed, source-controlled values allowed."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown project-membership outcome {outcome!r}")
    with _metrics_lock:
        try:
            with open(_metrics_lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    with open(_metrics_path, encoding="utf-8") as metrics_file:
                        snapshot = json.load(metrics_file)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    snapshot = {}
                if not isinstance(snapshot, dict):
                    snapshot = {}
                snapshot[outcome] = int(snapshot.get(outcome, 0)) + 1
                temporary_path = f"{_metrics_path}.{os.getpid()}.{threading.get_ident()}"
                with open(temporary_path, "w", encoding="utf-8") as metrics_file:
                    json.dump(snapshot, metrics_file, sort_keys=True, separators=(",", ":"))
                os.replace(temporary_path, _metrics_path)
        except (OSError, TypeError, ValueError):
            # Telemetry can degrade to process-local counters, but it must never
            # change the projection's authorization behavior.
            _local[outcome] = _local.get(outcome, 0) + 1


def _read_snapshot() -> dict:
    with _metrics_lock:
        try:
            with open(_metrics_lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                with open(_metrics_path, encoding="utf-8") as metrics_file:
                    snapshot = json.load(metrics_file)
            if not isinstance(snapshot, dict):
                snapshot = {}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            snapshot = dict(_local)
    return snapshot


def count_for(outcome: str) -> int:
    """Return the current count for one outcome (test/operator helper)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown project-membership outcome {outcome!r}")
    return int(_read_snapshot().get(outcome, 0))


def render_project_membership_metrics() -> str:
    """Render bounded-cardinality Prometheus counters for the serve outcomes."""
    snapshot = _read_snapshot()
    lines = [
        "# HELP pulse_project_membership_projection_total "
        "Project-membership projection serve outcomes by sanitized result.",
        "# TYPE pulse_project_membership_projection_total counter",
    ]
    for outcome in OUTCOMES:
        lines.append(
            f'pulse_project_membership_projection_total{{outcome="{outcome}"}} '
            f'{snapshot.get(outcome, 0)}'
        )
    return "\n".join(lines) + "\n"


# PLAT-5343: the v1 projection endpoint's latency tail (6-9s observed on 2/6
# samples) is fatal rather than merely slow — shizuha-id blocks every JWT mint
# on this call with a 1.0s budget, so a slow answer is indistinguishable at the
# token from "this user is a member of no projects". Serve-outcome counters
# (above) cannot see duration. This family adds a fixed, source-controlled
# bucket histogram so the tail is observable under normal load and a detector
# can alert on the overflow bucket directly. Buckets are milliseconds.
LATENCY_BUCKETS_MS = (100, 250, 500, 1000, 2500, 5000)
LATENCY_OVERFLOW_LABEL = "5000_plus"


def _latency_bucket_label(latency_ms: float) -> str:
    for upper in LATENCY_BUCKETS_MS:
        if latency_ms < upper:
            return f"lt_{upper}"
    return LATENCY_OVERFLOW_LABEL


def record_projection_latency_ms(latency_ms: float) -> None:
    """Record one serve duration into the fixed bucket histogram.

    Only the fixed, source-controlled buckets are ever written, so the metric
    keeps bounded cardinality no matter what the caller passes.
    """
    label = _latency_bucket_label(latency_ms)
    with _metrics_lock:
        try:
            with open(_metrics_lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    with open(_metrics_path, encoding="utf-8") as metrics_file:
                        snapshot = json.load(metrics_file)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    snapshot = {}
                if not isinstance(snapshot, dict):
                    snapshot = {}
                latency = snapshot.setdefault("latency_ms", {})
                if not isinstance(latency, dict):
                    latency = {}
                    snapshot["latency_ms"] = latency
                latency[label] = int(latency.get(label, 0)) + 1
                temporary_path = f"{_metrics_path}.{os.getpid()}.{threading.get_ident()}"
                with open(temporary_path, "w", encoding="utf-8") as metrics_file:
                    json.dump(snapshot, metrics_file, sort_keys=True, separators=(",", ":"))
                os.replace(temporary_path, _metrics_path)
        except (OSError, TypeError, ValueError):
            # Telemetry can degrade to process-local counters, but it must never
            # change the projection's authorization behavior.
            _local.setdefault("latency_ms", {})
            _local["latency_ms"][label] = _local["latency_ms"].get(label, 0) + 1


def render_project_membership_latency_metrics() -> str:
    """Render the bucket histogram, zero-filled across all fixed buckets."""
    snapshot = _read_snapshot()
    latency = snapshot.get("latency_ms") if isinstance(snapshot, dict) else None
    if not isinstance(latency, dict):
        latency = dict(_local.get("latency_ms") or {})
    labels = [f"lt_{upper}" for upper in LATENCY_BUCKETS_MS] + [LATENCY_OVERFLOW_LABEL]
    lines = [
        "# HELP pulse_project_membership_projection_latency_bucket "
        "Project-membership projection serve durations by fixed millisecond bucket.",
        "# TYPE pulse_project_membership_projection_latency_bucket counter",
    ]
    for label in labels:
        lines.append(
            f'pulse_project_membership_projection_latency_bucket{{bucket="{label}"}} '
            f'{int(latency.get(label, 0))}'
        )
    return "\n".join(lines) + "\n"
