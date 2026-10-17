"""Fail-loud telemetry for the exact-org org-health routing snapshot.

The sweep runs in Celery while ``/metrics/`` is served by web pods, so the
latest snapshot is kept in Django's shared cache rather than process memory.
Incomplete collections are explicitly unsuccessful; they never masquerade as
a legitimate zero-lane snapshot.
"""
from __future__ import annotations

import time

from django.core.cache import cache
from django.db import DatabaseError


CACHE_KEY = "pulse:plat4877:org_health_exact_snapshot:v1"
SOURCE = "org_health_exact_routing_lanes"
ERROR_CLASSES = ("none", "database", "timeout", "routing_authority", "unexpected")


def _error_class(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "timeout"
    if isinstance(exc, DatabaseError):
        return "database"
    if exc.__class__.__name__ == "RoutingAuthorityError":
        return "routing_authority"
    return "unexpected"


def _previous_last_success() -> float:
    try:
        state = cache.get(CACHE_KEY) or {}
        return float(state.get("last_success_timestamp") or 0)
    except Exception:
        return 0.0


def _store(state: dict) -> None:
    try:
        cache.set(CACHE_KEY, state, timeout=None)
    except Exception:
        # Telemetry failure must not mutate routing or turn a successful
        # detector pass into a failed detector pass.  The metrics endpoint's
        # missing-state fallback remains explicitly unhealthy.
        pass


def record_snapshot_success(*, attempted: int, completed: int, lanes: list[dict], now=None) -> dict:
    """Publish one complete collection, including a real zero-lane result."""
    observed = float(time.time() if now is None else now)
    state = {
        "initialized": True,
        "success": True,
        "error": "none",
        "attempted": int(attempted),
        "completed": int(completed),
        "last_success_timestamp": observed,
        "observed_timestamp": observed,
        "failed_lane": None,
        "lanes": list(lanes),
    }
    _store(state)
    return state


def record_snapshot_failure(
    exc: Exception,
    *,
    attempted: int = 0,
    completed: int = 0,
    failed_lane: dict | None = None,
    lanes: list[dict] | None = None,
    now=None,
) -> dict:
    """Publish an incomplete collection without advancing last-success."""
    observed = float(time.time() if now is None else now)
    state = {
        "initialized": True,
        "success": False,
        "error": _error_class(exc),
        "attempted": int(attempted),
        "completed": int(completed),
        "last_success_timestamp": _previous_last_success(),
        "observed_timestamp": observed,
        "failed_lane": failed_lane,
        "lanes": list(lanes or []),
    }
    _store(state)
    return state


def snapshot_probe_state() -> dict:
    try:
        state = cache.get(CACHE_KEY)
    except Exception:
        state = None
    if isinstance(state, dict):
        return state
    return {
        "initialized": False,
        "success": False,
        "error": "unexpected",
        "attempted": 0,
        "completed": 0,
        "last_success_timestamp": 0.0,
        "observed_timestamp": 0.0,
        "failed_lane": None,
        "lanes": [],
    }


def _escaped(value) -> str:
    return str(value or "unknown").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lane_labels(lane: dict) -> str:
    return (
        f'organization_id="{_escaped(lane.get("organization_id"))}",'
        f'team="{_escaped(lane.get("team_key"))}",'
        f'source="{_escaped(lane.get("source") or "unknown")}",'
        f'version="{_escaped(lane.get("version") or "unknown")}"'
    )


def render_org_health_snapshot_metrics(*, now=None) -> str:
    """Render bounded, shared-state Prometheus gauges for the latest attempt."""
    state = snapshot_probe_state()
    current = float(time.time() if now is None else now)
    last_success = float(state.get("last_success_timestamp") or 0)
    age = max(0.0, current - last_success) if last_success else 0.0
    lines = [
        "# HELP pulse_org_health_exact_snapshot_success Whether the latest exact-org snapshot completed.",
        "# TYPE pulse_org_health_exact_snapshot_success gauge",
        f'pulse_org_health_exact_snapshot_success{{source="{SOURCE}"}} {1 if state.get("success") else 0}',
        "# HELP pulse_org_health_exact_snapshot_lanes Latest snapshot lane progress by phase.",
        "# TYPE pulse_org_health_exact_snapshot_lanes gauge",
        f'pulse_org_health_exact_snapshot_lanes{{phase="attempted",source="{SOURCE}"}} {int(state.get("attempted") or 0)}',
        f'pulse_org_health_exact_snapshot_lanes{{phase="completed",source="{SOURCE}"}} {int(state.get("completed") or 0)}',
        "# HELP pulse_org_health_exact_snapshot_last_success_timestamp_seconds Unix timestamp of the last complete snapshot.",
        "# TYPE pulse_org_health_exact_snapshot_last_success_timestamp_seconds gauge",
        f'pulse_org_health_exact_snapshot_last_success_timestamp_seconds{{source="{SOURCE}"}} {last_success:g}',
        "# HELP pulse_org_health_exact_snapshot_last_success_age_seconds Age of the last complete snapshot, or zero before one exists.",
        "# TYPE pulse_org_health_exact_snapshot_last_success_age_seconds gauge",
        f'pulse_org_health_exact_snapshot_last_success_age_seconds{{source="{SOURCE}"}} {age:g}',
        "# HELP pulse_org_health_exact_snapshot_error Current bounded error class.",
        "# TYPE pulse_org_health_exact_snapshot_error gauge",
    ]
    current_error = state.get("error") if state.get("error") in ERROR_CLASSES else "unexpected"
    for error in ERROR_CLASSES:
        lines.append(
            f'pulse_org_health_exact_snapshot_error{{error="{error}",source="{SOURCE}"}} '
            f'{1 if current_error == error else 0}'
        )

    lane_rows = list(state.get("lanes") or [])
    failed_lane = state.get("failed_lane")
    if isinstance(failed_lane, dict):
        lane_rows.append({**failed_lane, "success": False})
    if not lane_rows and state.get("initialized") and not state.get("success"):
        lane_rows.append({
            "organization_id": "unknown", "team_key": "unknown",
            "source": "unknown", "version": 0, "success": False,
        })
    lines += [
        "# HELP pulse_org_health_exact_snapshot_lane_success Latest per-lane completion state.",
        "# TYPE pulse_org_health_exact_snapshot_lane_success gauge",
    ]
    seen = set()
    for lane in lane_rows:
        labels = _lane_labels(lane)
        if labels in seen:
            continue
        seen.add(labels)
        success = lane.get("success", True)
        lines.append(f'pulse_org_health_exact_snapshot_lane_success{{{labels}}} {1 if success else 0}')
    return "\n".join(lines) + "\n"
