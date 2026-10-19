"""Prometheus exposition: per-principal agent runtime estate (PLAT-11804).

The 10-14 fleet freeze decomposed into per-runtime provider binding failures,
but every frozen stop emitted only prose ("provider/runtime unavailable") — no
shared typed signal existed, so no carrier could distinguish "my provider is
down" from "the estate is down" from PLAT-8689 backoff. This exposition is the
convergent discriminator surface:

- ``pulse_agent_runtime_estate{principal}`` — per-principal value:
  ``1`` = fresh committed authenticated Pulse heartbeat (provider ok),
  ``0`` = published unavailable (no fresh heartbeat),
  ``2`` = published unavailable WITH a fresh heartbeat (PLAT-8689 backoff
  state — the frozen-gauge signature, not a live failure),
  ``-1`` = never seen in either plane.
- ``pulse_agent_runtime_estate_blind`` — ``1`` when the estate probe itself
  fails, so a blind gauge is distinguishable from a healthy roster (same
  blindness convention as ``pulse_broker_redis_error``).

Roster is discovered from the durable planes themselves (unavailability-reason
sidecar keys + heartbeat marker keys); no hardcoded usernames. Read-only by
construction; no item keys, payload, creds, or error text in the exposition.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Estate values (documented contract; do not reorder — Grafana panels key on these).
ESTATE_OK = 1
ESTATE_DOWN = 0
ESTATE_BACKOFF = 2
ESTATE_UNKNOWN = -1


def _scan_keys(redis_client, pattern):
    """Return a set of decoded keys matching ``pattern``. Never raises."""
    try:
        found = set()
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=200)
            for k in keys or []:
                if isinstance(k, bytes):
                    k = k.decode('utf-8', 'replace')
                found.add(k)
            if int(cursor) == 0:
                break
        return found
    except Exception:
        logger.warning("runtime estate key scan failed", exc_info=True)
        return None


def collect_agent_runtime_estate_metrics(redis_client=None):
    """Return (estate: dict principal->int, blind: bool) for the estate gauge.

    - ``pulse:agent_hb:<alias>`` present (any TTL state) = the identity
      committed an authenticated request recently -> OK; if it ALSO appears in
      the unavailability-reason sidecar, the runtime is cooling down -> BACKOFF.
    - Sidecar-only principals (published unavailable, heartbeat expired) = DOWN.
    """
    from .services.workflow_service import WorkflowService

    if redis_client is None:
        redis_client = WorkflowService._redis_client()
    try:
        raw = redis_client.get('pulse:agent_unavailability_reason')
        sidecar = json.loads(raw) if raw else {}
    except Exception:
        logger.warning("runtime estate reason sidecar read failed", exc_info=True)
        return {}, True
    if not isinstance(sidecar, dict):
        sidecar = {}

    hb_keys = _scan_keys(redis_client, 'pulse:agent_hb:*')
    if hb_keys is None:
        return {}, True

    estate: dict[str, int] = {}
    for principal in sidecar:
        p = str(principal).strip().lower()
        if not p:
            continue
        held = any(f'pulse:agent_hb:{alias}' in hb_keys for alias in _aliases(p))
        estate[p] = ESTATE_BACKOFF if held else ESTATE_DOWN
    for key in hb_keys:
        alias = key.removeprefix('pulse:agent_hb:')
        if not any(alias in _aliases(p) for p in estate):
            estate[alias] = ESTATE_OK
    return estate, False


def _aliases(principal):
    """Both marker address forms for ``principal`` (canonical + agents alias)."""
    from .agent_liveness import agent_email_aliases

    try:
        return agent_email_aliases(principal)
    except Exception:
        p = str(principal).strip().lower()
        out = {p}
        if p.endswith('@shizuha.com'):
            out.add(p[:-len('@shizuha.com')] + '@agents.shizuha.io')
        return out


def render_agent_runtime_estate_metrics() -> str:
    try:
        estate, blind = collect_agent_runtime_estate_metrics()
    except Exception:
        logger.warning("agent runtime estate metrics collection failed", exc_info=True)
        return 'pulse_agent_runtime_estate_blind 1\n'
    if blind:
        return 'pulse_agent_runtime_estate_blind 1\n'
    lines = ['# agent runtime estate (PLAT-11804): 1=ok 0=down 2=backoff(PLAT-8689)']
    for principal in sorted(principal for principal in estate):
        label = principal.replace('"', '')
        lines.append(f'pulse_agent_runtime_estate{{principal="{label}"}} {estate[principal]}')
    lines.append('pulse_agent_runtime_estate_blind 0')
    return '\n'.join(lines) + '\n'
