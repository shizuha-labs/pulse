"""EVOL-58 (EVOL-64): privileged per-user cost pull from Cortex for the
cost-to-output aggregate.

Exchanges a scoped Shizuha ID ``client_credentials`` RS256 service token
(scope ``cortex:usage-by-user.read``, aud ``cortex``, <=15m) and calls
Cortex's ``GET /v1/admin/usage/by-user/`` (cortex PR #84 M2M scope gate),
returning a ``{user_id: cost_row}`` map the daily attribution aggregate
LEFT-JOINs on ``user_id``.

EVOL-64 replaces the retired HS256 shared-secret bridge: Cortex no longer
accepts HS256, and Pulse never self-mints a JWT from the platform shared key.
The token is obtained ONLY by the OAuth ``client_credentials`` exchange
against Shizuha ID, cached within the issuer's TTL.

STRIDE-lite decisions (EVOL-58 task, carried forward):
- D1 spoofing/EoP: least-privilege M2M — exact scope, no wildcard, no staff
  claims (the guard is the ``cortex:usage-by-user.read`` + ``cortex`` audience
  contract, not a synthetic ``is_staff``).
- D4 info-disclosure: server-side only — the token never reaches an end user;
  the attribution aggregate's own EVOL-44 org-role gate remains the sole
  authorization for who sees cost.
- D5 DoS: fail-soft — any error returns ``({}, {...'error'...})`` so the audit
  aggregate never 500s on a Cortex/ID hiccup (availability > completeness),
  with ``error_stage`` distinguishing a token-mint failure from a Cortex-fetch
  failure, and never leaking the token or client secret.
"""
from __future__ import annotations

import logging
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8
_CORTEX_SCOPE = "cortex:usage-by-user.read"
# Internal cluster addresses; deploy config overrides via settings env.
_DEFAULT_ID_TOKEN_URL = "http://shizuha-id:8001/api/oauth/token"
_DEFAULT_CORTEX_URL = "http://localhost:8040"
# ID mints M2M tokens at <=15m; never cache beyond that (worst-case leak window).
_MAX_CACHE_TTL_SECONDS = 15 * 60
_CACHE_SAFETY_MARGIN_SECONDS = 30

_TOKEN_CACHE = {"token": None, "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()


class CortexAuthUnavailable(Exception):
    """Raised when a cortex service token cannot be obtained (missing/bad
    OAuth client config, ID exchange failure)."""


def _oauth_client_config() -> tuple[str, str]:
    client_id = str(getattr(settings, "PULSE_CORTEX_OAUTH_CLIENT_ID", "") or "")
    secret = str(getattr(settings, "PULSE_CORTEX_OAUTH_CLIENT_SECRET", "") or "")
    if not client_id or not secret:
        raise CortexAuthUnavailable(
            "cortex service auth not configured: Pulse->Cortex OAuth client id/secret missing"
        )
    return client_id, secret


def _exchange_cortex_service_token(*, force: bool = False) -> str:
    """Bounded Shizuha ID ``client_credentials`` exchange (RS256 service token).

    The ONLY way Pulse obtains a Cortex service token — never a local HS256
    mint. Cached within the issuer TTL (with safety margin); ``force`` bypasses
    the cache for explicit refresh. Raises ``CortexAuthUnavailable`` on any
    config/exchange failure; the secret and the returned token are never
    logged.
    """
    now = time.time()
    if not force:
        token = _TOKEN_CACHE.get("token")
        expires_at = _TOKEN_CACHE.get("expires_at", 0.0)
        if token and expires_at > now:
            return token
    client_id, secret = _oauth_client_config()
    resp = requests.post(
        _id_token_url(),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": _CORTEX_SCOPE,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    token = data.get("access_token")
    if not token:
        raise CortexAuthUnavailable("cortex service auth unavailable: ID returned no access_token")
    try:
        ttl = int(data.get("expires_in") or _MAX_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = _MAX_CACHE_TTL_SECONDS
    ttl = max(1, min(ttl, _MAX_CACHE_TTL_SECONDS))
    with _TOKEN_LOCK:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = (
            time.time() + ttl - _CACHE_SAFETY_MARGIN_SECONDS
        )
    logger.info("EVOL-64 cortex service token exchanged (scope=%s, ttl=%ds)", _CORTEX_SCOPE, ttl)
    return token


def _id_token_url() -> str:
    return str(
        getattr(settings, "SHIZUHA_ID_OAUTH_TOKEN_URL", _DEFAULT_ID_TOKEN_URL) or _DEFAULT_ID_TOKEN_URL
    ).rstrip("/")


def fetch_cortex_cost_by_user(days: int) -> tuple[dict[int, dict], dict]:
    """Return ``({user_id: cost_row}, provenance)`` for the given lookback window.

    Fail-soft (D5): on ANY error (auth unavailable, ID/Cortex timeout, non-200,
    malformed body) returns ``({}, {..., 'error': ..., 'error_stage': ...})`` —
    never raises — so the caller's aggregate degrades to ``cost: null`` rather
    than failing. ``error_stage`` is ``token_mint`` when obtaining the service
    token failed (ID/config) vs ``cortex_fetch`` when the Cortex call itself
    failed (incl. a 401 from wrong scope/audience), so on-call can tell the two
    apart without ever seeing the token or client secret.
    """
    base = getattr(settings, "SHIZUHA_CORTEX_API_URL", _DEFAULT_CORTEX_URL).rstrip("/")
    url = f"{base}/v1/admin/usage/by-user/"
    provenance: dict = {"source": "cortex.usage.by_user", "window_days": days, "cortex_url": base}
    # PLAT-1358 hermetic guard: the test suite forbids outbound calls; tests inject
    # their own stubs. Skip cleanly (fail-soft shape) so the audit aggregate's own
    # tests never touch the network.
    if getattr(settings, "TESTING", False):
        provenance["skipped"] = "TESTING"
        return {}, provenance
    try:
        token = _exchange_cortex_service_token()
    except Exception as exc:  # noqa: BLE001 — fail-soft is the contract (D5)
        logger.warning("EVOL-64 cortex token mint failed (fail-soft): %s", str(exc)[:200])
        provenance["error_stage"] = "token_mint"
        provenance["error"] = str(exc)[:200]
        return {}, provenance
    try:
        resp = requests.get(
            url,
            params={"days": days},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        by_user: dict[int, dict] = {}
        for row in (data.get("by_user") or []):
            uid = row.get("user_id")
            if uid is None:
                continue
            by_user[int(uid)] = row
        provenance["generated_at"] = data.get("generated_at")
        provenance["since"] = data.get("since")
        provenance["row_count"] = len(by_user)
        logger.info("EVOL-58 cortex cost pull ok: %d users, window=%dd", len(by_user), days)
        return by_user, provenance
    except Exception as exc:  # noqa: BLE001 — fail-soft is the contract (D5)
        logger.warning("EVOL-64 cortex cost fetch failed (fail-soft, stage=cortex_fetch): %s", str(exc)[:200])
        provenance["error_stage"] = "cortex_fetch"
        provenance["error"] = str(exc)[:200]
        return {}, provenance
