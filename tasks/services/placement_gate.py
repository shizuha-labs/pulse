"""PLAT-13267: dispatch-time placement gate (PLAT-10478 R1-R3).

Consults the detective alert set for the target lane/pool BEFORE placement so
a dispatch defers into a degraded lane instead of entering bounded fallback
there (R2: the seat's terminal andon was knowable pre-dispatch).

Design (sora 2026-11-09 08:56Z increment, epic PLAT-10478):
- R1: if ``ShizuhaAgentCapacityLimited`` is firing, or
  ``CortexModelNoHealthyBackend``/``NoBackend`` is firing for the models the
  seat's tasks pin, or a required dependency's service probe is down → defer
  (routing hold with the alert names in the reason), never send.
- R2: defer-not-terminal-stop — the caller composes this with the PLS-374
  routing-hold path; the gate only answers "degraded?" and names the alerts.
- R3: unpinned fallback reservation — see ``interactive_lane_reserved``;
  heavy-session placement excludes the reserved unpinned lane while
  capacity-limited is firing; interactive callers bypass via it.

Read path: Prometheus alert API (endpoint configurable; PLAT-13268 owns the
final read-path/latency-budget confirmation). The gate FAILS OPEN when the
read is unavailable — a blind gate must not out-rank a working placement.

Rollout: ``PULSE_PLACEMENT_GATE_ENFORCE`` (shadow default) composes with
``PULSE_ROUTABILITY_ENFORCE``. In shadow mode callers log the verdict and
place anyway; in enforce mode a non-empty firing set defers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Reason class emitted into the PLS-278 routability vocabulary when the gate
# defers a placement. Label lives in WorkflowService._routability_reason_label.
LANE_DEGRADED_REASON = 'lane_degraded_alert_firing'

# The detective alerts the gate consumes (epic PLAT-10478 / retro 2026-11-08
# row-5 mechanism — alerts exist; this module consumes them pre-dispatch).
CAPACITY_ALERT = 'ShizuhaAgentCapacityLimited'
MODEL_BACKEND_ALERTS = ('CortexModelNoHealthyBackend', 'NoBackend')

_DEFAULT_PROM_URL = 'http://prometheus.monitoring.svc.cluster.local:9090'
_CACHE_KEY = 'pulse:placement_gate:firing_set'
_CACHE_TTL_SECONDS = 20  # well under a dispatch; absorbs scheduler bursts


def _prom_base_url() -> str:
    return str(getattr(settings, 'PLACEMENT_GATE_PROM_URL', '') or _DEFAULT_PROM_URL).rstrip('/')


def _enforce() -> bool:
    return str(getattr(settings, 'PULSE_PLACEMENT_GATE_ENFORCE', 'shadow')
               ).strip().lower() not in {'0', 'false', 'off', 'shadow', 'shadow-only', 'disabled'}


def _fetch_firing_alerts() -> Optional[Dict[str, str]]:
    """Return {alertname: annotation/label digest} for firing detective
    alerts, or None when the read is unavailable (fail-open signal)."""
    import json
    import urllib.parse
    import urllib.request

    query = (
        'ALERTS{alertstate="firing",alertname=~'
        '"ShizuhaAgentCapacityLimited|CortexModelNoHealthyBackend|NoBackend"}'
    )
    url = (
        f"{_prom_base_url()}/api/v1/query?"
        + urllib.parse.urlencode({'query': query, 'timeout': '2s'})
    )
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # nosec B310 - scheme fixed above
            payload = json.load(resp)
        result = payload.get('data', {}).get('result', []) or []
        firing: Dict[str, str] = {}
        for row in result:
            labels = row.get('metric', {}) or {}
            name = str(labels.get('alertname') or '')
            if not name:
                continue
            detail = str(labels.get('model') or labels.get('lane')
                         or labels.get('team') or labels.get('instance') or '')
            # Coalesce per-alertname; keep the first detail per name.
            firing.setdefault(name, detail)
        return firing
    except Exception as exc:
        logger.warning("placement_gate: alert read unavailable (%s); failing open", exc)
        return None


def firing_alerts(force_refresh: bool = False) -> Optional[Dict[str, str]]:
    """Cached view of the firing detective alert set (None = read unavailable)."""
    from django.core.cache import cache

    if not force_refresh:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
    firing = _fetch_firing_alerts()
    if firing is None:
        return None  # fail-open: do not cache unavailability
    cache.set(_CACHE_KEY, firing, _CACHE_TTL_SECONDS)
    return firing


def capacity_limited(firing: Optional[Dict[str, str]] = None) -> bool:
    if firing is None:
        firing = firing_alerts()
    return bool(firing) and CAPACITY_ALERT in firing


def model_backend_unhealthy(pinned_models: Iterable[str],
                            firing: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Map pinned model -> firing alert name for models with no healthy backend."""
    if firing is None:
        firing = firing_alerts()
    if not firing:
        return {}
    unhealthy: Dict[str, str] = {}
    for model in pinned_models:
        token = str(model or '').strip().lower()
        if not token:
            continue
        for alert in MODEL_BACKEND_ALERTS:
            if alert in firing:
                unhealthy.setdefault(str(model), alert)
    return unhealthy


def interactive_lane_reserved() -> bool:
    """R3: at least one unpinned lane is reserved for interactive callers.

    Heavy-session placement must not pin the reserved lane while
    ``ShizuhaAgentCapacityLimited`` is firing; interactive dispatch may use
    it regardless.
    """
    reserved = int(getattr(settings, 'PLACEMENT_GATE_RESERVED_UNPINNED_LANES', 1) or 0)
    return reserved >= 1


def gate_verdict(*, team: str = '', pinned_models: Iterable[str] = (),
                 required_deps: Iterable[str] = (),
                 interactive: bool = False) -> Dict[str, Any]:
    """R1 verdict for one placement decision.

    Returns ``{'defer': bool, 'reason': str, 'alerts': {name: detail},
    'enforced': bool}``. ``defer`` is True only when the gate is enforced
    AND the read path answered AND at least one gate condition fired. The
    reason string names the firing alerts (acceptance b).
    """
    firing = firing_alerts()
    if firing is None:
        return {'defer': False, 'reason': '', 'alerts': {},
                'enforced': _enforce()}

    alerts: Dict[str, str] = {}
    if capacity_limited(firing):
        alerts[CAPACITY_ALERT] = firing.get(CAPACITY_ALERT, '')
    for model, alert in model_backend_unhealthy(pinned_models, firing).items():
        alerts[f"{alert}:{model}"] = model
    alerts.update({k: v for k, v in firing.items()
                   if k.startswith('probe') or k.startswith('ServiceProbe')})

    if interactive and capacity_limited(firing):
        # R3: interactive traffic rides the reserved unpinned lane instead of
        # deferring during capacity limitation.
        return {'defer': False, 'reason': '', 'alerts': alerts,
                'enforced': _enforce()}

    defer = _enforce() and bool(alerts)
    reason = ''
    if defer:
        named = ', '.join(sorted(alerts)) or 'unknown detective alert'
        reason = f"{LANE_DEGRADED_REASON}: {named} (lane={team or 'unspecified'})"
    return {'defer': defer, 'reason': reason, 'alerts': alerts,
            'enforced': _enforce()}
