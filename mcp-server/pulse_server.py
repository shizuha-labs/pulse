#!/usr/bin/env python3
"""
Shizuha Pulse MCP Server.

This MCP server provides tools for AI agents to interact with Shizuha Pulse,
the task and project management service.

Configuration:
- SHIZUHA_PULSE_URL or SHIZUHA_BASE_URL: Base URL for Pulse API
- SHIZUHA_PULSE_JWT_TOKEN: JWT token for authentication (required)
"""

import argparse
import json
import logging
import os
import sys
import time as _time
from typing import Optional, Union, List, Any
from urllib.parse import quote

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskmanager.settings")
import django

django.setup()

from tasks.services.recurring import validate_recurring_configuration
from tasks.origin_provenance import (
    ORIGIN_PUBLIC_URL,
    development_repo_block_reason,
    find_origin_run_url,
    linked_pr_development_block_reason,
    normalize_origin_observation,
    normalize_origin_observations,
    origin_ci_logs_url,
    parse_linked_pr_identity,
    parse_origin_pr_reference,
    parse_origin_pr_legacy_singular_url,
    parse_forgejo_legacy_host_pr_url,
    origin_pr_link_ingress_error,
    canonical_origin_pr_url,
    parse_origin_run_url,
)

import requests
from mcp.server.fastmcp import FastMCP
import datetime as _dt
import hashlib as _hashlib

from taskmanager.topology import current_topology as _current_topology


def _origin_api_base() -> str:
    """PLS-1409 / CON-466: Forgejo REST API base from env-configurable topology."""
    return _current_topology().origin_api_url


# No system user fallback — each agent authenticates with their own JWT.
# The token comes from the Authorization header on each MCP request.


# Initialize FastMCP server.
# stateless_http=True (PLAT-518): pulse runs HA (>1 replica) and streamable-HTTP
# sessions are replica-local — without it, a request can land on a replica that
# doesn't hold the session → "Session not found". Stateless mode makes every
# request self-contained so ANY replica serves ANY request (no session affinity,
# 100% reliable under horizontal scaling). Our tools are request/response, so we
# need no long-lived server-side session state.
mcp = FastMCP("shizuha-pulse", stateless_http=True)

# Wire OAuth: discovery metadata + (when SHIZUHA_MCP_OAUTH_ENFORCE=true)
# transport-level bearer enforcement. Shared with all other services via base.
from base import setup_oauth_routes as _setup_oauth_routes
import typed_change_spec as _tcs
from tasks.typed_change_integrity import project_typed_envelope
_setup_oauth_routes(mcp, "pulse")
from mcp.server.sse import TransportSecuritySettings
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _format_recurrence_validation_error(is_recurring, frequency, recurrence_pattern=None):
    try:
        validate_recurring_configuration(is_recurring, frequency, recurrence_pattern)
    except ValueError as exc:
        return f"Error: {exc}"
    return None


def parse_args():
    """Parse command-line arguments for CLI argument compatibility."""
    parser = argparse.ArgumentParser(description="Shizuha Pulse MCP Server")
    parser.add_argument(
        "--jwt-token",
        help="JWT token for authentication (overrides SHIZUHA_PULSE_JWT_TOKEN env var)",
    )
    parser.add_argument(
        "--api-url",
        help="Pulse API URL (overrides SHIZUHA_PULSE_URL env var)",
    )
    args, _ = parser.parse_known_args()
    return args


# Parse CLI args (for callers that don't pass env vars to MCP subprocesses)
_args = parse_args()

# Configuration priority: CLI args > environment > defaults
PULSE_API_URL = (
    _args.api_url
    or os.environ.get("SHIZUHA_PULSE_URL")
    or os.environ.get("PULSE_API_URL")
)
PULSE_JWT_TOKEN = (
    _args.jwt_token
    or os.environ.get("SHIZUHA_PULSE_JWT_TOKEN")
    or os.environ.get("PULSE_JWT_TOKEN")
    or os.environ.get("JWT_TOKEN")
    or ""
)

# User delegation token (dual-token model for chatbot sessions).
# When present, allows tools to make API calls on behalf of the chatting user.
PULSE_USER_JWT_TOKEN = os.environ.get("SHIZUHA_PULSE_USER_JWT_TOKEN", "")
PULSE_REFRESH_TOKEN = (
    os.environ.get("SHIZUHA_PULSE_REFRESH_TOKEN")
    or os.environ.get("PULSE_REFRESH_TOKEN")
    or os.environ.get("SHIZUHA_MCP_REFRESH_TOKEN")
    or ""
)
TOKEN_REFRESH_WINDOW_SECONDS = int(os.environ.get("SHIZUHA_MCP_TOKEN_REFRESH_WINDOW_SECONDS", "300"))
_REFRESHED_AGENT_TOKEN = ""
_REFRESHED_AGENT_TOKEN_EXP = 0

# Set up logging to stderr (stdout is used for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _extract_bearer_token(raw_value: Optional[str]) -> str:
    """Extract token from either 'Bearer <token>' or raw token formats."""
    if not raw_value:
        return ""
    value = str(raw_value).strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _get_request_headers() -> dict[str, str]:
    """Get normalized headers from the current MCP HTTP request."""
    try:
        context = mcp.get_context()
        request_context = getattr(context, "request_context", None)
        request = getattr(request_context, "request", None)
        if request is None:
            return {}
        raw_headers = getattr(request, "headers", None)
        if raw_headers is None:
            return {}
        return {str(k).lower(): str(v) for k, v in raw_headers.items() if v is not None}
    except Exception:
        return {}


def _get_effective_refresh_token() -> str:
    """Resolve refresh token from MCP headers or env for proactive access refresh."""
    headers = _get_request_headers()
    for header_name in (
        "x-shizuha-agent-refresh-token",
        "x-shizuha-refresh-token",
        "x-shizuha-mcp-refresh-token",
    ):
        token = _extract_bearer_token(headers.get(header_name))
        if token:
            return token
    return PULSE_REFRESH_TOKEN


def _jwt_expires_in(token: str) -> Optional[int]:
    if not token:
        return None
    try:
        import jwt as _jwt
        payload = _jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        exp = payload.get("exp")
        if exp is None:
            return None
        return int(float(exp) - _time.time())
    except Exception:
        return None


def _refresh_url() -> str:
    return os.environ.get("SHIZUHA_MCP_REFRESH_URL") or f"{os.environ.get('SHIZUHA_ID_URL', 'https://id.shizuha.com').rstrip('/')}/api/oauth/token"


def _refresh_agent_token(refresh_token: str) -> str:
    """Refresh an agent access token using OAuth refresh_token grant."""
    if not refresh_token:
        return ""
    try:
        response = requests.post(
            _refresh_url(),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        token = body.get("access_token") or body.get("access") or body.get("token")
        if token:
            logger.info("Refreshed Pulse MCP agent access token proactively")
            return str(token)
        logger.warning("Pulse MCP token refresh response did not include an access token")
    except Exception as exc:
        logger.warning("Pulse MCP proactive token refresh failed: %s", exc)
    return ""


def _maybe_refresh_agent_token(token: str) -> str:
    """Refresh before expiry when a refresh token is available; otherwise return original."""
    global _REFRESHED_AGENT_TOKEN, _REFRESHED_AGENT_TOKEN_EXP
    ttl = _jwt_expires_in(token)
    if ttl is None or ttl > TOKEN_REFRESH_WINDOW_SECONDS:
        return token

    now = int(_time.time())
    if _REFRESHED_AGENT_TOKEN and _REFRESHED_AGENT_TOKEN_EXP - now > TOKEN_REFRESH_WINDOW_SECONDS:
        return _REFRESHED_AGENT_TOKEN

    refresh_token = _get_effective_refresh_token()
    refreshed = _refresh_agent_token(refresh_token)
    if refreshed:
        refreshed_ttl = _jwt_expires_in(refreshed)
        _REFRESHED_AGENT_TOKEN = refreshed
        _REFRESHED_AGENT_TOKEN_EXP = now + (refreshed_ttl if refreshed_ttl is not None else 3600)
        return refreshed
    return token


def _get_effective_agent_token() -> str:
    """
    Get the calling agent's token for Pulse API calls.
    The MCP server is a transparent proxy — it forwards the caller's auth.
    Missing caller/static auth fails closed; never mint a privileged fallback.
    """
    # 1. Try extracting from the MCP request headers (per-agent identity)
    headers = _get_request_headers()
    if headers:
        logger.info(f"MCP request headers available: {list(headers.keys())}")
    for header_name in ("authorization", "x-shizuha-agent-authorization"):
        raw = headers.get(header_name, "")
        if raw.lower().startswith("bearer "):
            logger.info(f"Using caller token from header: {header_name}")
            return _maybe_refresh_agent_token(raw[7:].strip())

    # 2. Try static env token
    if PULSE_JWT_TOKEN:
        return _maybe_refresh_agent_token(PULSE_JWT_TOKEN)

    # 3. No token available: fail closed. PLAT-38 made the MCP transport
    # auth-required; PLAT-1068 removes the old privileged mcp-service fallback.
    return ""


def _get_effective_user_token() -> str:
    """Resolve delegated user JWT from request headers, fallback to static env."""
    headers = _get_request_headers()
    for header_name in (
        "x-shizuha-user-authorization",
        "x-shizuha-user-jwt-token",
        "x-shizuha-user-jwt",
    ):
        token = _extract_bearer_token(headers.get(header_name))
        if token:
            return token
    return PULSE_USER_JWT_TOKEN


def get_headers(as_user: bool = False) -> dict:
    """
    Get headers for API requests.

    Args:
        as_user: If True, use user delegation token (acts on behalf of the
                 chatting user, respecting their permissions).
    """
    user_token = _get_effective_user_token()
    agent_token = _get_effective_agent_token()

    if as_user:
        if not user_token:
            raise ValueError(
                "User delegation token not available. "
                "The user has not shared Pulse access with this agent."
            )
        return {
            "Authorization": f"Bearer {user_token}",
            "Content-Type": "application/json",
        }
    if not agent_token:
        raise ValueError(
            "Authorization header (or SHIZUHA_PULSE_JWT_TOKEN for local debug) is required"
        )
    return {
        "Authorization": f"Bearer {agent_token}",
        "Content-Type": "application/json",
    }


def _token_expiry_hint(token: str) -> dict:
    """Best-effort structured token-expiry hint for MCP callers."""
    if not token:
        return {}
    try:
        import jwt as _jwt
        import time as _time
        payload = _jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        exp = payload.get("exp")
        if exp is None:
            return {}
        expires_in = int(float(exp) - _time.time())
        return {
            "expires_in": expires_in,
            "expired": expires_in <= 0,
            "refresh_url": _refresh_url(),
            "re_auth_required": False,
        }
    except Exception:
        return {}


def api_request(
    method: str,
    endpoint: str,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    as_user: bool = False,
) -> dict:
    """Make an API request to Pulse."""
    url = f"{PULSE_API_URL}/api/{endpoint.lstrip('/')}"
    logger.info(f"API {method} {url} (as_user={as_user})")

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=get_headers(as_user=as_user),
            params=params,
            json=data,
            timeout=30,
        )
        response.raise_for_status()
        if response.status_code == 204:
            return {"success": True}
        return response.json()
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json()
        except Exception:
            error_detail = e.response.text
        status_code = e.response.status_code
        logger.error(f"API error: {e} - {error_detail}")
        body = {"error": str(e), "detail": error_detail, "status_code": status_code}
        if status_code == 401:
            token = _get_effective_user_token() if as_user else _get_effective_agent_token()
            hint = _token_expiry_hint(token)
            if hint:
                body["auth_error"] = "token_expired" if hint.get("expired") else "auth_failed"
                body["expires_in"] = hint.get("expires_in")
                body["refresh_url"] = hint.get("refresh_url")
                body["re_auth_required"] = bool(hint.get("re_auth_required"))
                logger.warning("Pulse MCP auth failure: %s expires_in=%s refresh_url=%s", body["auth_error"], body.get("expires_in"), body.get("refresh_url"))
            else:
                body["auth_error"] = "re_auth_required"
                body["re_auth_required"] = True
        return body
    except requests.exceptions.Timeout as e:
        # PLS-1388 / PLS-1397: a silent timeout on an escalation create is a
        # platform defect. Surface a typed, retryable error plus a stable
        # dedup key so a retry can be correlated instead of looking like a
        # dropped call.
        dedup = _transport_dedup_key(method, endpoint, data)
        logger.error("API timeout %s %s dedup=%s: %s", method, endpoint, dedup, e)
        return {
            "error": str(e),
            "error_code": "pulse_transport_timeout",
            "retryable": True,
            "dedup_key": dedup,
            "trace": f"timeout method={method} endpoint={endpoint} dedup={dedup}",
        }
    except requests.exceptions.RequestException as e:
        dedup = _transport_dedup_key(method, endpoint, data)
        logger.error("Request error %s %s dedup=%s: %s", method, endpoint, dedup, e)
        return {
            "error": str(e),
            "error_code": "pulse_transport_unreachable",
            "retryable": True,
            "dedup_key": dedup,
            "trace": f"unreachable method={method} endpoint={endpoint} dedup={dedup}",
        }



def _github_review_evidence(reviewers: list[str], since: _dt.datetime, github_org: str = "shizuha-labs") -> dict:
    """Best-effort reviewer evidence via GitHub Search + Pull Request Reviews REST APIs.

    GitHub's issue/PR search supports `is:pr reviewed-by:<user>`; we then fetch
    each candidate PR's reviews and filter by exact reviewer + submitted_at so
    the 24h table is based on review timestamps, not just PR updated time.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return {
            "configured": False,
            "source": "github.rest.search_and_pull_request_reviews",
            "warning": "GITHUB_TOKEN/GH_TOKEN not available to Pulse MCP server",
            "reviewers": [],
        }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    since_utc = since.astimezone(_dt.timezone.utc)
    since_date = since_utc.date().isoformat()
    out = []
    for reviewer in reviewers:
        reviewer = (reviewer or "").strip()
        if not reviewer:
            continue
        entry = {"reviewer": reviewer, "count": 0, "reviews": []}
        q = f"org:{github_org} is:pr reviewed-by:{reviewer} updated:>={since_date}"
        try:
            search = requests.get(
                "https://api.github.com/search/issues",
                headers=headers,
                params={"q": q, "per_page": 50},
                timeout=20,
            )
            search.raise_for_status()
            for item in search.json().get("items", []):
                repo_url = item.get("repository_url") or ""
                repo = repo_url.rsplit("/repos/", 1)[-1]
                number = item.get("number")
                if not repo or not number:
                    continue
                reviews = requests.get(
                    f"https://api.github.com/repos/{repo}/pulls/{number}/reviews",
                    headers=headers,
                    params={"per_page": 100},
                    timeout=20,
                )
                reviews.raise_for_status()
                for review in reviews.json():
                    user = (review.get("user") or {}).get("login") or ""
                    submitted_raw = review.get("submitted_at") or ""
                    if user.lower() != reviewer.lower() or not submitted_raw:
                        continue
                    submitted = _dt.datetime.fromisoformat(submitted_raw.replace("Z", "+00:00"))
                    if submitted < since_utc:
                        continue
                    entry["reviews"].append({
                        "repo": repo,
                        "number": number,
                        "state": review.get("state"),
                        "submitted_at": submitted_raw,
                        "url": review.get("html_url") or item.get("html_url"),
                    })
            entry["reviews"].sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
            entry["count"] = len(entry["reviews"])
        except Exception as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return {
        "configured": True,
        "source": "github.rest.search_and_pull_request_reviews",
        "github_org": github_org,
        "since": since_utc.isoformat(),
        "reviewers": out,
    }

def _format_error_field_details(details) -> str:
    """Render a structured field→messages map (PLS-859 assignment_validation_failed)."""
    if not isinstance(details, dict) or not details:
        return ""
    parts = []
    for field, messages in details.items():
        if isinstance(messages, (list, tuple)):
            msg = "; ".join(str(m) for m in messages)
        else:
            msg = str(messages)
        parts.append(f"{field}: {msg}")
    return "; ".join(parts)


def _error_detail_text(result: dict) -> str:
    """Extract the server's explanation (response body) from an api_request error dict.

    api_request() stashes the raw response body (JSON or text) under 'detail'; this
    pulls out the most human-readable message so callers can surface WHY a request
    failed instead of a bare '400 Client Error'. Returns '' when there is no body.

    PLS-859: when the body is ``assignment_validation_failed`` with a structured
    ``details`` map (field → constraints), prefer the human ``detail`` string if
    present, otherwise format ``details``. Never drop the field map — that is
    what left agents with a bare code and no failing constraint.
    """
    detail = result.get("detail", "")
    if isinstance(detail, dict):
        human = detail.get("detail")
        if human and str(human).strip() and str(human).strip() not in (
            "assignment_validation_failed",
            "{}",
            "None",
        ):
            # Prefer the server's human detail; append structured field map when
            # it adds information the prose does not already contain.
            text = str(human).strip()
            field_text = _format_error_field_details(detail.get("details"))
            if field_text and field_text not in text:
                text = f"{text} | details: {field_text}"
            err_code = detail.get("error")
            if (
                err_code
                and str(err_code).strip()
                and str(err_code).strip() not in text
                and str(err_code).strip() != "assignment_validation_failed"
            ):
                text = f"{err_code}: {text}"
            elif err_code and str(err_code).strip() == "assignment_validation_failed":
                text = f"assignment_validation_failed — {text}"
            return text
        field_text = _format_error_field_details(detail.get("details"))
        err_code = detail.get("error")
        if field_text:
            if err_code:
                return f"{err_code} — {field_text}"
            return field_text
        # PLS-858: the API carries the human explanation in a `message` key
        # alongside the machine `error` code (e.g. the wip_capacity_denied body:
        # {"error": "wip_capacity_denied", "message": "<remedy>"}). Consult it
        # here — a bare code made agents guess the remedy instead of acting on
        # the server's own sentence. Combine code + message when both exist so
        # callers that match on the code keep working.
        msg_text = detail.get("message")
        if msg_text and str(msg_text).strip() and str(msg_text).strip() not in ("{}", "None"):
            msg_text = str(msg_text).strip()
            if err_code and str(err_code).strip() and str(err_code).strip() not in msg_text:
                return f"{err_code} — {msg_text}"
            return msg_text
        detail = human or err_code or detail
    text = str(detail).strip()
    return "" if text in ("", "{}", "None") else text


def _assign_error_explanation(assign_res: dict) -> str:
    """Generic api-error string PLUS the server's response body, for assignment
    follow-ups on create/update (PLAT-4178: the operator asked that the body be
    surfaced so a failure is self-explanatory rather than a bare '400 Client Error')."""
    explanation = str(assign_res.get("error") or "assignment failed")
    detail = _error_detail_text(assign_res)
    if detail and detail not in explanation:
        explanation = f"{explanation} — {detail}"
    return explanation


def _transport_dedup_key(method: str, endpoint: str, data: Optional[dict]) -> str:
    """Stable client-side correlation key for a transport failure.

    The server may never have seen the request, so this is not a server
    idempotency token. It is the visible trace a retry can echo.
    """
    material = json.dumps(
        {"method": method, "endpoint": endpoint, "data": data or {}},
        sort_keys=True,
        default=str,
    )
    return _hashlib.sha256(material.encode()).hexdigest()[:16]


def _fmt_error(result: dict) -> str:
    """Format an error result into a user-friendly string."""
    status = result.get("status_code")
    detail = result.get("detail", "")
    retry_after = detail.get("retry_after") if isinstance(detail, dict) else None
    detail_text = _error_detail_text(result) or detail

    # PLS-523/PLS-358: preserve the server's structured failing-layer markers
    # (backend `code` + `marker`) alongside the human detail. The API names the
    # exact layer that failed in these fields (e.g. PLS358_PROJECTS_UNEXPECTED_ERROR,
    # PLS523_TRANSITION_RECEIPT_DEGRADED); dropping them left operators with only
    # "failed unexpectedly" and no way to localize the fault (banto's PLS-523
    # observation). Normalize a JSON-string body to a dict first so markers
    # survive regardless of how the body was captured.
    detail_obj = detail
    if isinstance(detail_obj, str):
        try:
            detail_obj = json.loads(detail_obj)
        except (json.JSONDecodeError, TypeError, ValueError):
            detail_obj = None
    marker_bits = []
    if isinstance(detail_obj, dict):
        if detail_obj.get("code"):
            marker_bits.append(f"code={detail_obj['code']}")
        if detail_obj.get("marker"):
            marker_bits.append(f"marker={detail_obj['marker']}")
    marker_text = (" [" + " ".join(marker_bits) + "]") if marker_bits else ""

    outage_hint = ""
    if status and int(status) >= 500:
        outage_hint = (
            "\nPulse API returned a server-side error. Treat this as transient "
            "Pulse degradation: retry with backoff and check /api/health/ "
            "before assuming the task/tool arguments are malformed."
        )
        if retry_after:
            outage_hint += f" Suggested retry_after={retry_after}s."

    transport = ""
    if result.get("error_code"):
        transport = (
            f"\n[transport error_code={result['error_code']}"
            f" retryable={bool(result.get('retryable'))}"
            f" dedup_key={result.get('dedup_key') or ''}]"
        )
        if result.get("trace"):
            transport += f"\nTrace: {result['trace']}"

    return f"Error: {result['error']}\nDetail: {detail_text}{marker_text}{outage_hint}{transport}"


# Statuses that mean "this task is done — no further action required".
# A task in any of these is terminal (not "pending on" anyone). ADR-0073:
# terminal is an EXPLICIT allow-list — never "not ready ⇒ terminal". This set
# must mirror backend tasks/services/actionable_items.py::TERMINAL_STATUS_SLUGS
# exactly (a drift-guard test enforces equality); the MCP must never maintain a
# superset.
TERMINAL_STATUSES = frozenset({
    'completed', 'cancelled', 'canceled', 'closed', 'done',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'expired', 'deferred',
    # PLS-1362: mirror the backend additions (actionable_items.py) — done-
    # category terminal dispositions that slug-keyed checks were missing.
    'applied', 'risk_accepted',
})

# ADR-0073 / PLS-862: `scheduled` is WAITING, never terminal. It is the resting
# state of a recurring task between cycles — future active work with a due
# date, the opposite of done. PLS-70's intent ("parked recurring tasks must not
# appear as actionable") is already implemented by the backend
# WAITING_QUEUE_STATUS_SLUGS + filter_active_queue_items (parks scheduled rows
# server-side for is_active=true) and the MCP local pending/not-yet-due
# suppression — NOT by a terminal label. Rows in this set are folded into the
# hidden non-terminal bucket (hidden_scheduled) and render as "waiting", never
# as actionable and never as terminal.
PARKED_WAITING_SLUGS = frozenset({'scheduled'})

# PLS-98: a task whose due_date is far in the future is future work, not "ready
# now" — it must not surface in the actionable queue (causing per-heartbeat
# triage churn, e.g. KURA-8 due 2026-11-20) until it is within this window of
# its due date. Prior fixes (PLS-70/PLS-73) parked KURA-8 behind a *blocker*,
# which un-did itself when the blocker resolved; this is a deployment-durable,
# code-level suppression keyed on the due_date itself.
#
# PLS-126: recurring tasks are stricter than one-off due-dated work. If a
# recurring occurrence is due in the future and is not already in an active work
# stage, it is scheduled/waiting, not actionable, even when the due date is only
# a couple of days out or the task is temporarily blocked by a containment link.
FAR_FUTURE_DUE_DAYS = 30
# PLS-838: priorities worth naming explicitly in the hidden-not-yet-due
# annotation. A blocked seat must be able to see an escalated item coming
# without running a second, status-filtered query.
ESCALATED_PRIORITIES = frozenset({'urgent', 'high'})
BACKLOG_PULL_READY_THRESHOLD = 3
BACKLOG_PULL_LIMIT = 5
# Heartbeat-facing queue tools default to a short, priority-ordered head so
# agents do not dump dozens of tasks into one turn (context bloat / expensive-turn).
# Callers can pass limit=N (capped) when they need a wider window.
MY_TASKS_DEFAULT_LIMIT = 5
MY_TASKS_MAX_LIMIT = 200
BACKLOG_PULL_WAITING_STATUSES = frozenset({
    'blocked', 'awaiting', 'awaiting_input', 'awaiting input',
    'awaiting_review', 'awaiting review', 'waiting', 'on_hold', 'on hold',
    'stalled', 'needs_info', 'needs info',
    # Shion 2026-09-15: HIVE-1888 awaiting_deploy was labelled ready/movable
    # so heartbeat needs_help fired while she correctly held for the deploy
    # lane. Same class as awaiting_merge — another lane owns the next step.
    'awaiting_merge', 'awaiting merge',
    'awaiting_deploy', 'awaiting deploy',
    'awaiting_verification', 'awaiting verification',
    'verification', 'deploying', 'applying',
    # PLAT-8169 arm (c) (rui 2026-09-14 evidence, nagi 2026-09-16): PLS-719's
    # block-releasing resting state — a blocker child whose ask was answered on
    # the parent stays non-terminal `resolved_elsewhere` by design ("no action
    # required in this workflow state"). The listing classifier never picked it
    # up, so every heartbeat preflight counted these rows ready/movable and the
    # assignee re-ruled no-action per cycle (live: PLS-1227, CON-438, SCLI-719,
    # CTX-946, PLAT-8012). Same class as awaiting_merge/awaiting_deploy above:
    # another lane (or no one — the ask is answered) owns the next step.
    'resolved_elsewhere',
    # PLS-1406: accepted security-finding ledgers are parked dispositions.
    'accepted', 'awaiting_operator',
})

# Only suppress far-future one-off tasks that have NOT been started. Once work is
# in flight (in_progress, in_review, awaiting_*, verification, applying, …) the
# task stays on the plate regardless of its due_date. These are the "waiting to
# be picked up" statuses where a distant due_date means not-yet-ready.
NOT_STARTED_STATUSES = frozenset({
    'open', 'todo', 'to_do', 'to do', 'backlog',
    'needs_triage', 'needs triage', 'triage', 'pending', 'proposed', 'new',
})

# Recurring future occurrences remain hidden unless somebody is actively moving
# the current work item through a delivery/review/deploy stage. Blocked/waiting
# recurring tasks are not ready queue work; showing them caused PLS-126 churn.
ACTIVE_WORK_STATUSES = frozenset({
    'in_progress', 'in progress', 'in_review', 'in review',
    'awaiting_review', 'awaiting review', 'awaiting_merge', 'awaiting merge',
    'awaiting_deploy', 'awaiting deploy', 'awaiting_verification',
    'awaiting verification', 'verification', 'deploying', 'applying',
})


def _normalize_task_title(title: str) -> str:
    import re
    return re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).strip()


def _task_title_tokens(title: str) -> set:
    stop = {
        'a', 'an', 'and', 'as', 'by', 'for', 'from', 'in', 'into', 'of',
        'on', 'or', 'that', 'the', 'this', 'to', 'was', 'with',
    }
    return {
        token for token in _normalize_task_title(title).split()
        if len(token) > 2 and token not in stop
    }


def _task_search_queries(text: str) -> list[str]:
    """Generate narrower task-search probes for long, alert-shaped queries."""
    import re
    raw = (text or "").strip()
    if not raw:
        return []

    queries: list[str] = [raw]
    lowered = raw.lower()
    nodes = re.findall(r"\b[a-z][a-z0-9]*-\d+\b", lowered)
    alert_names = re.findall(r"\bGB10[A-Za-z0-9]*(?:Temp|Thermal|Warm|Critical|Alarm)[A-Za-z0-9]*\b", raw)

    def add(q: str) -> None:
        q = q.strip()
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)

    for node in nodes:
        add(node)
        if "nic" in lowered or "connectx" in lowered:
            add(f"{node} NIC")
            add(f"{node} NIC cooling")
            add(f"{node} ConnectX")
        if any(word in lowered for word in ("thermal", "temp", "temperature", "cooling", "hwmon")):
            add(f"{node} thermal")
            add(f"{node} cooling")
        if "nvme" in lowered:
            add(f"{node} NVMe")

    for name in alert_names:
        add(name)
    if "nic" in lowered and "cooling" in lowered:
        add("NIC cooling")
    if "connectx" in lowered:
        add("ConnectX")

    tokens = [t for t in _normalize_task_title(raw).split() if len(t) > 2]
    if len(tokens) >= 3:
        add(" ".join(tokens[:3]))
    return queries[:12]


def _is_suppressed_gx10_4_thermal_task(title: str, description: str = "") -> bool:
    """Deployment-specific ignored thermal telemetry (PLAT-666)."""
    text = f"{title or ''}\n{description or ''}".lower()
    hosts = [value.strip().lower() for value in os.environ.get(
        'PULSE_SUPPRESSED_THERMAL_HOSTS', ''
    ).split(',') if value.strip()]
    if not any(host in text for host in hosts):
        return False
    thermal_signal = any(
        word in text
        for word in (
            "thermal", "temperature", " temp", "temp ", "cooling", "hwmon",
            "gb10nictemp", "node_hwmon_temp", "node_thermal_zone_temp",
        )
    )
    affected_sensor = any(word in text for word in ("nic", "connectx", "nvme", "sensor"))
    return thermal_signal and affected_sensor


def _task_status_slug(task: dict) -> str:
    return str(
        task.get("status_slug")
        or task.get("status")
        or task.get("status_name")
        or ""
    ).strip().lower()


def _is_backlog_pull_ready(task: dict) -> bool:
    return (
        task.get('owner_action_policy') != 'awareness'
        and _task_status_slug(task) not in BACKLOG_PULL_WAITING_STATUSES
    )


def _find_existing_task_candidates(title: str, project_id: int, limit: int = 10) -> list:
    """Find likely active/deferred duplicates before creating a task.

    This is deliberately conservative: exact/near-exact title matches block
    creation; weaker matches are ignored to avoid blocking legitimate subtasks.
    Deferred matches always block because they encode an intentional human
    decision to park the work.
    """
    if not title or not project_id:
        return []

    from difflib import SequenceMatcher

    wanted = _normalize_task_title(title)
    if not wanted:
        return []
    wanted_tokens = _task_title_tokens(title)

    queries = _task_search_queries(title)
    if wanted_tokens:
        keyword_query = " ".join(sorted(wanted_tokens)[:8])
        if keyword_query and keyword_query != title:
            queries.append(keyword_query)

    candidates = []
    seen = set()
    for query in queries + [None]:
        params = {
            "mode": "task",
            "project": project_id,
            "page_size": 100 if query is None else limit,
        }
        if query:
            params["search"] = query
        result = api_request("GET", "/items/", params=params)
        if "error" in result:
            continue

        for item in result.get("results", []):
            key = item.get("id") or item.get("item_key")
            if key in seen:
                continue
            existing = _normalize_task_title(
                f"{item.get('title', '')} {item.get('description', '')}"
            )
            if not existing:
                continue
            score = SequenceMatcher(None, wanted, existing).ratio()
            status = (item.get("status") or "").lower()
            active_or_deferred = status not in TERMINAL_STATUSES or status == "deferred"
            existing_tokens = _task_title_tokens(
                f"{item.get('title', '')} {item.get('description', '')}"
            )
            common = wanted_tokens & existing_tokens
            overlap = len(common) / max(min(len(wanted_tokens), len(existing_tokens)), 1)
            deferred_overlap = status == "deferred" and len(common) >= 3 and overlap >= 0.45
            active_overlap = status not in TERMINAL_STATUSES and len(common) >= 4 and overlap >= 0.60
            if active_or_deferred and (score >= 0.82 or wanted == existing or deferred_overlap or active_overlap):
                candidates.append({**item, "_similarity": score})
                seen.add(key)
    return candidates


# ── PLAT-524: Simple-workflow misfile classifier (PLAT-521 HLD §4) ──────────
# Set PULSE_SIMPLE_GUARD_DISABLED=1 to revert to pre-guard behavior (rollback).
_SIMPLE_GUARD_DISABLED = os.environ.get("PULSE_SIMPLE_GUARD_DISABLED", "").lower() in ("1", "true", "yes")

_STRONG_ITEM_TYPES = frozenset({"bug", "story", "task.bug", "task.story"})

_KEYWORD_TOKENS = frozenset({
    "review", "qa", "merge", "pr", "code", "fix",
    "implement", "deploy", "patch", "hotfix", "regression", "refactor",
    "endpoint", "bug", "defect",
})
_KEYWORD_PHRASES = frozenset({"pull request"})

_EXCLUSION_TOKENS = frozenset({
    "epic", "hld", "adr", "rfc", "design", "decision", "charter",
    "roadmap", "backlog", "triage", "sweep", "audit", "policy",
    "runbook", "research", "plan", "proposal", "signoff", "sign-off",
    "coordination", "adminops", "admin-ops", "billing", "governance",
    "analytics",
})

_CODE_TYPE_LABELS = frozenset({"code", "bug", "fix", "patch", "hotfix", "refactor"})
_BUG_KEYWORDS = frozenset({"fix", "hotfix", "regression", "defect", "bug"})
_DEV_KEYWORDS = frozenset({"implement", "deploy", "refactor", "endpoint", "code", "pr", "merge", "review", "qa"})
_RESEARCH_WIKI_INTENT_TOKENS = frozenset({
    "wiki", "documentation", "docs", "notes", "research", "read-only", "read", "readonly",
})

_ORG_LOCAL_TRIAGE_TEAM = "admin-ops"

_PR_URL_RE = None


def _pr_url_re():
    global _PR_URL_RE
    if _PR_URL_RE is None:
        import re
        _PR_URL_RE = re.compile(
            r'github\.com/[^/\s]+/[^/\s]+/pull/\d+|pull\s*request',
            re.IGNORECASE,
        )
    return _PR_URL_RE


def _word_boundary(word: str, text: str) -> bool:
    import re
    return bool(re.search(r'\b' + re.escape(word) + r'\b', text))


def _corpus(payload: dict) -> str:
    parts = [payload.get("title", ""), payload.get("description", "")]
    for field in ("labels", "tags"):
        val = payload.get(field) or []
        if isinstance(val, list):
            parts.extend(str(v) for v in val)
        else:
            parts.append(str(val))
    return " ".join(parts).lower()


def _select_target_workflow(item_type: str, text: str) -> "str | None":
    """Pick autonomous-bug or autonomous-dev; None when ambiguous."""
    itype = item_type.replace("task.", "").replace("alert.", "")
    if itype == "bug":
        return "autonomous-bug"
    if itype == "story":
        return "autonomous-dev"
    bug_score = sum(1 for kw in _BUG_KEYWORDS if _word_boundary(kw, text))
    dev_score = sum(1 for kw in _DEV_KEYWORDS if _word_boundary(kw, text))
    if bug_score > dev_score:
        return "autonomous-bug"
    if dev_score > bug_score:
        return "autonomous-dev"
    return None


def _is_research_wiki_only_task(payload: dict, text: str, labels_lower: set, team: str) -> bool:
    """Explicit Research/wiki-only work is not a simple-workflow misfile.

    PLAT-524's keyword guard intentionally treats words like "review", "code",
    and "deploy" as implementation signals. Research tasks often mention those
    words in read-only constraints ("inspect code, do not change deployments"),
    so require an explicit research route plus wiki/documentation intent before
    bypassing the heuristic.
    """
    if labels_lower & _CODE_TYPE_LABELS:
        return False

    raw_tags = payload.get("tags") or []
    if isinstance(raw_tags, list):
        tags_lower = {str(t).strip().lower() for t in raw_tags if str(t).strip()}
    else:
        tags_lower = {t.strip().lower() for t in str(raw_tags).split(",") if t.strip()}

    explicit_research = (
        team in {"research", "research-analytics"}
        or "research" in labels_lower
        or "research" in tags_lower
    )
    if not explicit_research:
        return False

    has_wiki_intent = any(_word_boundary(tok, text) for tok in _RESEARCH_WIKI_INTENT_TOKENS)
    no_change_intent = any(
        phrase in text
        for phrase in (
            "read-only",
            "read only",
            "do not change",
            "do not modify",
            "write only to wiki",
            "writes only to wiki",
            "wiki updates",
            "wiki maintenance",
        )
    )
    return has_wiki_intent and no_change_intent


def _classify_simple_workflow_misfile(payload: dict) -> dict:
    """
    PLAT-521 HLD §4 decision matrix applied at creation time.
    Only fires when workflow == 'simple'.

    Returns dict: {action, target_workflow, matched_signals, reason}
    action in {'pass', 'auto_reclassify', 'cos_triage'}
    """
    wf = (payload.get("workflow_name") or payload.get("workflow") or "").lower().strip()
    if wf != "simple":
        return {"action": "pass", "target_workflow": None, "matched_signals": [], "reason": "not simple"}

    if _SIMPLE_GUARD_DISABLED:
        return {"action": "pass", "target_workflow": None, "matched_signals": [], "reason": "guard disabled"}

    # Override: keep-simple / triaged-keep-simple suppresses guard (leads/CoS only)
    labels_raw = payload.get("labels") or []
    labels_list = labels_raw if isinstance(labels_raw, list) else [str(l).strip() for l in str(labels_raw).split(",") if str(l).strip()]
    labels_lower = {l.strip().lower() for l in labels_list}
    if labels_lower & {"keep-simple", "triaged-keep-simple"}:
        return {"action": "pass", "target_workflow": None, "matched_signals": [], "reason": "keep-simple override"}

    item_type = (payload.get("item_type") or "").lower().strip()
    priority = (payload.get("priority") or "").lower().strip()
    team = (payload.get("assignment_group") or "").lower().strip()
    text = _corpus(payload)

    if _is_research_wiki_only_task(payload, text, labels_lower, team):
        return {
            "action": "pass",
            "target_workflow": None,
            "matched_signals": ["research-wiki-only"],
            "reason": "research/wiki-only read-only task",
        }

    # §4.3 Sev-1 fast-path: critical → route directly to engineering, never park
    if priority == "critical":
        target = _select_target_workflow(item_type, text) or "autonomous-dev"
        return {
            "action": "auto_reclassify",
            "target_workflow": target,
            "matched_signals": ["priority=critical"],
            "reason": "Sev-1 fast-path — bypasses triage, routes directly to engineering",
        }

    # §4.2 devops exemption: team==devops + no code-type labels → pass
    if team == "devops":
        if not (labels_lower & _CODE_TYPE_LABELS):
            return {"action": "pass", "target_workflow": None, "matched_signals": ["team=devops,no-code-labels"], "reason": "devops infra exemption (PLAT-511)"}
        return {
            "action": "cos_triage",
            "target_workflow": None,
            "matched_signals": [f"team=devops,code-labels={sorted(labels_lower & _CODE_TYPE_LABELS)}"],
            "reason": "devops + code/bug labels — borderline, CoS decides",
        }

    # §4.2 Exclusion allowlist
    exclusions = [tok for tok in _EXCLUSION_TOKENS if _word_boundary(tok, text)]
    if item_type in ("epic", "task.epic"):
        exclusions.append("item_type=epic")

    # §4.2 STRONG signals
    strong = []
    itype_base = item_type.replace("task.", "").replace("alert.", "")
    if itype_base in ("bug", "story") or item_type in _STRONG_ITEM_TYPES:
        strong.append(f"item_type={item_type}")
    combined = (payload.get("description", "") + " " + payload.get("title", ""))
    if _pr_url_re().search(combined):
        strong.append("linked-pr")

    # §4.3 STRONG + no exclusion → auto-reclassify
    if strong and not exclusions:
        target = _select_target_workflow(item_type, text)
        if target:
            return {"action": "auto_reclassify", "target_workflow": target, "matched_signals": strong, "reason": "strong signal, no exclusion"}
        return {"action": "cos_triage", "target_workflow": None, "matched_signals": strong, "reason": "strong signal but target ambiguous"}

    # §4.3 STRONG + exclusion → CoS-triage
    if strong and exclusions:
        return {"action": "cos_triage", "target_workflow": None, "matched_signals": strong + [f"excl:{e}" for e in exclusions], "reason": "strong+exclusion — genuinely ambiguous"}

    # §4.2 Keyword signals
    kw_hits = [kw for kw in _KEYWORD_TOKENS if _word_boundary(kw, text)]
    kw_hits += [ph for ph in _KEYWORD_PHRASES if ph in text]
    kw_hits = list(dict.fromkeys(kw_hits))  # dedup, preserve order

    if not kw_hits:
        return {"action": "pass", "target_workflow": None, "matched_signals": [], "reason": "no signals"}

    # keyword + exclusion → CoS-triage
    if exclusions:
        return {"action": "cos_triage", "target_workflow": None, "matched_signals": kw_hits + [f"excl:{e}" for e in exclusions], "reason": "keyword+exclusion — genuine ambiguity"}

    # ≥2 keywords, no exclusion → auto-reclassify (or CoS if target ambiguous)
    if len(kw_hits) >= 2:
        target = _select_target_workflow(item_type, text)
        if target:
            return {"action": "auto_reclassify", "target_workflow": target, "matched_signals": kw_hits, "reason": "≥2 keyword signals, no exclusion"}
        return {"action": "cos_triage", "target_workflow": None, "matched_signals": kw_hits, "reason": "≥2 keywords but target ambiguous"}

    # 1 keyword only → pass (too noisy; §4.3)
    return {"action": "pass", "target_workflow": None, "matched_signals": kw_hits, "reason": "single keyword — too noisy to act"}
# ── end PLAT-524 classifier ──────────────────────────────────────────────────


def _filter_out_terminal(items: list) -> tuple:
    """
    Return (filtered_items, num_hidden). Removes items whose status is in
    TERMINAL_STATUSES. Case-insensitive. This implements the default
    behavior for "list tasks assigned to me" style queries — terminal
    tasks (completed, rejected, etc.) are NOT "on your plate" and should
    not pollute the default answer.
    """
    if not items:
        return [], 0
    kept = [
        it for it in items
        if (it.get('status') or '').lower() not in TERMINAL_STATUSES
    ]
    return kept, len(items) - len(kept)


def _parse_due_date(value):
    """Parse an ISO due_date string into a datetime, or None. Tolerates a
    trailing 'Z' and date-only values."""
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime
    s = value.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10])  # date-only fallback
        except ValueError:
            return None


def _humanize_delta(delta) -> str:
    """Render a timedelta as a short human distance: `3h53m`, `4d 17h`, `23d`."""
    secs = int(delta.total_seconds())
    if secs < 0:
        return "overdue"
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        h, m = divmod(secs // 60, 60)
        return f"{h}h{m:02d}m"
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    return f"{days}d" if days >= 7 or hours == 0 else f"{days}d {hours}h"


def _describe_hidden_scheduled(hidden, now=None) -> str:
    """PLS-838: describe the not-yet-due bucket from the ACTUAL rows in it.

    The previous text was a fixed ``(>{FAR_FUTURE_DUE_DAYS}d out)`` label
    attached to the whole bucket. That describes only ONE of the two
    suppression rules in ``_filter_far_future_due`` — the far-future one. The
    other rule (PLS-126) hides a future-due *recurring* occurrence at ANY
    distance, so an urgent item due in under four hours was being reported as
    ">30d out". On a seat whose visible item is blocked, that annotation is the
    entire remaining signal about the queue, and it read as "stand down".

    Returns a phrase describing the real minimum distance, naming an
    urgent/high item when one is hidden. Accepts a bare int for callers that
    only have a count — in which case it deliberately states NO distance rather
    than guessing one.

    ``now`` is injectable so tests can assert an exact rendered distance.
    Without it a fixture built at ``now + 3h53m`` renders ``3h52m`` once a few
    milliseconds elapse, because the distance is floored — a real flake, not a
    hypothetical one: it failed this suite on the first run.
    """
    if isinstance(hidden, int):
        return f"{hidden} not yet due" if hidden > 0 else ""
    if not hidden:
        return ""

    from datetime import datetime, timezone
    if now is None:
        now = datetime.now(timezone.utc)
    dated = []
    for it in hidden:
        due = _parse_due_date(it.get('due_date'))
        if due is None:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        dated.append((due, it))

    base = f"{len(hidden)} not yet due"
    if not dated:
        # Hidden rows with no parseable due date: say so, claim no distance.
        return base
    dated.sort(key=lambda pair: pair[0])

    def _describe(pair) -> str:
        due, it = pair
        key = it.get('item_key') or f"#{it.get('id')}"
        pri = (it.get('priority') or '').lower()
        suffix = f" ({pri})" if pri in ESCALATED_PRIORITIES else ""
        return f"{key}{suffix} in {_humanize_delta(due - now)}"

    parts = [f"{base}; soonest {_describe(dated[0])}"]
    escalated = [p for p in dated if (p[1].get('priority') or '').lower() in ESCALATED_PRIORITIES]
    if escalated and escalated[0] is not dated[0]:
        parts.append(f"nearest {escalated[0][1].get('priority', '').lower()} {_describe(escalated[0])}")
    return ", ".join(parts)


def _filter_far_future_due(items: list) -> tuple:
    """
    Return (filtered_items, hidden_items). PLS-98: drop NOT-yet-started tasks whose
    due_date is more than FAR_FUTURE_DUE_DAYS out — they are future work, not
    actionable now, and must not churn the heartbeat queue. PLS-126: for
    recurring tasks, drop any future-due occurrence unless it is already in an
    active work stage; a future recurrence is scheduled/waiting even when the due
    date is within the normal 30-day planning window. Tasks with no due_date are
    kept (unchanged from today's behavior).
    """
    if not items:
        # PLS-838: hidden side is always a row list (never a bare count).
        return [], []
    # Use the Django clock, not the raw wall clock: the rest of the app reads
    # django.utils.timezone.now (which the test suite freezes), and a raw
    # datetime.now() here made _filter_far_future_due immune to that freeze —
    # time-bombing every test whose fixture due dates the real calendar
    # eventually crosses (test_recurring_initial_owner_readiness).
    from datetime import timedelta
    from django.utils import timezone as _dj_tz
    now = _dj_tz.now()
    cutoff = now + timedelta(days=FAR_FUTURE_DUE_DAYS)

    def _is_future_waiting(it) -> bool:
        status = (it.get('status') or '').lower()
        # ADR-0073/PLS-862: a parked-waiting status (scheduled) is never
        # default-ready — regardless of due date or recurring flag. The backend
        # already parks these rows for is_active=true; this keeps rows that
        # still reach the MCP (legacy/live paths, explicit multi-status fetches)
        # out of the actionable set and puts them in the non-terminal hidden
        # bucket instead (they are NOT terminal).
        if status in PARKED_WAITING_SLUGS:
            return True
        due = _parse_due_date(it.get('due_date'))
        if due is None:
            return False  # no due date → actionable, as before
        if due.tzinfo is None:
            # PLAT-8903 gate fix: the PLS-258 refactor split _is_future_waiting
            # out of the function that held the local 'from datetime import
            # datetime, timezone' import, leaving bare 'timezone' unbound here —
            # NameError on every naive due date, reding mcp-contract-tests
            # deterministically (main run 6352 at 2969a295; fixed on main by
            # ccc0d82, which this branch's main-merge predated). Use the
            # module-level 'import datetime as _dt' alias, consistent with the
            # other module uses.
            due = due.replace(tzinfo=_dt.timezone.utc)  # treat naive as UTC

        is_recurring = bool(it.get('is_recurring'))
        if is_recurring and due > now and status not in ACTIVE_WORK_STATUSES:
            return True

        return status in NOT_STARTED_STATUSES and due > cutoff

    kept = [it for it in items if not _is_future_waiting(it)]
    # PLS-838: return the hidden ROWS, not just a count. The annotation has to
    # be computed from them; a count alone is what forced the old static label.
    hidden = [it for it in items if _is_future_waiting(it)]
    return kept, hidden


def _filter_default_active_items(items: list) -> tuple[list, int, list]:
    """Apply MCP-side compatibility filters for default active queue queries.

    The Pulse API handles terminal/backlog/scheduled status exclusion for
    `is_active=true`, but the MCP still has local due-date suppression for
    far-future/future-recurring rows. Keep that local filtering in one place so
    callers can paginate until they have `limit` *visible* rows instead of
    letting a hidden row consume a small API page (PLS-405).
    """
    kept, hidden_terminal = _filter_out_terminal(items)
    kept, hidden_scheduled = _filter_far_future_due(kept)
    return kept, hidden_terminal, hidden_scheduled  # hidden_scheduled is a list of rows (PLS-838)


def _visible_page_size(limit: int) -> int:
    """Fetch enough rows that small visible limits survive local filtering."""
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = 20
    requested = max(1, requested)
    return min(200, max(requested, max(20, requested * 5)))


def _fetch_complementary_parked_scheduled(params: dict, *, as_user: bool = False) -> list:
    """ADR-0073 / PLS-862: account for `scheduled` rows the API parks.

    On the default-active (`is_active=true`) path the backend deliberately
    EXCLUDES scheduled/backlog rows server-side (WAITING_QUEUE_STATUS_SLUGS +
    filter_active_queue_items). They therefore never reach the MCP page — so a
    future recurring compliance item (e.g. a GST cycle) appears in no bucket at
    all: not shown, not hidden_scheduled, not hidden_terminal.

    ADR option (a): run an MCP-side complementary query scoped to the same
    assignee (when one is in scope) with `status='scheduled'` so those rows are
    ACCOUNTED in hidden_scheduled — the non-terminal parked bucket — rather
    than silently dropped. Scoped to assignee queries only: a global parked
    count is not the "assigned scope" invariant, and an unscoped schedule
    sweep would be expensive and meaningless.
    """
    if not (params.get('assignee_email') or params.get('assignee_id')):
        return []
    scheduled_params = {
        'mode': 'task',
        'status': 'scheduled',
        'page_size': 200,
        'queue_order': 'true',
        # Match the main default-active path: skip the hidden-by-permission
        # diagnostic count (PLS-698) so we do not double the control-plane
        # query on every heartbeat just to compute data this caller discards.
        'permission_hint': 'false',
    }
    # Carry the same narrowing scope so the parked-item accounting matches the
    # visible queue's assignee/workflow/team/project/etc. (not a global sweep).
    for key in ('assignee_email', 'assignee_id', 'workflow_name', 'workflow',
                'team', 'assignment_group', 'priority', 'project', 'sprint_id',
                'component_id', 'labels', 'tags'):
        if params.get(key):
            scheduled_params[key] = params[key]
    result = api_request('GET', '/items/', params=scheduled_params, as_user=as_user)
    if 'error' in result:
        # Stay quiet on error: a parked-count outage must not break the queue.
        return []
    return result.get('results', []) or []


def _extend_hidden_scheduled_dedupe(hidden: list, extra: list) -> list:
    """Extend the hidden_scheduled row list, dropping duplicate item ids.

    The complementary parked query and the in-page future-waiting filter could
    see the same recurring row (e.g. a future recurring row surfaced by a
    legacy API while the complementary parked query also returns it) — never
    double-count an item in the hidden bucket.
    """
    seen = {it.get('id') for it in hidden if it.get('id') is not None}
    for it in extra:
        if it.get('id') is not None and it['id'] in seen:
            continue
        hidden.append(it)
        if it.get('id') is not None:
            seen.add(it['id'])
    return hidden


def _fetch_default_active_page(
    params: dict,
    limit: int,
    *,
    as_user: bool = False,
) -> tuple[dict, list, int, list]:
    """Fetch default-active task pages and apply local filters before `limit`.

    PLS-405 root cause: tools requested `page_size=limit`, then filtered
    terminal/future-scheduled rows locally. With `limit=1`, one hidden future
    recurring row could occupy the only API slot and make the tool report an
    empty queue even when ready work existed on the next API row/page.

    This helper over-fetches modestly and follows pagination until it has the
    requested number of visible rows (or the backend runs out). The returned
    hidden_terminal is a count; hidden_scheduled is the row list (PLS-838) so
    the annotation can be computed from actual contents.
    """
    try:
        requested = max(1, int(limit))
    except (TypeError, ValueError):
        requested = 20

    visible: list = []
    hidden_terminal = 0
    hidden_scheduled: list = []   # PLS-838: rows, so the annotation can be computed
    result: dict = {}
    page_size = _visible_page_size(limit)
    page = 1
    max_pages = 10  # safety cap: 10 * 200 = 2k inspected rows

    while page <= max_pages and len(visible) < requested:
        page_params = {**params, "page_size": page_size, "page": page}
        result = api_request("GET", "/items/", params=page_params, as_user=as_user)
        if "error" in result:
            return result, [], 0, []
        page_items = result.get("results", [])
        kept, hidden, hidden_future = _filter_default_active_items(page_items)
        hidden_terminal += hidden
        hidden_scheduled.extend(hidden_future)
        needed = requested - len(visible)
        visible.extend(kept[:needed])
        if len(visible) >= requested or not result.get("next"):
            break
        page += 1

    # PLS-862 / ADR-0073: the default-active (`is_active=true`) API path parks
    # `scheduled` rows server-side, so recurring compliance items between cycles
    # never appear in any bucket. Fold an assignee-scoped complementary
    # `status=scheduled` query into hidden_scheduled so the accounting invariant
    # (shown + hidden_non_terminal + hidden_terminal == assigned scope) holds.
    # Only for assignee-scoped default-active requests (get_my_tasks /
    # get_user_tasks / list_tasks with an assignee).
    if params.get('is_active', '').lower() == 'true':
        parked = _fetch_complementary_parked_scheduled(params, as_user=as_user)
        hidden_scheduled = _extend_hidden_scheduled_dedupe(hidden_scheduled, parked)

    return result, visible, hidden_terminal, hidden_scheduled


def _fmt_pr_links(item: dict, include_url: bool = False) -> str:
    """
    Extract a compact `PRs: shizuha-labs/connect#15 (open), ...` line from either
    the flat `pull_requests` list (ItemListSerializer) or the nested
    `metadata.pull_requests` array (ItemSerializer for single-item fetches).

    PLS-220: ALWAYS qualify each PR with its stored `owner/repo`. A bare `#N` is
    ambiguous across ~30 org repos (PR numbers collide constantly), and the
    review flow resolved bare numbers against a default repo (shizuha-tasks),
    bouncing valid PRs on the wrong repo (CON-131, HIVE-331). The repo is stored
    on the link record by `link_pr`; never drop it at the render layer. When a
    legacy link has no stored repo, fall back to the full URL if present, else
    the bare number as a last resort. `include_url=True` (detail view) also
    appends the canonical URL so the exact PR is never ambiguous.
    """
    prs = item.get('pull_requests')
    if not isinstance(prs, list) or not prs:
        md = item.get('metadata') or {}
        if isinstance(md, dict):
            prs = md.get('pull_requests') or []
    if not isinstance(prs, list) or not prs:
        return ""
    parts = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        num = pr.get('number')
        status = pr.get('status') or 'open'
        repo = pr.get('repo')
        url = pr.get('url')
        if num and repo:
            ref = f"{repo}#{num}"
        elif num:
            # Legacy link without a stored repo: prefer the full URL over a
            # bare, ambiguous number so the consumer can't mis-resolve it.
            ref = url or f"#{num}"
        elif url:
            ref = url
        else:
            continue
        piece = f"{ref} ({status})"
        if include_url and url and ref != url:
            piece += f" — {url}"
        parts.append(piece)
    return ", ".join(parts)


def _fmt_origin_observations(item: dict, include_url: bool = False) -> str:
    """Render immutable Origin evidence without ambiguous bare run numbers."""
    observations = item.get('origin_observations')
    if not isinstance(observations, list) or not observations:
        metadata = item.get('metadata') or {}
        if isinstance(metadata, dict):
            observations = metadata.get('origin_observations') or []
    try:
        observations = normalize_origin_observations(observations)
    except ValueError:
        return ""

    parts = []
    for observation in observations:
        repo = observation.get('repo') or 'unknown-repo'
        sha = observation.get('commit_sha')
        ref = f"{repo}@{sha[:12]}" if sha else repo
        details = []
        if observation.get('ref'):
            details.append(f"ref={observation['ref']}")
        if observation.get('workflow'):
            details.append(f"workflow={observation['workflow']}")
        if observation.get('run_number'):
            details.append(f"run_number={observation['run_number']}")
        if observation.get('run_id'):
            details.append(f"run_id={observation['run_id']}")
        if sha:
            details.append(f"commit_sha={sha}")
        if observation.get('job_id'):
            details.append(f"job={observation['job_id']}")
        if observation.get('check'):
            details.append(f"check={observation['check']}")
        if observation.get('status'):
            details.append(f"status={observation['status']}")
        piece = ref + (f" ({', '.join(details)})" if details else "")
        if include_url and observation.get('run_url'):
            piece += f" — {observation['run_url']}"
        logs_url = origin_ci_logs_url(observation)
        if logs_url:
            piece += f" — Logs (GET, Shizuha ID bearer): {logs_url}"
        parts.append(piece)
    return "; ".join(parts)


def _forgejo_token() -> str:
    return (
        os.environ.get("FORGEJO_TOKEN")
        or os.environ.get("ORIGIN_TOKEN")
        or os.environ.get("GITEA_TOKEN")
        or os.environ.get("TEA_TOKEN")
        or ""
    )


def _current_state_staleness(item: dict, pin_ts: Optional[str]) -> Optional[str]:
    """PLS-518 staleness nudge: if substantive (non-system) comments are newer
    than the pinned current-state timestamp, prompt the assignee to refresh the
    pin. Fail-open — any fetch/parse error yields no nudge, never a broken
    get_task. System/lifecycle comments (failover, rebalance, auto-block) do NOT
    count: they are exactly the noise the pin exists to cut through."""
    if not pin_ts:
        return None
    try:
        item_pk = item.get('id')
        if not item_pk:
            return None
        r = api_request(
            "GET", "/comments/",
            params={"item": item_pk, "page_size": 50, "page": 1},
        )
        if "error" in r:
            return None
        results = r.get("results") or []
        newer = 0
        for c in results:
            if c.get("is_system"):
                continue
            c_ts = c.get("created_at")
            if c_ts and c_ts > pin_ts:
                newer += 1
        if newer:
            return (
                f"Pinned current-state may be stale: {newer} substantive "
                f"comment(s) landed after the pin. Refresh it via "
                f"pulse_update_task(current_state=...) when you park or hand off."
            )
    except Exception:
        return None
    return None


def _enrich_origin_observation(observation: dict) -> dict[str, str]:
    """Resolve a run URL to immutable Forgejo run provenance when possible.

    The caller-supplied fields remain a fail-open fallback when Origin or its
    token is unavailable; task creation must not be lost because enrichment is
    temporarily down.
    """
    normalized = normalize_origin_observation(observation)
    parsed = parse_origin_run_url(normalized.get('run_url', ''))
    repo = normalized.get('repo') or parsed.get('repo')
    run_number = normalized.get('run_number') or parsed.get('run_number')
    token = _forgejo_token()
    if not (repo and run_number and token):
        return normalized

    owner, repo_name = repo.split('/', 1)
    try:
        response = requests.get(
            f"{_origin_api_base()}/repos/{owner}/{repo_name}/actions/runs",
            # Forgejo exposes an exact run_number filter. The UI URL contains
            # index_in_repo, not the global DB id, so this resolves even old
            # runs without paging/relying on a recent-runs window.
            params={'run_number': int(run_number), 'limit': 5},
            headers={'Authorization': f'token {token}', 'Accept': 'application/json'},
            timeout=15,
        )
        response.raise_for_status()
        runs = response.json().get('workflow_runs') or []
        matches = [
            candidate for candidate in runs
            if str(candidate.get('index_in_repo') or '') == str(run_number)
            and (not normalized.get('run_id')
                 or str(candidate.get('id')) == normalized['run_id'])
            and (not normalized.get('commit_sha')
                 or str(candidate.get('commit_sha') or '').startswith(normalized['commit_sha']))
        ]
        run = matches[0] if len(matches) == 1 else None
        if run:
            normalized.update({
                key: str(value).strip()
                for key, value in {
                    'repo': repo,
                    'run_id': run.get('id'),
                    'run_number': run.get('index_in_repo') or run_number,
                    'commit_sha': run.get('commit_sha'),
                    'ref': run.get('prettyref'),
                    'workflow': run.get('workflow_id'),
                    'status': run.get('status'),
                    'observed_at': run.get('stopped') or run.get('updated') or run.get('created'),
                }.items()
                if value not in (None, '', '1970-01-01T00:00:00Z')
            })
    except Exception:
        logger.warning(
            "Origin observation enrichment failed for %s run %s; preserving caller evidence",
            repo, run_number, exc_info=True,
        )
    return normalize_origin_observation(normalized)


SCHEMA_REPAIR_NEXT_ACTION = (
    "Next action: retry the parent transition with the typed evidence JSON "
    "already in this ticket. Do not pulse_get_task this key again — "
    "call pulse_get_available_transitions on the parent, then pulse_transition_task."
)


def _is_schema_repair_item(item: dict) -> bool:
    title = str(item.get("title") or "")
    if title.startswith("[SCHEMA REPAIR:"):
        return True
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return bool(metadata.get("escalation_schema_repair"))


def _schema_repair_next_action_line(item: dict) -> str:
    if not _is_schema_repair_item(item):
        return ""
    return SCHEMA_REPAIR_NEXT_ACTION


def _fmt_items(items: list, label: str = "task", hidden_terminal: int = 0, hidden_scheduled=0) -> str:
    """
    Format a list of items into readable output.

    Output shape:
        Found N task(s) — A active (R ready/movable, B blocked/waiting), T terminal [— Z hidden terminal]

        - **KEY**: Title
          Status: s | Priority: p
          Workflow: ...
          Assignee: ...
          PRs: #N (open), ...    ← only when linked
          Due: ...

    The `A active (R ready/movable, B blocked/waiting), T terminal` header lets
    the agent see at a glance whether anything is movable right now. PLS-464:
    the heartbeat execution gate is READY/MOVABLE work, so the summary must
    separate ready work from blocked/waiting work instead of lumping both under
    a single "actionable" label. `active` = non-terminal total (ready +
    blocked/waiting). `ready/movable` = non-terminal, not blocked/waiting, not
    parked. `blocked/waiting` = non-terminal statuses in
    BACKLOG_PULL_WAITING_STATUSES or PARKED_WAITING_SLUGS (scheduled recurring
    rows are waiting, never terminal — ADR-0073). `terminal` = the explicit
    TERMINAL_STATUSES allow-list. When `hidden_terminal > 0`, a note tells the
    agent that more terminal items exist but were hidden by the default filter
    — so the agent can mention the count and the user can ask for the full list
    if they care.
    """
    # Empty-result case — be deliberately quiet about hidden items.
    # We still give a count so the agent can mention it if the human
    # asks a follow-up, but we do NOT nudge the agent toward setting
    # include_terminal=true — that's a user-initiated override, not
    # a default move.
    if not items:
        notes = []
        if hidden_terminal > 0:
            notes.append(f"{hidden_terminal} prior {label}(s) in terminal states")
        scheduled_note = _describe_hidden_scheduled(hidden_scheduled)
        if scheduled_note:
            notes.append(scheduled_note)
        if notes:
            return (
                f"No ready/movable {label}s found. "
                f"({'; '.join(notes)} exist but are hidden by default.)"
            )
        return f"No {label}s found."

    blocked_waiting = sum(
        1 for it in items
        if (it.get('status') or '').lower() not in TERMINAL_STATUSES
        and it.get('owner_action_policy') != 'awareness'
        and (
            (it.get('status') or '').lower() in BACKLOG_PULL_WAITING_STATUSES
            or (it.get('status') or '').lower() in PARKED_WAITING_SLUGS
        )
    )
    terminal = sum(
        1 for it in items
        if (it.get('status') or '').lower() in TERMINAL_STATUSES
    )
    active = len(items) - terminal
    awareness = sum(
        1 for item in items
        if (item.get('status') or '').lower() not in TERMINAL_STATUSES
        and item.get('owner_action_policy') == 'awareness'
    )
    ready = active - blocked_waiting - awareness
    awareness_label = f', {awareness} awareness-only' if awareness else ''
    header = (
        f"Found {len(items)} {label}(s) — {active} active "
        f"({ready} ready/movable, {blocked_waiting} blocked/waiting{awareness_label}), "
        f"{terminal} terminal"
    )
    extras = []
    if hidden_terminal > 0:
        extras.append(f"{hidden_terminal} more in terminal states")
    scheduled_note = _describe_hidden_scheduled(hidden_scheduled)
    if scheduled_note:
        extras.append(scheduled_note)
    if extras:
        header += f" (+{', '.join(extras)}, hidden by default)"
    output = header + ".\n\n"

    for item in items:
        item_key = item.get('item_key')
        pk = item.get('id')
        key = item_key or f"#{pk}"
        output += f"- **{key}**: {item.get('title', 'Untitled')}\n"
        # PLS-266: surface the numeric primary key so callers can use it directly
        # as parent_item_id (pulse_create_task) or the `item` field on write
        # endpoints without a separate pulse_get_task round-trip. Skip when there
        # is no distinct item_key (the key line already shows #<pk>).
        if item_key and pk is not None:
            output += f"  ID: {pk}\n"
        next_action = _schema_repair_next_action_line(item)
        if next_action:
            output += f"  {next_action}\n"
        output += f"  Status: {item.get('status', 'unknown')} | Priority: {item.get('priority', 'normal')}\n"
        if item.get('owner_action_policy') == 'awareness':
            output += '  Owner action: awareness-only (retained ownership; no action required in this workflow state)\n'
        if item.get("workflow_name"):
            output += f"  Workflow: {item['workflow_name']} (status: {item.get('workflow_status', '?')})\n"
        if item.get("assignment_group"):
            output += f"  Team: {item['assignment_group']}\n"
        if item.get("assignee_email"):
            output += f"  Assignee: {item['assignee_email']}\n"
        containment = item.get('containment_decision')
        if isinstance(containment, dict):
            output += (
                f"  Containment: {containment.get('state', 'unknown')} "
                f"(v{containment.get('version', '?')}, owner="
                f"{containment.get('owner_email') or containment.get('owner_group') or 'unresolved'})\n"
            )
        pr_line = _fmt_pr_links(item)
        if pr_line:
            output += f"  PRs: {pr_line}\n"
        origin_line = _fmt_origin_observations(item)
        if origin_line:
            output += f"  Observed on Origin: {origin_line}\n"
        if item.get("due_date"):
            output += f"  Due: {item['due_date']}\n"
        output += "\n"
    return output


def _get_my_user_id() -> tuple[Optional[int], Optional[str]]:
    """Get current agent's user_id and email from JWT."""
    import jwt
    token = _get_effective_agent_token()
    if not token:
        return None, None
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload.get("user_id"), payload.get("email")
    except Exception:
        return None, None


# =============================================================================
# IDENTITY
# =============================================================================

@mcp.tool(name="pulse_get_user_identity")
def get_user_identity() -> str:
    """
    Get the chatting user's identity (the human you are assisting).
    Use id_get_my_identity for YOUR (the agent's) own identity.
    """
    import jwt
    token = _get_effective_user_token()
    if not token:
        return "No user context available. The user has not shared their identity."
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        identity = {
            "user_id": payload.get("user_id"),
            "email": payload.get("email"),
            "username": payload.get("username"),
            "organization_id": payload.get("organization_id"),
            "note": "This is the human user you are assisting.",
        }
        return json.dumps(identity, indent=2)
    except Exception as e:
        return f"Error decoding user token: {e}"


# =============================================================================
# TASKS / ITEMS - CRUD
# =============================================================================

@mcp.tool(name="pulse_list_tasks")
def list_tasks(
    status: Optional[str] = None,
    workflow: Optional[str] = None,
    team: Optional[str] = None,
    priority: Optional[str] = None,
    project_id: Optional[int] = None,
    assignee_id: Optional[int] = None,
    assignee_email: Optional[str] = None,
    sprint_id: Optional[str] = None,
    component_id: Optional[str] = None,
    # PLS-712: FastMCP/Pydantic eagerly decodes JSON-looking arguments.  A
    # string-only annotation rejects a valid JSON array/object before this tool
    # can pass it to the backend's dual-shape filter parser.
    labels: Optional[Union[str, List[str], dict[str, str]]] = None,
    tags: Optional[str] = None,
    jql: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    List tasks in active states. This tool returns ONLY tasks in active states
    (pending, in_progress, in_review, awaiting_*, etc.) — terminal tasks
    (completed, rejected, cancelled, closed, done, resolved, duplicate,
    wont_fix, merged, failed, expired, deferred) are NEVER returned unless you
    pass an explicit `status=` filter naming them.

    Default queue order is ready work first, then highest priority first
    (urgent > high > normal/medium > low), then FIFO by creation time inside
    that priority bucket.

    This is the correct tool for: "what's pending on me", "what's
    assigned to X", "what's on my plate", "what am I working on".

    To query historical or closed work, pass an explicit `status` filter,
    e.g. `status="rejected"` for rejected findings, `status="completed"`
    for finished tasks, or `status="completed,rejected"` for both. You
    can also use `jql` for more complex queries.

    To scope to a specific workflow's statuses (e.g. find all 'design' tasks
    in the bug workflow), combine workflow + status:
        `workflow="autonomous-bug", status="design"`
    Without a workflow filter, `status="design"` matches across ALL workflows
    that use a status slug named "design" — which is usually fine but can
    produce surprising cross-workflow results.

    Args:
        status: Filter by status slug (e.g. 'in_progress', 'design', 'in_qa'). Comma-separated allowed. If omitted, terminal statuses are excluded.
        workflow: Filter by workflow name or slug (e.g. 'autonomous-bug', 'bug-workflow', 'simple'). Use with status= to scope to workflow-specific statuses.
        team: Filter by team / assignment_group (e.g. 'engineering', 'review', 'security').
        priority: Filter by priority (low, normal, high, urgent)
        project_id: Filter by project ID
        assignee_id: Filter by assignee user ID
        assignee_email: Filter by assignee email (e.g., 'operator@example.test', 'kai@shizuha.com')
        sprint_id: Filter by sprint ID
        component_id: Filter by component ID
        labels: Filter by labels: comma-separated string, JSON/string array, or
                key/value object. Tokens are ANDed and match either persisted
                array or object labels.
        tags: Filter by tags (comma-separated)
        jql: JQL query string for advanced filtering (e.g., "status = 'in_progress' AND priority = 'high'")
        limit: Maximum number of tasks (default 20)
    """
    params = {"mode": "task", "page_size": limit, "queue_order": "true"}
    if status:
        params["status"] = status
    else:
        # Filter terminal/scheduled rows in the API before pagination so old
        # active work cannot be pushed out of the MCP page by newer closed work.
        params["is_active"] = "true"
    if workflow: params["workflow_name"] = workflow
    if team: params["team"] = team
    if priority: params["priority"] = priority
    if project_id: params["project"] = project_id
    if assignee_id: params["assignee_id"] = assignee_id
    if assignee_email: params["assignee_email"] = assignee_email
    if sprint_id: params["sprint"] = sprint_id
    if component_id: params["component"] = component_id
    if labels:
        params["labels"] = (
            json.dumps(labels, separators=(",", ":"))
            if isinstance(labels, (list, dict))
            else labels
        )
    if tags: params["tags"] = tags
    if jql: params["jql"] = jql

    if status:
        result = api_request("GET", "/items/", params=params)
        if "error" in result:
            return _fmt_error(result)
        items = result.get("results", [])
        hidden = 0
        hidden_future = 0
    else:
        result, items, hidden, hidden_future = _fetch_default_active_page(params, limit)
        if "error" in result:
            return _fmt_error(result)

    output = _fmt_items(items, hidden_terminal=hidden, hidden_scheduled=hidden_future)
    # Surface the permission-filtered hint when the backend signals that
    # items matching the query exist but are not visible to the calling
    # agent (cross-org or private-project items). This prevents "No tasks
    # found." from being misread as "nothing exists." (PLS-24).
    if result.get("permission_filtered") and not items:
        hidden_perm = result.get("hidden_by_permission", 0)
        if hidden_perm:
            output += (
                f"\n\n⚠️  {hidden_perm} task(s) exist but are hidden by permission "
                f"(private project or org boundary). Use a more privileged token or "
                f"refine with assignee_email= to scope to accessible items."
            )
        else:
            output += (
                "\n\n(Note: permission scoping is active — some items may be hidden "
                "if they are in private projects or outside your org membership.)"
            )
    return output


@mcp.tool(name="pulse_get_task")
def get_task(task_identifier: str = "", item_key: str = "", task_key: str = "",
             task_id: str = "", item_id: str = "", key: str = "", id: str = "") -> str:
    """
    Get detailed information about a task by ID (UUID) or item_key (e.g., 'PROJ-123').
    Returns full task details including description, labels, story points, and metadata.

    Pass the key as the first positional arg — `pulse_get_task('PLAT-123')` — or via any
    of these accepted names: task_identifier, item_key, task_key, task_id, item_id, key, id.
    (They are aliases; sibling tools use task_id/item_id, so all are tolerated here.)

    You MUST pass a specific key. If you want to know which tasks are assigned to YOU
    (e.g. on a heartbeat — "what should I work on?"), do NOT call this tool with no/empty
    id — call `pulse_get_my_tasks` instead (defaults to the top few queue-ordered tasks).
    """
    # PLS-61: callers naturally pass the key under item_key/task_key/task_id/item_id
    # (the names sibling tools use), not only `task_identifier`. fastmcp silently drops
    # unknown kwargs, so a wrong-name call used to leave task_identifier="" and hit the
    # "no id" branch — making the tool look completely broken. Coalesce all aliases.
    task_identifier = next(
        (str(v).strip() for v in
         (task_identifier, item_key, task_key, task_id, item_id, key, id)
         if v and str(v).strip()),
        "")
    # Tolerate the common agent mistake of calling get_task with no id (esp. on a
    # heartbeat). Returning actionable guidance instead of an opaque validation error
    # stops the empty-args retry loop and points the agent at the right tool.
    if not task_identifier:
        return ("No task key provided. `pulse_get_task` fetches ONE specific task by its "
                "item_key — pass it as the first positional arg, e.g. pulse_get_task('PLAT-123') "
                "(or via task_identifier/item_key/task_key/task_id/item_id). To see the tasks "
                "assigned to YOU, call `pulse_get_my_tasks` (optional limit=N) — then call "
                "pulse_get_task with one of the returned item_keys if you need full detail.")
    result = api_request("GET", _item_path(task_identifier))
    if "error" in result:
        return _fmt_error(result)

    t = result
    output = f"# {t.get('item_key', 'N/A')}: {t.get('title', 'Untitled')}\n\n"
    output += f"**Status**: {t.get('status')} | **Priority**: {t.get('priority')}\n"
    if t.get("workflow_name"): output += f"**Workflow**: {t['workflow_name']} (status: {t.get('workflow_status', '?')})\n"
    if t.get("assignment_group"): output += f"**Team**: {t['assignment_group']}\n"
    if t.get("project_name"): output += f"**Project**: {t['project_name']}\n"
    if t.get("assignee_email"): output += f"**Assignee**: {t['assignee_email']}\n"
    if t.get("created_by_email"): output += f"**Reporter**: {t['created_by_email']}\n"
    if t.get("due_date"): output += f"**Due**: {t['due_date']}\n"
    if t.get("sprint_name"): output += f"**Sprint**: {t['sprint_name']}\n"
    if t.get("story_points"): output += f"**Story Points**: {t['story_points']}\n"
    if t.get("resolution"): output += f"**Resolution**: {t['resolution']}\n"
    if t.get("labels"): output += f"**Labels**: {', '.join(t['labels']) if isinstance(t['labels'], list) else t['labels']}\n"
    if t.get("tags"): output += f"**Tags**: {t['tags']}\n"
    wait = (t.get('external_wait') or {}).get('current') or {}
    if wait.get('branch') == 'human_escalation_v1':
        output += "\n## Human-held wait\n" + json.dumps(wait, sort_keys=True) + "\n"
        output += (
            "For an already fulfilled ask, use pulse_settle_human_wait with "
            "this receipt/generation and the configured resolved_elsewhere "
            "transition from resume_status. Read immutable parent references "
            "with pulse_list_comments(include_hashes=True).\n"
        )
    next_action = _schema_repair_next_action_line(t)
    if next_action:
        output += f"\n**{next_action}**\n"
    containment = t.get('containment_decision')
    if isinstance(containment, dict):
        output += (
            f"**Containment**: {containment.get('state', 'unknown')} "
            f"(v{containment.get('version', '?')}, owner="
            f"{containment.get('owner_email') or containment.get('owner_group') or 'unresolved'}, "
            f"risk={','.join(containment.get('risk_markers') or [])})\n"
        )
        if containment.get('reason'):
            output += f"**Containment reason**: {containment['reason']}\n"
    if t.get("component_name"): output += f"**Component**: {t['component_name']}\n"
    if t.get("fix_version_name"): output += f"**Fix Version**: {t['fix_version_name']}\n"
    pr_line = _fmt_pr_links(t, include_url=True)
    if pr_line:
        output += f"**Linked PRs**: {pr_line}\n"
    origin_line = _fmt_origin_observations(t, include_url=True)
    if origin_line:
        output += f"**Observed on Origin**: {origin_line}\n"
    # PLS-518: pinned current-state handoff summary — shown ABOVE the description
    # and comments so a failover/handoff seat reads "where are we now" without
    # paging a long thread. It is a handoff aid, not authoritative task state
    # (the workflow/status is). Nudge when substantive comments are newer than
    # the pin (the assignee should refresh it).
    _cs = (t.get('current_state') or '').strip()
    if _cs:
        output += f"\n## Current state\n{_cs}\n"
        _cs_ts = t.get('current_state_updated_at')
        if _cs_ts:
            output += f"\n_Updated: {_cs_ts}_\n"
        _stale = _current_state_staleness(t, _cs_ts)
        if _stale:
            output += f"\n⚠️ {_stale}\n"
    output += f"\n## Description\n{t.get('description', 'No description.')}\n"
    output += f"\n**Created**: {t.get('created_at')} | **Updated**: {t.get('updated_at')}\n"
    return output


@mcp.tool(name="pulse_get_team_throughput")
def get_team_throughput(days: int = 14, project: str = "") -> str:
    """Per-team × status stage-dwell + throughput over a window (PLS-13).

    For each (team, workflow status) reports: sample count, average and p90 dwell
    (how long items sit IN that status, from the transition log — true dwell, not
    an updated_at proxy), and throughput (transitions OUT per day) over the last
    `days` days. Use it to spot bottleneck stages — a high avg/p90 dwell means
    work is piling up in that status for that team.

    Args:
        days: window size in days (default 14).
        project: optional project key (e.g. 'PLS') to scope to one project.
    """
    params: dict = {"days": days}
    if project and str(project).strip():
        params["project"] = str(project).strip()
    result = api_request("GET", "/items/team_throughput/", params=params)
    if "error" in result:
        return _fmt_error(result)
    rows = result.get("rows", [])
    window = result.get("window_days", days)
    if not rows:
        return f"No stage-dwell data in the last {window} days."

    def _h(secs):
        return "-" if secs is None else f"{round(secs / 3600, 1)}h"

    header = f"# Team throughput — last {window} days"
    if result.get("project"):
        header += f" (project {result['project']})"
    lines = [header]
    for r in rows:
        lines.append(
            f"- **{r['team'] or '(unassigned)'}** / {r['status']}: "
            f"avg {_h(r['avg_dwell_seconds'])}, p90 {_h(r['p90_dwell_seconds'])}, "
            f"{r['throughput_out_per_day']}/day out (n={r['samples']})"
        )
    return "\n".join(lines)


@mcp.tool(name="pulse_create_task")
def create_task(
    title: str,
    description: str = "",
    priority: str = "normal",
    project_id: Optional[int] = None,
    assignment_group: Optional[str] = None,
    assignee_email: Optional[str] = None,
    direct_assignment_reason: Optional[str] = None,
    # PLS-900 / ADR-0052 conflict authority: emails/identifiers that must never
    # be selected as this task's assignee. Accepts a list or comma-separated
    # string of email addresses. The resolver hard-excludes them (Option C), so
    # a conflicted party is never silently assigned as the owner.
    conflicted_parties: Optional[Union[str, List[str]]] = None,
    due_date: Optional[str] = None,
    story_points: Optional[int] = None,
    # PLS-3: accept a real JSON array OR a string (JSON / comma-separated) —
    # plain `str` made pydantic reject array args before coercion ran (the
    # update_task fix landed the same change; create_task was missed).
    labels: Optional[Union[str, List[str]]] = None,
    tags: Optional[Union[str, List[str]]] = None,
    sprint_id: Optional[str] = None,
    component_id: Optional[str] = None,
    parent_item_id: Optional[str] = None,
    workflow: Optional[str] = None,
    item_type: Optional[str] = None,
    is_recurring: bool = False,
    frequency: Optional[str] = None,
    recurrence_pattern: Optional[str] = None,
    origin_run_url: Optional[str] = None,
    origin_repo: Optional[str] = None,
    origin_commit_sha: Optional[str] = None,
    origin_ref: Optional[str] = None,
    origin_workflow: Optional[str] = None,
    origin_check: Optional[str] = None,
    origin_image: Optional[str] = None,
    allow_duplicate: bool = False,
) -> str:
    """
    Create a new task.

    ⚠️ SEARCH FIRST — DO NOT CREATE DUPLICATES. Before calling this, run
    `pulse_search_tasks` (and `pulse_list_tasks`) for the same/related ask — a
    matching task may already exist, ACTIVE or DEFERRED. Re-filing work that's
    already tracked (or was intentionally deferred) creates an endless churn of
    duplicates. As a backstop this tool ALSO auto-searches the project for
    active/deferred tasks with a similar title (exact, or ≥60% word overlap) and
    REFUSES to create a near-duplicate — it returns the existing task so you read
    it and its comments first. Only pass `allow_duplicate=True` after you've read
    the existing task and confirmed this is genuinely separate work. If a related
    task is DEFERRED, that was a deliberate operator decision — do not reopen or
    re-file it; comment on the deferred task instead if needed.

    Args:
        title: Task title (required)
        description: Description (markdown supported)
        priority: low, normal, high, urgent
        project_id: Project ID (optional)
        assignment_group: Team/pod to route to, e.g. 'engineering', 'architecture',
            'review', 'qa', 'security', 'documentation', 'research-analytics',
            'admin-ops', or 'social-media'. Prefer this for all normal task
            creation. Pulse resolves the current available concrete owner.
        assignee_email: Explicit direct-person override only. Do NOT use this for
            normal task creation; use assignment_group. If you must directly
            assign a person, also pass direct_assignment_reason explaining why a
            team assignment is impossible.
        direct_assignment_reason: Required whenever assignee_email is used. This
            leaves an audit breadcrumb for intentional direct routing.
        due_date: ISO format date (e.g., '2024-03-15T17:00:00Z')
        story_points: Story point estimate
        labels: Labels — accepts a list (["frontend","bug"]), a comma-separated string ("frontend,bug"), or a single string ("bug"). All are normalized to a list.
        tags: Tags — accepts a list or a comma-separated string.
        sprint_id: Sprint ID to add to
        component_id: Component ID
        parent_item_id: Parent task ID/PK or human task key (e.g. PLAT-123) (creates sub-task)
        workflow: Workflow slug to use. **ALWAYS pass this explicitly** — never rely on the default. Choices for autonomous work:
                  - 'simple' — recurring wiki-only/read-only research or docs maintenance where the deliverable is a wiki/comment update and code/deploy/runtime/config changes are explicitly out of scope
                  - 'reviewed-task' — research briefs or operational tasks that should be independently reviewed before being treated as reliable
                  - 'feature-request-rfc' — cross-system/product discovery or RFC parent tasks that may later spawn reviewed implementation work
                  - 'autonomous-dev' — features, infra, general dev work (routes reviews by label, merges without human gate)
                  - 'autonomous-bug' — bugs you discover during your work (agent triage + QA verification, no human gate)
                  - 'training-run' — ML training runs (planned → training → evaluating → documenting → done)
                  Research tasks should use assignment_group='research-analytics'. The legacy 'research' and 'analytics' labels canonicalize to that one team. If a research task forbids implementation or follow-up task creation, document findings in wiki/comments instead of creating implementation tasks.
                  Legacy workflows like 'software-development' and 'bug-workflow' have a Merge step that gates on Hritik — avoid them. Call pulse_list_workflows for the full list.
        item_type: Item type (e.g., 'task.bug', 'task.story', 'task.generic'). Affects which workflow is applied when a project has a workflow scheme.
        is_recurring: Set True to make this a recurring task. The task will automatically reset to its initial status and advance due_date at each interval boundary, fired by the platform scheduler. No external automation rule needed.
        frequency: One of 'daily', 'weekly', 'monthly', 'yearly', 'custom'. Required when is_recurring=True. Use 'custom' with recurrence_pattern for sub-daily / arbitrary intervals.
        recurrence_pattern: JSON string with custom recurrence config when frequency='custom'. Example: '{"interval_hours": 4}' for every 4 hours, '{"interval_minutes": 30}' for every 30 min, '{"days_of_week": ["mon", "wed", "fri"]}' for specific weekdays. Optional for non-custom frequencies.
        origin_run_url: Exact Shizuha Origin Actions run/job URL where the symptom
            was observed. If omitted, the first Origin run URL in description is
            detected automatically. Pulse enriches it to repo/ref/full SHA/workflow.
        origin_repo: Optional owner/repo fallback when no run URL is available.
        origin_commit_sha: Exact affected commit (7-64 hex characters).
        origin_ref: Affected branch/tag/ref.
        origin_workflow: Origin workflow filename/name.
        origin_check: Exact failing check or job name.
        origin_image: Exact affected image/tag when the observation is runtime-facing.
        allow_duplicate: Set True only after reading the existing task/comments and confirming this is genuinely separate work.

    Examples:
        Weekly recurring documentation task:
            pulse_create_task(title="Weekly status report", assignment_group="documentation",
                              is_recurring=True, frequency="weekly", due_date="2026-04-25T17:00:00Z")

        Weekly recurring research/wiki maintenance task:
            pulse_create_task(title="Weekly Cortex model-serving research scan",
                              assignment_group="research-analytics", workflow="simple",
                              labels=["research", "wiki"],
                              is_recurring=True, frequency="weekly",
                              due_date="2026-07-06T10:00:00Z")

        Every-4-hours recurring task:
            pulse_create_task(title="Reply to trending tweet", assignment_group="social-media",
                              is_recurring=True, frequency="custom",
                              recurrence_pattern='{"interval_hours": 4}')
    """
    data = {"mode": "task", "title": title, "description": description, "priority": priority}
    inferred_run_url = origin_run_url or find_origin_run_url(description)
    if any((inferred_run_url, origin_repo, origin_commit_sha, origin_image)):
        observation = parse_origin_run_url(inferred_run_url) if inferred_run_url else {}
        observation.update({
            key: value for key, value in {
                'run_url': inferred_run_url,
                'repo': origin_repo,
                'commit_sha': origin_commit_sha,
                'ref': origin_ref,
                'workflow': origin_workflow,
                'check': origin_check,
                'image': origin_image,
                'observed_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }.items() if value
        })
        try:
            observation = _enrich_origin_observation(observation)
        except ValueError as exc:
            return f"Error: invalid Origin observation: {exc}"
        data['metadata'] = {'origin_observations': [observation]}
        data['source'] = 'origin-forgejo'
        if observation.get('run_url'):
            data['source_url'] = observation['run_url']
    if _is_suppressed_gx10_4_thermal_task(title, description):
        return (
            "Error: configured-host thermal/NIC/NVMe telemetry is operator-suppressed by PLAT-666. "
            "Do not create, escalate, or re-file inspection/temperature tasks for the configured host "
            "based on node_hwmon/node_thermal/GB10NicTemp readings. Comment on PLAT-666 "
            "only if the operator explicitly reverses that decision; non-thermal failures "
            "for that host remain valid work."
        )
    if assignee_email and not direct_assignment_reason:
        return (
            "Error: direct assignee_email assignment is not the normal workflow. "
            "Create tasks with assignment_group='engineering'/'architecture'/'review'/"
            "'qa'/'security'/'documentation'/'research-analytics'/'admin-ops'/etc. so Pulse "
            "can route to an available team member. If direct assignment is truly "
            "intentional, retry with direct_assignment_reason explaining why a team "
            "assignment is impossible."
        )
    # Require explicit project_id — defaulting was causing agents to file
    # platform / Hikari work into the legacy Automata project and silently
    # pick up the old Software Development Workflow that routes merges to
    # Hritik. If you don't know which project to use: call
    # `pulse_list_projects` first, pick the right one, and pass its id here.
    if not project_id:
        return (
            "Error: `project_id` is required. Call `pulse_list_projects` to "
            "find the right one:\n"
            "  • Hikari LLM work → project_id=330 (key HKR)\n"
            "  • Platform infra / tooling → project_id=331 (key PLAT)\n"
            "  • Other projects → list first\n"
            "Never file tasks into the default Automata project unless the "
            "work is genuinely Automata-scoped."
        )
    data["project"] = project_id
    if assignment_group:
        data["assignment_group"] = assignment_group
    # PLS-465: creation must commit exactly one final assignment.  The backend
    # create path now resolves the canonical id/email pair, validates the direct
    # reason/cap/team contract, records the pin, and publishes the notification
    # after commit.  Sending a group-only create followed by /assign/ exposed the
    # transient team-resolved owner (and notified them) before the explicit owner
    # replaced it.  Put the direct handoff in the initial payload instead.
    if assignee_email:
        data["assignee_email"] = assignee_email
        data["direct_assignment_reason"] = direct_assignment_reason

    # Workflow discipline: the legacy `Software Development Workflow` has
    # a `Merge → Hritik` step that breaks the autonomous contract. Any
    # task created without an explicit workflow choice defaults to
    # `autonomous-dev` here (safer than relying on the project scheme)
    # and we surface a reminder in the response so the agent learns to
    # pass the flag next time.
    default_workflow_used = False
    if not workflow:
        workflow = "autonomous-dev"
        default_workflow_used = True
    if due_date: data["due_date"] = due_date
    if story_points: data["story_points"] = story_points
    if sprint_id: data["sprint"] = sprint_id
    if component_id: data["component"] = component_id
    if parent_item_id: data["parent_item"] = _resolve_item_pk(parent_item_id)
    # PLS-900 / ADR-0052 conflict authority (Option C): normalize a list or a
    # comma-separated string of emails into the task's conflicted_parties list.
    if conflicted_parties:
        if isinstance(conflicted_parties, str):
            conflicted_parties = [
                p.strip() for p in conflicted_parties.split(",") if p.strip()
            ]
        data["conflicted_parties"] = [
            str(p).strip().lower()
            for p in conflicted_parties
            if p is not None and str(p).strip()
        ]
    if tags:
        # Item.tags is a comma-separated TextField. The MCP contract accepts a
        # native JSON array as well as the legacy string form, so normalize the
        # array before crossing the HTTP boundary instead of letting DRF reject
        # it with ``tags: Not a valid string``.
        if isinstance(tags, list):
            data["tags"] = ",".join(
                tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()
            )
        else:
            data["tags"] = tags
    if workflow: data["workflow_name"] = workflow
    if item_type: data["item_type"] = item_type
    if labels:
        try:
            data["labels"] = json.loads(labels) if isinstance(labels, str) else labels
        except json.JSONDecodeError:
            data["labels"] = [l.strip() for l in labels.split(",")]

    # Recurrence config — surfaced as first-class args so agents don't need
    # to ask an admin to set up a Pulse automation rule. Validation mirrors
    # the model/serializer guard so unschedulable configs fail before API POST.
    if is_recurring or frequency is not None or recurrence_pattern is not None:
        effective_is_recurring = is_recurring or (frequency not in (None, "one_time"))
        effective_frequency = frequency or "one_time"
        err = _format_recurrence_validation_error(
            effective_is_recurring, effective_frequency, recurrence_pattern
        )
        if err:
            return err
        data["is_recurring"] = effective_is_recurring
        data["frequency"] = effective_frequency
        if recurrence_pattern is not None:
            data["recurrence_pattern"] = recurrence_pattern

    if not allow_duplicate:
        existing = _find_existing_task_candidates(title, project_id)
        if existing:
            lines = [
                "Error: likely existing task(s) found. Read the existing task and comments before creating a new one.",
                "If this is genuinely distinct follow-up work, retry with allow_duplicate=True and explain the distinction in the description.",
                "",
            ]
            for item in existing[:5]:
                key = item.get("item_key") or f"#{item.get('id')}"
                status = item.get("status", "?")
                assignee = item.get("assignee_email") or "unassigned"
                lines.append(f"- {key}: {item.get('title', 'Untitled')} | status={status} | assignee={assignee}")
                if status == "deferred":
                    lines.append(f"  Deferred means intentionally parked; do not recreate. Use pulse_get_task('{key}') and pulse_list_comments('{key}') to read the reason.")
            return "\n".join(lines)

    # ── PLAT-524: Simple-workflow misfile guard (PLAT-521 HLD §4) ────────────
    # HLD §4.7: fail-open — a classifier exception must NEVER block legitimate
    # task creation; log the error and treat as action=pass.
    try:
        _clf = _classify_simple_workflow_misfile({**data, "workflow": data.get("workflow_name", "")})
    except Exception:
        logger.warning(
            "PLAT-524 classifier raised unexpectedly — failing open (action=pass)",
            exc_info=True,
        )
        _clf = {"action": "pass", "target_workflow": None, "matched_signals": [], "reason": "classifier-error-fail-open"}
    _clf_action = _clf["action"]
    _clf_signals = _clf["matched_signals"]
    _clf_target = _clf.get("target_workflow")
    logger.info(
        "PLAT-524 classifier: action=%s target=%s signals=%s reason=%s title=%r",
        _clf_action, _clf_target, _clf_signals, _clf.get("reason", ""), title[:80],
    )

    # PLAT-1004 status guard: suppress cos_triage / auto_reclassify when an existing
    # near-match task is already past todo/open (in_progress, blocked, in_review, …).
    # Triggered only when allow_duplicate=True bypassed the normal dedup gate — the
    # non-allow_duplicate path already returns early if a near-match exists.
    # Fail-open: a lookup exception logs a warning but never blocks task creation.
    if _clf_action in ("cos_triage", "auto_reclassify") and allow_duplicate:
        _STARTED_STATUSES = frozenset({
            'in_progress', 'blocked', 'in_review', 'awaiting_merge',
            'verification', 'approved',
        })
        try:
            _guard_candidates = _find_existing_task_candidates(title, project_id)
            _already_started = [
                t for t in _guard_candidates
                if (t.get('status') or '').lower() in _STARTED_STATUSES
            ]
            if _already_started:
                logger.info(
                    "PLAT-1004 classifier guard: suppressing %s — %d existing task(s) "
                    "already past todo/open: %s",
                    _clf_action, len(_already_started),
                    [t.get('item_key') for t in _already_started],
                )
                _clf_action = "pass"
                _clf_signals = []
                _clf_target = None
        except Exception:
            logger.warning(
                "PLAT-1004 classifier guard: lookup failed (fail-open) for %r",
                title[:80], exc_info=True,
            )

    _reclassify_notice = ""
    _cos_triage_notice = ""
    if _clf_action == "auto_reclassify" and _clf_target:
        data["workflow_name"] = _clf_target
        default_workflow_used = False  # suppress unrelated "you forgot workflow" warning
        existing_labels = data.get("labels") or []
        if not isinstance(existing_labels, list):
            existing_labels = [str(existing_labels)]
        if "auto-reclassified" not in existing_labels:
            existing_labels.append("auto-reclassified")
        data["labels"] = existing_labels
        _reclassify_notice = (
            f"\n\n🔀 Auto-routed: simple → `{_clf_target}` (signals: {', '.join(_clf_signals)}). "
            f"This workflow has the review/merge/deploy chain code work needs. "
            f"If this was intentional planning work, a lead/CoS can re-file with the `keep-simple` label."
        )
    elif _clf_action == "cos_triage":
        existing_labels = data.get("labels") or []
        if not isinstance(existing_labels, list):
            existing_labels = [str(existing_labels)]
        if "needs-cos-triage" not in existing_labels:
            existing_labels.append("needs-cos-triage")
        data["labels"] = existing_labels
        # Organization isolation: a classifier must never replace an explicit
        # org-local team route with a globally hard-coded person.  That leaked the
        # Global CoS into otherwise valid Digital/Trading/Personal creates and the
        # API correctly rejected them at the project-org assignment fence. Keep
        # the caller's team when supplied; otherwise use the canonical triage team,
        # which the API resolves against the selected project's exact-org roster.
        data.setdefault("assignment_group", _ORG_LOCAL_TRIAGE_TEAM)
        triage_team = data["assignment_group"]
        _cos_triage_notice = (
            f"\n\n⚠️  Organization-local triage: flagged `needs-cos-triage` "
            f"(signals: {', '.join(_clf_signals)}). The selected `{triage_team}` "
            f"team will confirm the correct workflow within this task's organization. "
            f"SLA: high=1h, urgent=30m, critical=never-parked."
        )
    # ─────────────────────────────────────────────────────────────────────────

    result = api_request("POST", "/items/", data=data)
    if "error" in result:
        return _fmt_error(result)

    item_id = result["id"]

    # Post auto-reclassify explanation comment so the creator sees what happened
    if _clf_action == "auto_reclassify" and _clf_target:
        api_request("POST", _item_path(item_id, "comments"), data={
            "content": (
                f"Auto-routed from Simple → `{_clf_target}` by the creation guard "
                f"(matched: {', '.join(_clf_signals)}). "
                f"This workflow carries the review/merge/deploy chain that code work needs. "
                f"If this was intentional planning work, a lead/CoS can re-file with the "
                f"`keep-simple` label (PLAT-521)."
            )
        })

    # PLS-465: ordinary direct and team-only creates are complete after the
    # initial POST.  A second /assign/ call creates a second assignment event and
    # can notify an owner who existed for only a few milliseconds.
    wf = result.get('workflow_name', '')
    wf_status = result.get('workflow_status', '')
    wf_info = f"\nWorkflow: {wf} (status: {wf_status})" if wf else ""
    rec_info = ""
    if result.get('is_recurring'):
        rec_info = f"\nRecurring: {result.get('frequency', '?')}"
        nxt = result.get('next_run_at') or result.get('due_date')
        if nxt:
            rec_info += f" (next fire: {str(nxt)[:19]})"
    default_warning = ""
    if default_workflow_used:
        default_warning = (
            "\n\n⚠️  Note: `workflow` was not specified — defaulted to "
            "`autonomous-dev`. NEXT TIME, pass it explicitly: "
            "`workflow='autonomous-dev'` for features / general tasks, "
            "`workflow='autonomous-bug'` for bugs, `workflow='training-run'` "
            "for ML training runs. Relying on defaults is how tasks end up "
            "on the wrong workflow and gate on Hritik."
        )
    return (
        f"Task created: [{result.get('item_key')}] {result.get('title')}"
        f"{wf_info}{rec_info}"
        f"{_origin_creation_notice(data)}"
        f"{_reclassify_notice}{_cos_triage_notice}{default_warning}"
    )


@mcp.tool(name="pulse_update_task")
def update_task(
    task_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    assignment_group: Optional[str] = None,
    assignee_email: Optional[str] = None,
    direct_assignment_reason: Optional[str] = None,
    # PLS-900 / ADR-0052 conflict authority: replace the set of emails that must
    # never be selected as this task's assignee. Accepts a list or comma-separated
    # string of email addresses.
    conflicted_parties: Optional[Union[str, List[str]]] = None,
    due_date: Optional[str] = None,
    story_points: Optional[int] = None,
    percent_complete: Optional[int] = None,
    is_recurring: Optional[bool] = None,
    frequency: Optional[str] = None,
    recurrence_pattern: Optional[str] = None,
    # PLS-3: accept a real JSON array OR a string (JSON / comma-separated).
    # Declaring plain `str` made pydantic reject array args BEFORE the
    # coercion below could run -> agents retried the same shape in a loop.
    labels: Optional[Union[str, List[str]]] = None,
    tags: Optional[Union[str, List[str]]] = None,
    sprint_id: Optional[str] = None,
    component_id: Optional[str] = None,
    resolution: Optional[str] = None,
    # PLS-518: pinned current-state handoff summary — "where are we now / next
    # executable step / who-what-blocks". Editable ONLY by the current assignee
    # (or staff); the server timestamps it. Update it when you park or hand off.
    current_state: Optional[str] = None,
) -> str:
    """
    Update non-state fields on a task. Only provided fields are changed.

    **Status is NOT accepted here.** Every task in Pulse is governed by a workflow
    — even "simple" tasks use the `simple` workflow — and status changes must go
    through `pulse_execute_transition(item_id, to_status)` so that transition
    rules, knowledge articles, validators, and post-functions fire. Attempting
    to set status via this tool would be rejected by the server anyway; we've
    removed the parameter so there's no confusion.

    Args:
        task_id: Task ID or item_key (e.g., 'AT-57')
        title: New title
        description: New description
        priority: New priority (low, normal, high, urgent)
        assignment_group: Team/pod to route to. Prefer this over direct person assignment.
        assignee_email: Explicit direct-person override (empty string to unassign). If
            non-empty, direct_assignment_reason is required.
        direct_assignment_reason: Required when assignee_email is non-empty; explains
            why a team route was not used.
        due_date: New due date (ISO format)
        story_points: New story points
        percent_complete: Completion percentage (0-100). Auto-set by workflow transitions, can be manually overridden.
        is_recurring: Set/clear recurrence. If True, frequency must be a supported non-one_time value.
        frequency: Recurrence frequency (daily, weekly, monthly, yearly, custom). Use custom for sub-daily intervals.
        recurrence_pattern: Custom recurrence pattern, e.g. "every 8 hours" or JSON interval keys. Cron strings are not supported.
        labels: New labels — accepts a list (["a","b"]), a comma-separated string ("a,b"), or a single string ("a"). All are normalized to a list.
        tags: New tags (comma-separated)
        sprint_id: Move to sprint (UUID)
        component_id: Set component (UUID)
        resolution: Set resolution (e.g., 'fixed', 'wontfix', 'duplicate')
        current_state: Pinned current-state handoff summary (PLS-518). Only the
            current assignee (or staff) may set it; the server timestamps it.
            Pass "" to clear the pin. Update it when you park or hand off.
    """
    data = {}
    if title is not None: data["title"] = title
    if description is not None: data["description"] = description
    if priority is not None: data["priority"] = priority
    # Assignee changes go through the /assign/ endpoint below — NOT in the
    # PATCH payload, which would leave assignee_id unresolved and silently
    # skip the Connect notification. Tracked via the extracted `assignee_email`
    # local.
    if due_date is not None: data["due_date"] = due_date
    if story_points is not None: data["story_points"] = story_points
    if percent_complete is not None: data["percent_complete"] = max(0, min(100, percent_complete))

    if is_recurring is not None or frequency is not None or recurrence_pattern is not None:
        current = api_request("GET", _item_path(task_id))
        if "error" in current:
            return _fmt_error(current)
        effective_is_recurring = (
            is_recurring if is_recurring is not None else bool(current.get("is_recurring"))
        )
        effective_frequency = (
            frequency if frequency is not None else current.get("frequency") or "one_time"
        )
        effective_pattern = (
            recurrence_pattern if recurrence_pattern is not None else current.get("recurrence_pattern")
        )
        if is_recurring is False and frequency is None:
            effective_frequency = "one_time"
            effective_pattern = None
        if effective_frequency != "one_time" and not effective_is_recurring:
            effective_is_recurring = True
        err = _format_recurrence_validation_error(
            effective_is_recurring, effective_frequency, effective_pattern
        )
        if err:
            return err
        data["is_recurring"] = effective_is_recurring
        data["frequency"] = effective_frequency
        data["recurrence_pattern"] = effective_pattern

    if tags is not None: data["tags"] = tags
    if sprint_id is not None: data["sprint"] = sprint_id
    if component_id is not None: data["component"] = component_id
    if resolution is not None: data["resolution"] = resolution
    # PLS-518: pinned current-state handoff summary. "" clears the pin; the
    # server timestamps every write and gates on the current assignee.
    if current_state is not None:
        data["current_state"] = current_state
    # PLS-900 / ADR-0052 conflict authority: allow callers to clear the exclusion
    # set (empty list/string) as well as set it. Normalize a comma-separated
    # string or a list of emails.
    if conflicted_parties is not None:
        if isinstance(conflicted_parties, str):
            conflicted_parts = [
                p.strip() for p in conflicted_parties.split(",") if p.strip()
            ]
        else:
            conflicted_parts = [
                str(p).strip() for p in conflicted_parties if p is not None and str(p).strip()
            ]
        data["conflicted_parties"] = [p.lower() for p in conflicted_parts]
    if labels is not None:
        try:
            data["labels"] = json.loads(labels) if isinstance(labels, str) else labels
        except json.JSONDecodeError:
            data["labels"] = [l.strip() for l in labels.split(",")]

    if assignee_email and not direct_assignment_reason:
        return (
            "Error: direct assignee_email reassignment needs "
            "direct_assignment_reason. Prefer assignment_group for normal routing."
        )

    if not data and assignee_email is None and assignment_group is None:
        return "Error: No fields provided to update. (Note: to change status, use pulse_execute_transition.)"

    # Run the non-assignment PATCH (if any fields aside from assignee changed).
    result = None
    if data:
        result = api_request("PATCH", _item_path(task_id), data=data)
        if "error" in result:
            return _fmt_error(result)

    # Dedicated assignment path — triggers email→user_id resolution + signal.
    assign_warning = ""
    if assignee_email is not None or assignment_group is not None:
        assign_payload = {}
        if assignment_group is not None:
            assign_payload["assignment_group"] = assignment_group
        if assignee_email == "":
            # Explicit empty string = unassign
            assign_payload["assignee_email"] = ""
        elif assignee_email is not None:
            assign_payload["assignee_email"] = assignee_email
            assign_payload["direct_assignment_reason"] = direct_assignment_reason
        assign_res = api_request("POST", _item_path(task_id, "assign"), data=assign_payload)
        if "error" in assign_res:
            assign_warning = (
                f"\n⚠️  Assignment change failed: "
                f"{_assign_error_explanation(assign_res)}"
            )
        elif result is None:
            # Assignment was the only change — surface that task state
            result = assign_res

    if result is None:
        # Fallback (should be unreachable given early-return above)
        return "Error: No changes applied."
    return f"Updated: [{result.get('item_key')}] {result.get('title','')} — Status: {result.get('status','?')}{assign_warning}"


@mcp.tool(name="pulse_delete_task")
def delete_task(task_id: str) -> str:
    """
    Delete a task permanently.

    Args:
        task_id: Task ID (UUID) to delete
    """
    result = api_request("DELETE", _item_path(task_id))
    if "error" in result:
        return _fmt_error(result)
    return "Task deleted successfully."


# =============================================================================
# TASK LIFECYCLE ACTIONS
# =============================================================================

def _pending_workflow_message(result, item_id):
    operation_id = result.get('operation_id')
    item_id = result.get('item_key') or result.get('id') or item_id
    return (
        f"Transition queued for '{result.get('title', item_id)}'; not completed. "
        f"Current status remains '{result.get('status', 'unknown')}'. "
        f"Operation {operation_id} is pending. Read its final receipt with "
        f"pulse_get_workflow_operation(item_id={item_id!r}, operation_id={str(operation_id)!r})."
    )


@mcp.tool(name="pulse_get_workflow_operation")
def get_workflow_operation(item_id: str, operation_id: str) -> str:
    """Read a queued transition/completion receipt using its task and operation ID.

    Returns pending, succeeded, failed or stale from the authorized operation
    endpoint. A succeeded receipt describes this operation's committed result;
    the task's current status is reported separately because it may change later.
    This tool only reads; use the operation ID returned by a queued action.
    """
    if not str(item_id).strip() or not str(operation_id).strip():
        return "Error: provide the task and operation IDs from the queued receipt."
    result = api_request("GET", _item_path(item_id, "workflow-operations"),
                         params={"operation_id": operation_id})
    if isinstance(result, dict) and "error" in result:
        return _fmt_error(result)
    if not isinstance(result, list):
        return "Error: invalid workflow operation response."
    rows = [row for row in result if isinstance(row, dict)
            and str(row.get('operation_id')) == str(operation_id)]
    if len(rows) != 1:
        return "Error: workflow operation not found for this visible task."
    row = rows[0]
    state = row.get('operation_state')
    if (state not in {'pending', 'succeeded', 'failed', 'stale'}
            or row.get('pending') is not (state == 'pending')
            or row.get('committed') is not (state == 'succeeded')):
        return "Error: inconsistent workflow operation receipt."
    label = {
        'pending': 'pending; not completed',
        'succeeded': 'succeeded; graph transaction committed',
        'failed': 'failed; graph transaction did not commit',
        'stale': 'stale; graph transaction did not commit',
    }[state]
    output = (f"Workflow operation {operation_id}: {label}.\n"
              f"Current task [{row.get('item_key', item_id)}] status: {row.get('status', 'unknown')}.\n")
    if row.get('result'):
        output += "Receipt: " + json.dumps(row['result'], sort_keys=True)
    return output


@mcp.tool(name="pulse_complete_task")
def complete_task(task_id: str) -> str:
    """Mark a task as complete. Triggers completion logic and post-functions."""
    result = api_request("POST", _item_path(task_id, "complete"))
    if "error" in result:
        return _fmt_error(result)
    if result.get("pending") is True:
        return _pending_workflow_message(result, task_id)
    return f"Completed: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_reopen_task")
def reopen_task(task_id: str) -> str:
    """Reopen a completed task, setting it back to pending status."""
    result = api_request("POST", _item_path(task_id, "reopen"))
    if "error" in result:
        return _fmt_error(result)
    return f"Reopened: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_assign_task")
def assign_task(
    task_id: str,
    assignee_email: Optional[str] = None,
    assignment_group: Optional[str] = None,
    direct_assignment_reason: Optional[str] = None,
) -> str:
    """
    Assign a task. Prefer assignment_group/team routing; direct person assignment
    is an explicit override and requires a reason.

    Args:
        task_id: Task ID (UUID)
        assignee_email: Email of the user to assign to directly
        assignment_group: Team/pod to route to, e.g. engineering/review/security
        direct_assignment_reason: Required when assignee_email is used
    """
    if not assignee_email and not assignment_group:
        return "Error: provide assignment_group for team routing, or assignee_email with direct_assignment_reason."
    if assignee_email and not direct_assignment_reason:
        return "Error: direct assignee_email assignment needs direct_assignment_reason. Prefer assignment_group."
    payload = {}
    if assignment_group is not None:
        payload["assignment_group"] = assignment_group
    if assignee_email is not None:
        payload["assignee_email"] = assignee_email
        payload["direct_assignment_reason"] = direct_assignment_reason
    result = api_request("POST", _item_path(task_id, "assign"), data=payload)
    if "error" in result:
        return _fmt_error(result)
    key = result.get('item_key') or result.get('item_id') or task_id
    title = result.get('title') or ''
    title_part = f" {title}" if title else ""
    resp_group = result.get('assignment_group')  # team route the SERVER actually persisted
    resolved = result.get('assignee_email') or (result.get('assignee') or {}).get('email')
    if not resolved:
        # Group-only acceptance (PLAT-4178): the API accepted the task team-routed
        # with no concrete assignee. Every such case is a SUCCESS — the task is queued
        # to the team and pulled when a member frees (rebalance sweep / completion
        # hook): all members at the WIP cap (queued_at_capacity), none currently
        # available (queued_no_available_member), or a zero-member team
        # (zero_member_team_alert). Previously only queued_at_capacity was recognized,
        # so the no-available-member queue this change introduces — and the
        # pre-existing zero-member queue — were mis-reported as tool errors.
        #
        # Success is decided from RESPONSE evidence ONLY — a queue signal or a
        # response-side assignment_group — never from the caller's input argument. A
        # request-side assignment_group is NOT proof the server persisted a team route;
        # trusting it would falsely report a malformed 200 (no assignee, no queue flag,
        # no response team route) as "Queued" and silently leave the task unrouted —
        # the exact loss mode PLAT-4178 eliminates. So fail closed in that case.
        if result.get('queued_at_capacity'):
            reason = "all members at the WIP cap"
        elif result.get('queued_no_available_member'):
            reason = "no member currently available"
        elif result.get('zero_member_team_alert'):
            reason = "team has no members — zero-member alert filed"
        elif resp_group:
            reason = "no concrete assignee currently resolvable"
        else:
            return (
                f"Error: assignment API returned success for [{key}]{title_part} but did not "
                "report a concrete assignee, a team queue signal, or a persisted team route "
                "(assignment_group). Failing closed — the task may be unrouted; verify with "
                "pulse_get_task and retry."
            )
        return (
            f"Queued [{key}]{title_part} group-only in team {resp_group or assignment_group} "
            f"({reason}; pulled when a member frees)."
        )
    if assignment_group and not assignee_email:
        resp_team = result.get('assignment_group') or assignment_group
        # PLS-859: never report a silent team rewrite. When the server
        # canonicalized the requested slug (trading-engineering → engineering)
        # or the response team differs from the request, name both forms so the
        # agent cannot mistake the rewrite for "fell back to my default team".
        requested = (assignment_group or "").strip()
        canonicalized = bool(result.get("assignment_group_canonicalized"))
        req_norm = requested.lower().replace("_", "-").replace(" ", "-")
        resp_norm = str(resp_team or "").strip().lower().replace("_", "-").replace(" ", "-")
        rewrite_note = ""
        if canonicalized or (requested and resp_norm and req_norm != resp_norm):
            rewrite_note = (
                f" (NOTE: requested assignment_group '{requested}' was "
                f"canonicalized to '{resp_team}' — not a silent fallback)"
            )
        return (
            f"Assigned [{key}]{title_part} to {resolved} via team "
            f"{resp_team}{rewrite_note}"
        )
    return f"Assigned [{key}]{title_part} to {resolved}"


@mcp.tool(name="pulse_return_task")
def return_task(
    task_id: str,
    reason: str,
    reason_class: str,
    destination_group: Optional[str] = None,
) -> str:
    """
    Return a task you are assigned but CANNOT execute back to its lane, with a
    structured reason (PLAT-8222 self-service action — no lead intervention needed).

    Use when the seat cannot execute the work: missing capability/vantage,
    blocked access, or wrong lane. The capability-gated rebalance sweep will
    not re-pull capability-mismatched work to your seat. A third return for
    the same reason_class is refused — escalate instead.

    Args:
        task_id: Task ID (UUID) or item key (e.g. AT-37)
        reason: Why this seat cannot execute the task (required, auditable)
        reason_class: One of: capability-mismatch, missing-vantage, access-blocked, wrong-lane
        destination_group: Optional team slug to return to (defaults to the task's current lane)
    """
    payload = {"reason": reason, "reason_class": reason_class}
    if destination_group:
        payload["destination_group"] = destination_group
    result = api_request("POST", _item_path(task_id, "return-to-lane"), data=payload)
    if "error" in result:
        return _fmt_error(result)
    key = result.get('item_key') or task_id
    lane = result.get('lane') or destination_group or 'its lane'
    return (
        f"Returned [{key}] to lane {lane} (cannot-execute: {result.get('reason_class')}). "
        f"The capability-gated sweep will not re-pull it to a seat lacking the demanded capability."
    )


@mcp.tool(name="pulse_approve_action")
def approve_action(task_id: str, comment: str = "") -> str:
    """
    Approve an action item (mode=action).

    Args:
        task_id: Action ID (UUID)
        comment: Optional approval comment
    """
    data = {}
    if comment:
        data["comment"] = comment
    result = api_request("POST", _item_path(task_id, "approve"), data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Approved: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_reject_action")
def reject_action(task_id: str, reason: str = "") -> str:
    """
    Reject an action item.

    Args:
        task_id: Action ID (UUID)
        reason: Rejection reason
    """
    data = {}
    if reason:
        data["reason"] = reason
    result = api_request("POST", _item_path(task_id, "reject"), data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Rejected: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_issue_deploy_verify_waiver")
def issue_deploy_verify_waiver(task_id: str, reason: str = "") -> str:
    """
    Issue a role-gated deploy-verify waiver on a task (PLS-675 / PLAT-1351 v4).

    Only a member of devops/admin-ops/qa or the human operator may issue one.
    Automation identities (system@pulse, workflow@pulse, etc.) are always
    refused. The waiver is minted as an immutable typed mechanical receipt
    (receipt.deploy_verify_waiver) that satisfies the live/deployed terminal
    close gate when a correlated production deployment is not available.

    Args:
        task_id: Item/task ID (UUID or key, e.g. 'TRD-34')
        reason: Required non-empty reason for the waiver
    """
    data = {}
    if reason:
        data["reason"] = reason
    result = api_request(
        "POST",
        _item_path(task_id, "issue-deploy-verify-waiver"),
        data=data,
        as_user=True,
    )
    if "error" in result:
        return _fmt_error(result)
    return (
        f"Deploy-verify waiver issued on [{result.get('item_key') or task_id}]: "
        f"{result.get('origin_event')} ({result.get('origin_event_id')})"
    )


@mcp.tool(name="pulse_acknowledge_alert")
def acknowledge_alert(task_id: str) -> str:
    """Acknowledge an alert (mode=alert), indicating it has been seen."""
    result = api_request("POST", _item_path(task_id, "acknowledge"))
    if "error" in result:
        return _fmt_error(result)
    return f"Acknowledged: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_resolve_alert")
def resolve_alert(task_id: str, comment: str = "") -> str:
    """
    Resolve an alert.

    Args:
        task_id: Alert ID (UUID)
        comment: Resolution comment
    """
    data = {}
    if comment:
        data["comment"] = comment
    result = api_request("POST", _item_path(task_id, "resolve"), data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Resolved: [{result.get('item_key')}] {result.get('title')}"


@mcp.tool(name="pulse_silence_alert")
def silence_alert(task_id: str, duration_minutes: int = 60, reason: str = "") -> str:
    """
    Silence an alert for a duration.

    Args:
        task_id: Alert ID (UUID)
        duration_minutes: How long to silence (default 60)
        reason: Why silencing
    """
    data = {"duration_minutes": duration_minutes}
    if reason:
        data["reason"] = reason
    result = api_request("POST", _item_path(task_id, "silence"), data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Silenced for {duration_minutes} minutes."


@mcp.tool(name="pulse_get_my_alerts")
def get_my_alerts(limit: int = 20) -> str:
    """Get firing/acknowledged Pulse alerts assigned to the current agent.

    Alerts are durable incident episodes (``mode=alert``), never task-queue
    items. Prefer ``pulse_get_my_work`` on a heartbeat — it returns alerts and
    tasks in one snapshot so an empty alert inbox cannot look like no work.

    Args:
        limit: Max active alerts (default 20)
    """
    _user_id, email = _get_my_user_id()
    if not email:
        return "Error: Could not determine email from token."
    result = api_request(
        "GET",
        "/items/",
        params={
            "mode": "alert",
            "assignee_email": email,
            "status": "firing,acknowledged",
            "page_size": limit,
        },
    )
    if "error" in result:
        return _fmt_error(result)
    alerts = result.get("results", [])
    if not alerts:
        return f"No active alerts assigned to {email}."
    return f"Active alerts for {email}:\n\n" + _fmt_items(alerts)


# =============================================================================
# TASK QUERIES
# =============================================================================

@mcp.tool(name="pulse_search_tasks")
def search_tasks(query: str, limit: int = 20) -> str:
    """
    Search tasks by title or description text only.
    Does NOT search by assignee, status, priority, or other fields.
    Use pulse_list_tasks with structured filters (assignee_email, status, etc.) for field-based queries.

    Args:
        query: Search query string
        limit: Max results (default 20)
    """
    results = []
    seen = set()
    for probe in _task_search_queries(query):
        result = api_request("GET", "/items/", params={"mode": "task", "search": probe, "page_size": limit})
        if "error" in result:
            return _fmt_error(result)
        for item in result.get("results", []):
            key = item.get("id") or item.get("item_key")
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                return _fmt_items(results)
    return _fmt_items(results)


def _clamp_my_tasks_limit(limit: int) -> int:
    """Normalize get_my_tasks limit: default-ish floor 1, cap MY_TASKS_MAX_LIMIT."""
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = MY_TASKS_DEFAULT_LIMIT
    return max(1, min(MY_TASKS_MAX_LIMIT, requested))


def _my_tasks_page_header(email: str, shown: int, total: Optional[int], limit: int) -> str:
    """Header that prevents silent false-drain when the page is truncated."""
    if total is not None and total > shown:
        return (
            f"Tasks for {email} (showing top {shown} of {total} active, "
            f"queue-ordered listing; limit={limit}). "
            f"{total - shown} more remain on your queue. "
            f"You choose what to advance; pass limit=N for a wider window."
        )
    if total is not None:
        return f"Tasks for {email} (showing {shown} of {total} active, queue-ordered; limit={limit}):"
    return f"Tasks for {email} (showing up to {limit}, queue-ordered):"


@mcp.tool(name="pulse_get_my_tasks")
def get_my_tasks(
    status: Optional[str] = None,
    limit: int = MY_TASKS_DEFAULT_LIMIT,
) -> str:
    """
    Get active tasks assigned to the current agent.

    Default listing order is ready/movable work first, then highest priority,
    then FIFO by creation time. That is a listing, not a command — you choose
    what to advance. Prefer ``pulse_get_my_work`` on a heartbeat.

    Returns ONLY tasks in active states (pending, in_progress, in_review,
    etc.) — terminal tasks (completed, rejected, cancelled, closed, deferred, etc.)
    are NEVER returned unless you pass an explicit `status=` filter
    naming them (e.g. `status="rejected"` or `status="completed"`).

    By default returns only the top few queue-ordered tasks (limit=5) so a
    heartbeat does not dump an entire large queue into model context. Pass a
    higher `limit` when you intentionally need a wider window (capped at 200).
    When more active work exists than the page, the response states the total
    so a short page is not mistaken for an empty queue. The summary separates
    ready/movable work from blocked/waiting work (PLS-464) so an all-blocked
    queue is not misread as actionable.

    Args:
        status: Filter by status. If omitted, terminal statuses are excluded.
        limit: Max results (default 5). Override with a larger value when needed.
    """
    limit = _clamp_my_tasks_limit(limit)
    user_id, email = _get_my_user_id()
    if not email:
        return "Error: Could not determine email from token."

    # Query by email — tasks are assigned by email, not numeric user_id.
    # PLS-405: for default-active queues, page after MCP-side visibility
    # filters so a hidden future/scheduled row cannot consume limit=1.
    params = {
        "mode": "task",
        "assignee_email": email,
        "queue_order": "true",
        # PLS-698: the MCP never renders the API's hidden-by-permission hint.
        # Opt out so a fleet heartbeat does not repeat the active-queue COUNT
        # solely to compute diagnostic data that this caller discards.
        "permission_hint": "false",
    }
    if status:
        params["status"] = status
        params["page_size"] = limit
    else:
        # Exclude terminal/scheduled rows before pagination; local filtering is
        # only a compatibility backstop for older Pulse servers.
        params["is_active"] = "true"

    total_active: Optional[int] = None
    if status:
        result = api_request("GET", "/items/", params=params)
        if "error" in result:
            return _fmt_error(result)
        tasks = result.get("results", [])[:limit]
        hidden = 0
        hidden_future = 0
        try:
            total_active = int(result.get("count")) if result.get("count") is not None else len(tasks)
        except (TypeError, ValueError):
            total_active = len(tasks)
    else:
        result, tasks, hidden, hidden_future = _fetch_default_active_page(params, limit)
        if "error" in result:
            return _fmt_error(result)
        try:
            total_active = int(result.get("count")) if result.get("count") is not None else None
        except (TypeError, ValueError):
            total_active = None
        if total_active is None:
            # No backend total — at least distinguish "page full" from empty.
            total_active = len(tasks) if not result.get("next") else max(len(tasks), limit + 1)
        ready_count = sum(1 for task in tasks if _is_backlog_pull_ready(task))
        if ready_count <= BACKLOG_PULL_READY_THRESHOLD:
            backlog_params = {
                "mode": "task",
                "assignee_email": email,
                "status": "backlog",
                "page_size": BACKLOG_PULL_LIMIT,
                "queue_order": "true",
                "permission_hint": "false",
            }
            backlog_result = api_request("GET", "/items/", params=backlog_params)
            if "error" not in backlog_result:
                backlog_items = backlog_result.get("results", [])
                backlog_count = backlog_result.get("count", len(backlog_items))
                if backlog_items:
                    active_text = (
                        _fmt_items(tasks, hidden_terminal=hidden, hidden_scheduled=hidden_future)
                        if tasks else ""
                    )
                    backlog_text = _fmt_items(backlog_items)
                    header = _my_tasks_page_header(email, len(tasks), total_active if tasks else 0, limit)
                    if active_text:
                        header += f"\n\n{active_text}"
                    else:
                        header += "\n\nNo ready/movable active tasks assigned."
                    return (
                        f"{header}\n\n"
                        f"**Backlog pull lane**: {backlog_count} parked backlog item(s) are assigned to you. "
                        f"Because your movable ready queue has {ready_count} item(s) (threshold {BACKLOG_PULL_READY_THRESHOLD}), "
                        "pull ONE highest-priority backlog item below into active work if you can advance it now. "
                        "Use the task's available workflow transition (for example To Do/Open/Start Progress); "
                        "do not churn the whole backlog.\n\n"
                        f"{backlog_text}"
                    )
    # PLS-838: hidden_future is a row list on the default-active path (and an
    # int 0 on the explicit status= path). `not hidden_future` is true for both
    # empty shapes; `hidden_future == 0` is False for [] and was a false-drain.
    if not tasks and hidden == 0 and not hidden_future:
        return f"No tasks assigned to {email}."
    header = _my_tasks_page_header(email, len(tasks), total_active, limit)
    return f"{header}\n\n" + _fmt_items(tasks, hidden_terminal=hidden, hidden_scheduled=hidden_future)


@mcp.tool(name="pulse_get_my_work")
def get_my_work(limit: int = MY_TASKS_DEFAULT_LIMIT) -> str:
    """One-call heartbeat inbox: assigned alerts and assigned tasks together.

    Use this on a ``[Heartbeat]`` instead of calling ``pulse_get_my_alerts``
    and then ``pulse_get_my_tasks``. Empty alerts do not mean an empty queue.
    This tool does not pick an item — you choose what to advance.

    Args:
        limit: Max tasks in the task half (default 5, same as get_my_tasks).
    """
    alerts = get_my_alerts()
    tasks = get_my_tasks(limit=limit)
    return (
        "Your Pulse work (alerts + tasks, one snapshot). "
        "You choose what to advance. Empty alerts does not mean an empty queue.\n\n"
        f"## Alerts\n{alerts}\n\n## Tasks\n{tasks}"
    )


@mcp.tool(name="pulse_get_user_tasks")
def get_user_tasks(
    status: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Get active tasks assigned to the user you are assisting (not your own).
    When user asks "what are MY tasks?", use this. For YOUR tasks, use pulse_get_my_tasks.

    Default ordering is ready work first, then highest priority first, then FIFO
    by creation time inside that priority bucket.

    Returns ONLY tasks in active states — terminal tasks (completed,
    rejected, cancelled, closed, deferred, etc.) are NEVER returned unless you
    pass an explicit `status=` filter naming them.

    Args:
        status: Filter by status. If omitted, terminal statuses are excluded.
        limit: Max results (default 20)
    """
    import jwt
    token = _get_effective_user_token()
    if not token:
        return "No user context available."
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        user_id = payload.get("user_id")
        user_email = payload.get("email")
    except Exception as e:
        return f"Error reading user token: {e}"

    params = {
        "mode": "task",
        "assignee_id": user_id,
        "queue_order": "true",
        "permission_hint": "false",
    }
    if status:
        params["status"] = status
        params["page_size"] = limit
    else:
        params["is_active"] = "true"

    if status:
        result = api_request("GET", "/items/", params=params, as_user=True)
        if "error" in result:
            return _fmt_error(result)
        tasks = result.get("results", [])
        hidden = 0
        hidden_future = 0
    else:
        result, tasks, hidden, hidden_future = _fetch_default_active_page(params, limit, as_user=True)
        if "error" in result:
            return _fmt_error(result)
    # PLS-838: see get_my_tasks — compare the row list with `not`, not `== 0`.
    if not tasks and hidden == 0 and not hidden_future:
        return f"No tasks assigned to {user_email}."
    return f"Tasks for {user_email}:\n\n" + _fmt_items(tasks, hidden_terminal=hidden, hidden_scheduled=hidden_future)


@mcp.tool(name="pulse_get_overdue_tasks")
def get_overdue_tasks(project_id: Optional[int] = None, limit: int = 20) -> str:
    """
    Get tasks that are past their due date.

    Args:
        project_id: Filter by project
        limit: Max results
    """
    params = {"page_size": limit}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/items/overdue/", params=params)
    if "error" in result:
        return _fmt_error(result)
    items = (result if isinstance(result, list) else result.get("results", []))
    return _fmt_items(items, "overdue task")




@mcp.tool(name="pulse_get_agent_output_attribution")
def get_agent_output_attribution(
    organization_id: int,
    window_hours: int = 24,
    github_reviewers: str = "kai2574,sara2574",
    github_org: str = "shizuha-labs",
) -> str:
    """
    EVOL-44 audit aggregate for the daily performance inspection.

    Returns per-agent Pulse output counts (comments + status transitions) for an
    explicit organization, with no permission-hidden per-agent gaps because the
    backend returns aggregate counts only. Also includes best-effort GitHub PR
    review evidence for reviewer identities when GITHUB_TOKEN/GH_TOKEN is
    available.

    Args:
        organization_id: Pulse/Admin organization id to audit.
        window_hours: Lookback window, default 24h (server caps to 14 days).
        github_reviewers: Comma-separated GitHub usernames to check for PR reviews.
        github_org: GitHub org to search, default shizuha-labs.
    """
    result = api_request(
        "GET",
        "/audit/agent-output-attribution/",
        params={"organization_id": organization_id, "window_hours": window_hours},
    )
    if "error" in result:
        return _fmt_error(result)
    since_raw = result.get("since") or _dt.datetime.now(_dt.timezone.utc).isoformat()
    since = _dt.datetime.fromisoformat(str(since_raw).replace("Z", "+00:00"))
    reviewers = [r.strip() for r in str(github_reviewers or "").split(",") if r.strip()]
    result["github_review_evidence"] = _github_review_evidence(reviewers, since, github_org=github_org)
    return json.dumps(result, indent=2)


@mcp.tool(name="pulse_get_statistics")
def get_statistics() -> str:
    """Get task statistics: counts by status, priority, and project."""
    result = api_request("GET", "/items/statistics/")
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, indent=2)


@mcp.tool(name="pulse_get_activity")
def get_activity(task_id: str, limit: int = 20) -> str:
    """
    Get the activity/change history for a task.

    Args:
        task_id: Task ID (UUID)
        limit: Max entries
    """
    result = api_request("GET", _item_path(task_id, "activity"), params={"limit": limit})
    if "error" in result:
        return _fmt_error(result)
    activities = (result if isinstance(result, list) else result.get("activities", result.get("results", [])))
    if not activities:
        return "No activity found."
    output = f"Activity ({len(activities)} entries):\n\n"
    for a in activities:
        output += f"- {a.get('action', 'unknown')} by {a.get('user_email', 'system')} at {a.get('created_at', '')}\n"
        if a.get("details"):
            output += f"  {a['details']}\n"
    return output


# =============================================================================
# COMMENTS
# =============================================================================

def _item_path_id(item) -> str:
    """Normalize/escape an item identifier for use inside /items/<id>/ paths.

    A raw display key such as ``#6559`` cannot be interpolated into an HTTP
    path because ``#`` starts a URL fragment and never reaches the backend.
    Numeric display keys are equivalent to their numeric pk; other shapes are
    percent-escaped so backend path resolution receives the full identifier.
    """
    if isinstance(item, int):
        return str(item)
    s = str(item).strip()
    if s.startswith("#") and s[1:].isdigit():
        s = s[1:]
    return quote(s, safe="")


def _item_path(item, suffix: str = "") -> str:
    suffix = suffix.strip("/")
    tail = f"{suffix}/" if suffix else ""
    return f"/items/{_item_path_id(item)}/{tail}"


def _resolve_item_pk(item):
    """Resolve a task key ('PLAT-23'), UUID, or numeric id to the numeric item
    primary key that field-based write endpoints (e.g. /comments/ `item`) require.

    PLAT-34: read tools + path-based write tools accept the task key directly
    (the backend resolves `/items/<key>/`), but endpoints that take `item` as a
    *field* (not a path segment) reject the string and demand the numeric pk.
    This makes "if the key works in pulse_get_task, it works everywhere" true.

    Numeric input passes through. On lookup failure, returns the input so the
    caller still surfaces the backend's error rather than masking it.
    """
    if isinstance(item, int):
        return item
    s = str(item).strip()
    lookup = s[1:] if s.startswith("#") and s[1:].isdigit() else s
    res = api_request("GET", _item_path(lookup))
    if isinstance(res, dict) and "error" not in res and res.get("id") is not None:
        return res["id"]
    if lookup.isdigit():
        return int(lookup)
    return item


@mcp.tool(name="pulse_add_comment")
def add_comment(task_id: str, content: str, client_message_id: Optional[str] = None) -> str:
    """
    Add a comment to a task.

    Args:
        task_id: Task ID, item_key (e.g. 'PLAT-23'), UUID, or numeric PK
        content: Comment content (markdown supported)
        client_message_id: Optional idempotency key (PLAT-10171 Patch #2a). A retry
            carrying the same id on the same task returns the original result WITHOUT
            re-posting — kills the 7x-resend class at the send boundary the way
            message_user's client_message_id kills it on the DM lane.
    """
    item_pk = _resolve_item_pk(task_id)

    # PLAT-10171 Patch #2a — send idempotency + anti-burst guard.
    #
    # Two layers, both process-local (deliberately: the proven failure shapes
    # are in-process retry loops — revi's 7x-resend specimen, SCLI-821
    # identical-turn storms at 4-11s cadence; a process restart also ends the
    # retry loop, so cross-restart persistence adds nothing for these faces).
    #
    # Layer 1 (explicit id): the caller supplies client_message_id; the same
    # (item, id) pair returns the original result verbatim — never re-posts.
    # Layer 2 (anti-burst): an IDENTICAL (item, content) POST within
    # ANTI_BURST_WINDOW_S of the last one is treated as a retry replay even
    # without an id — the tight-cadence identical-turn storm class has no
    # client ids, and identical content to the same item inside the burst
    # window is a resend by definition (legitimate follow-ups rephrase).
    global _COMMENT_SEND_LEDGER
    now = _time.time()
    content_hash = _hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    replay_note = ""
    if client_message_id:
        key = f"id:{item_pk}:{client_message_id}"
        prior = _COMMENT_SEND_LEDGER.get(key)
        if prior is not None:
            return (
                f"Comment already added (idempotent replay — no duplicate). Original: {prior} "
                f"[PLAT-10171 Patch #2a: same client_message_id on task {task_id}]"
            )
    else:
        burst_key = f"burst:{item_pk}:{content_hash}"
        prior_ts = _COMMENT_SEND_LEDGER.get(burst_key)
        if prior_ts is not None and (now - prior_ts) < _COMMENT_ANTI_BURST_WINDOW_S:
            return (
                f"BLOCKED by send anti-burst guard (PLAT-10171 Patch #2a): an IDENTICAL comment was posted to this "
                f"task {now - prior_ts:.0f}s ago. This is the retry-storm signature — do NOT re-send the same content; "
                "if the post failed, list the task's comments to verify what landed before composing a different reply."
            )

    result = api_request("POST", "/comments/", data={"item": item_pk, "content": content})
    if "error" in result:
        return _fmt_error(result)
    summary = f"Comment added (ID: {result.get('id')})"
    if client_message_id:
        _COMMENT_SEND_LEDGER[f"id:{item_pk}:{client_message_id}"] = summary
        replay_note = " [idempotency key recorded]"
    _COMMENT_SEND_LEDGER[f"burst:{item_pk}:{content_hash}"] = now
    _COMMENT_SEND_LEDGER_PRUNE(now)
    return summary + replay_note


# ── PLAT-10171 Patch #2a support ──
_COMMENT_ANTI_BURST_WINDOW_S = 30.0
_COMMENT_SEND_LEDGER: dict = {}
_COMMENT_SEND_LEDGER_MAX_ENTRIES = 5000


def _COMMENT_SEND_LEDGER_PRUNE(now: float) -> None:
    """Bound the ledger without ever wholesale-clearing it.

    Eviction order: (1) burst entries older than 4x the anti-burst window
    (their replay-blocking purpose has expired), (2) oldest burst entries
    when still over the size cap, (3) oldest explicit-id entries
    (insertion-order FIFO). NEVER ``clear()``: the Layer-1 contract is that
    an id-keyed replay returns the original result verbatim for the ledger's
    lifetime — a wholesale clear would let a late retry RE-POST, defeating
    the never-re-posts guarantee (aoi, PLAT-10471 review 5679 blocker-1).
    """
    stale = [k for k, v in _COMMENT_SEND_LEDGER.items() if k.startswith("burst:") and (now - v) > _COMMENT_ANTI_BURST_WINDOW_S * 4]
    for k in stale:
        _COMMENT_SEND_LEDGER.pop(k, None)
    if len(_COMMENT_SEND_LEDGER) <= _COMMENT_SEND_LEDGER_MAX_ENTRIES:
        return
    # Over cap: evict the expendable burst class FIRST — all of it — before
    # any explicit-id entry is touched (hiro/aoi: id keys are pinned).
    for k in [k for k in _COMMENT_SEND_LEDGER if k.startswith("burst:")]:
        _COMMENT_SEND_LEDGER.pop(k, None)
    excess = len(_COMMENT_SEND_LEDGER) - _COMMENT_SEND_LEDGER_MAX_ENTRIES
    for k in list(_COMMENT_SEND_LEDGER)[:excess]:
        _COMMENT_SEND_LEDGER.pop(k, None)


@mcp.tool(name="pulse_list_comments")
def list_comments(task_id: str, limit: int = 20, offset: int = 0, include_system: bool = False,
                  include_hashes: bool = False) -> str:
    """
    List comments on a task, NEWEST first — the actionable end (reviewer
    fix-lists, gate rulings, the feedback you must act on land newest).

    PLAT-302: the API paginates oldest-first, so naively showing page 1 hid the
    newest comments on any thread with >10 of them. This collects the full thread
    and returns the newest `limit`, with an explicit marker naming any older
    comments not shown — never a silent truncation.

    PLS-518: system/lifecycle comments (failover, rebalance, moved-to-backlog,
    auto-block/unblock, routability holds, validator/resolver audits) are
    SEGREGATED from substantive human/agent content. By default they are hidden
    (they outnumber substantive comments on standing items) and reported as a
    count; pass include_system=True to show them, each tagged [system].

    Args:
        task_id: Task ID, item_key (e.g. 'PLAT-23'), UUID, or numeric PK
        limit: max comments to return, newest first (default 20)
        offset: skip the N newest before the window (page into older comments)
        include_system: also show system/lifecycle comments (tagged [system]); default False
        include_hashes: include exact stored comment references for evidenced settlement; default False
    """
    item_pk = _resolve_item_pk(task_id)
    PAGE = 200  # StandardPagination max_page_size

    # The /comments/ endpoint is OLDEST-first (Comment.Meta orders by created_at
    # ASC and CommentViewSet enables no `?ordering`), so the NEWEST comments live
    # on the LAST page. We fetch page 1 only to learn the total `count`, then walk
    # from the last page BACKWARD until the requested newest window is filled.
    # This is correct for any thread size (no oldest-prefix truncation), needs no
    # ignored ordering param, and is cheap (1–2 requests for normal use). If a
    # page fails mid-walk we surface an explicit INCOMPLETE marker rather than
    # mislabel an oldest prefix as "newest".
    first = api_request("GET", "/comments/", params={"item": item_pk, "page_size": PAGE, "page": 1})
    if "error" in first:
        return _fmt_error(first)
    total = first.get("count")
    if total is None:
        total = len(first.get("results", []))
    if not total:
        return "No comments."

    need = total if limit <= 0 else min(total, offset + limit)
    last_page = (total + PAGE - 1) // PAGE

    collected: list = []
    incomplete = False
    page = last_page
    while page >= 1 and len(collected) < need:
        if page == 1:
            batch = first.get("results", [])
        else:
            r = api_request("GET", "/comments/", params={"item": item_pk, "page_size": PAGE, "page": page})
            if "error" in r:
                incomplete = True  # a NEWER page failed — flag it, never mislabel
                break
            batch = r.get("results", [])
        collected = batch + collected  # older pages prepend before newer ones
        page -= 1

    # `collected` spans the newest pages (oldest-first within); sort newest-first.
    collected.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    window = collected[offset:] if limit <= 0 else collected[offset:offset + limit]

    # PLS-518: segregate system/lifecycle comments from substantive content.
    system_count = sum(1 for c in window if c.get("is_system"))
    substantive = [c for c in window if not c.get("is_system")]
    shown = window if include_system else substantive

    header = f"{total} comment(s)"
    if offset or len(shown) < total:
        header += f" — showing newest {len(shown)}" + (f" (skipping {offset} newest)" if offset else "")
    if incomplete:
        header += " [INCOMPLETE — a page failed to load; some NEWER comments may be missing]"
    if system_count and not include_system:
        header += f" — {system_count} system/lifecycle comment(s) hidden (pass include_system=True to show)"
    elif system_count:
        header += f" — includes {system_count} system/lifecycle comment(s)"
    header += ", NEWEST first:\n\n"
    output = header
    for c in shown:
        tag = " **[system]**" if c.get("is_system") else ""
        output += f"**{c.get('author_email', 'Unknown')}**{tag} ({c.get('created_at', '')}):\n{c.get('content', '')}\n\n"
        if include_hashes:
            import hashlib
            reference = {
                'parent_id': c.get('item', item_pk),
                'comment_id': c['id'],
                'sha256': hashlib.sha256(c.get('content', '').encode('utf-8')).hexdigest(),
            }
            output += "Settlement reference: " + json.dumps(reference, sort_keys=True) + "\n\n"

    newer = offset
    older = total - (offset + len(window))
    if newer > 0:
        output += f"_↑ {newer} newer comment(s) above this window (decrease offset to see them)._\n"
    if older > 0:
        output += f"_↓ (+{older} older comment(s) not shown — call again with offset={offset + len(window)} to page back)._\n"
    return output


# =============================================================================
# PROJECTS
# =============================================================================

@mcp.tool(name="pulse_list_projects")
def list_projects(organization_id: Optional[int] = None) -> str:
    """
    List projects the agent has access to.

    Args:
        organization_id: Filter by organization
    """
    # PLS-48: /projects/ is paginated (StandardPagination, default page_size 10).
    # The old call requested no page_size and read only the FIRST page's `results`,
    # silently truncating to 10 projects and omitting the rest (e.g. PLAT/VEN). Request
    # the max page size AND follow `next` so the COMPLETE set the caller can access is
    # returned, not just page 1. (Whether a given project is in that accessible set is
    # the backend's membership/open-access policy — orthogonal to this truncation bug.)
    base = {"page_size": 200}
    if organization_id: base["organization"] = organization_id
    projects = []
    page = 1
    while True:
        result = api_request("GET", "/projects/", params={**base, "page": page})
        if "error" in result:
            return _fmt_error(result)
        if isinstance(result, list):
            projects.extend(result)
            break
        projects.extend(result.get("results", []))
        if not result.get("next"):
            break
        page += 1
        if page > 50:  # safety cap (50 * 200 = 10k projects)
            break
    if not projects:
        return "No projects found."
    output = f"{len(projects)} project(s):\n\n"
    for p in projects:
        output += f"- [{p.get('key')}] {p.get('name')} (ID: {p.get('id')})\n"
        if p.get("description"):
            output += f"  {p['description'][:100]}\n"
        output += f"  Type: {p.get('project_type')} | Access: {p.get('access_level')}\n\n"
    return output


@mcp.tool(name="pulse_list_user_projects")
def list_user_projects(limit: int = 20) -> str:
    """List projects the user you are assisting has access to."""
    if not _get_effective_user_token():
        return "No user context available."
    result = api_request("GET", "/projects/", params={"page_size": limit}, as_user=True)
    if "error" in result:
        return _fmt_error(result)
    projects = result.get("results", [])
    if not projects:
        return "No projects found for user."
    output = f"User's projects ({len(projects)}):\n\n"
    for p in projects:
        output += f"- [{p.get('key')}] {p.get('name')} (ID: {p.get('id')})\n"
    return output


@mcp.tool(name="pulse_get_project")
def get_project(project_id: str) -> str:
    """
    Get project details including member count and item count.

    Args:
        project_id: Project ID
    """
    result = api_request("GET", f"/projects/{project_id}/")
    if "error" in result:
        return _fmt_error(result)
    p = result
    output = f"# Project: {p.get('name')} [{p.get('key')}]\n\n"
    output += f"**Type**: {p.get('project_type')} | **Access**: {p.get('access_level')}\n"
    if p.get("description"): output += f"**Description**: {p['description']}\n"
    if p.get("lead_email"): output += f"**Lead**: {p['lead_email']}\n"
    output += f"**ID**: {p.get('id')}\n"
    return output


@mcp.tool(name="pulse_list_project_members")
def list_project_members(project_id: str) -> str:
    """
    List members of a project with their roles.

    Args:
        project_id: Project ID
    """
    result = api_request("GET", f"/projects/{project_id}/members/")
    if "error" in result:
        return _fmt_error(result)
    members = result.get("members", result.get("results", []))
    if not members:
        return "No members found."
    output = f"{len(members)} member(s):\n\n"
    for m in members:
        output += f"- {m.get('user_email', 'unknown')} — Role: {m.get('role', 'member')}\n"
    return output


# =============================================================================
# SPRINTS
# =============================================================================

@mcp.tool(name="pulse_list_sprints")
def list_sprints(project_id: Optional[int] = None, limit: int = 20) -> str:
    """
    List sprints, optionally filtered by project.

    Args:
        project_id: Filter by project ID
        limit: Max results
    """
    params = {"page_size": limit}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/sprints/", params=params)
    if "error" in result:
        return _fmt_error(result)
    sprints = result.get("results", [])
    if not sprints:
        return "No sprints found."
    output = f"{len(sprints)} sprint(s):\n\n"
    for s in sprints:
        output += f"- {s.get('name')} ({s.get('status', 'planning')})\n"
        output += f"  Start: {s.get('start_date', 'N/A')} | End: {s.get('end_date', 'N/A')}\n"
        output += f"  ID: {s.get('id')}\n\n"
    return output


@mcp.tool(name="pulse_create_sprint")
def create_sprint(
    name: str,
    project_id: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    goal: str = "",
) -> str:
    """
    Create a new sprint.

    Args:
        name: Sprint name (e.g., "Sprint 5")
        project_id: Project ID
        start_date: Start date (ISO format)
        end_date: End date (ISO format)
        goal: Sprint goal
    """
    data = {"name": name, "project": project_id}
    if start_date: data["start_date"] = start_date
    if end_date: data["end_date"] = end_date
    if goal: data["goal"] = goal
    result = api_request("POST", "/sprints/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Sprint created: {result.get('name')} (ID: {result.get('id')})"


@mcp.tool(name="pulse_start_sprint")
def start_sprint(sprint_id: str) -> str:
    """Start a sprint, changing its status to active."""
    result = api_request("POST", f"/sprints/{sprint_id}/start/")
    if "error" in result:
        return _fmt_error(result)
    return f"Sprint started: {result.get('name')}"


@mcp.tool(name="pulse_complete_sprint")
def complete_sprint(sprint_id: str, move_to_sprint: Optional[str] = None) -> str:
    """
    Complete a sprint. Incomplete items can be moved to another sprint.

    Args:
        sprint_id: Sprint ID to complete
        move_to_sprint: Sprint ID to move incomplete items to
    """
    data = {}
    if move_to_sprint: data["move_to_sprint"] = move_to_sprint
    result = api_request("POST", f"/sprints/{sprint_id}/complete/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Sprint completed: {result.get('name')}"


@mcp.tool(name="pulse_get_sprint_burndown")
def get_sprint_burndown(sprint_id: str) -> str:
    """
    Get burndown chart data for a sprint.

    Args:
        sprint_id: Sprint ID
    """
    result = api_request("GET", f"/sprints/{sprint_id}/burndown/")
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, indent=2)


@mcp.tool(name="pulse_get_velocity")
def get_velocity(project_id: int) -> str:
    """
    Get velocity data (story points per sprint) for a project.

    Args:
        project_id: Project ID
    """
    result = api_request("GET", "/sprints/velocity/", params={"project": project_id})
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, indent=2)


# =============================================================================
# COMPONENTS & VERSIONS
# =============================================================================

@mcp.tool(name="pulse_list_components")
def list_components(project_id: Optional[int] = None) -> str:
    """
    List project components.

    Args:
        project_id: Filter by project
    """
    params = {}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/components/", params=params)
    if "error" in result:
        return _fmt_error(result)
    comps = (result if isinstance(result, list) else result.get("results", []))
    if not comps:
        return "No components found."
    output = f"{len(comps)} component(s):\n\n"
    for c in comps:
        output += f"- {c.get('name')} (ID: {c.get('id')})\n"
        if c.get("lead_email"): output += f"  Lead: {c['lead_email']}\n"
        if c.get("description"): output += f"  {c['description'][:80]}\n"
    return output


@mcp.tool(name="pulse_create_component")
def create_component(project_id: int, name: str, description: str = "", lead_email: str = "") -> str:
    """
    Create a project component.

    Args:
        project_id: Project ID
        name: Component name (e.g., "Frontend", "API")
        description: Description
        lead_email: Component lead email
    """
    data = {"project": project_id, "name": name}
    if description: data["description"] = description
    if lead_email: data["lead_email"] = lead_email
    result = api_request("POST", "/components/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Component created: {result.get('name')} (ID: {result.get('id')})"


@mcp.tool(name="pulse_list_versions")
def list_versions(project_id: Optional[int] = None) -> str:
    """
    List project versions/releases.

    Args:
        project_id: Filter by project
    """
    params = {}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/versions/", params=params)
    if "error" in result:
        return _fmt_error(result)
    versions = (result if isinstance(result, list) else result.get("results", []))
    if not versions:
        return "No versions found."
    output = f"{len(versions)} version(s):\n\n"
    for v in versions:
        output += f"- {v.get('name')} ({v.get('status', 'planning')}) — ID: {v.get('id')}\n"
        if v.get("release_date"): output += f"  Release: {v['release_date']}\n"
    return output


@mcp.tool(name="pulse_create_version")
def create_version(project_id: int, name: str, description: str = "", release_date: Optional[str] = None) -> str:
    """
    Create a project version/release.

    Args:
        project_id: Project ID
        name: Version name (e.g., "1.0.0")
        description: Description
        release_date: Target release date (ISO format)
    """
    data = {"project": project_id, "name": name}
    if description: data["description"] = description
    if release_date: data["release_date"] = release_date
    result = api_request("POST", "/versions/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Version created: {result.get('name')} (ID: {result.get('id')})"


@mcp.tool(name="pulse_release_version")
def release_version(version_id: str) -> str:
    """
    Mark a version as released.

    Args:
        version_id: Version ID
    """
    result = api_request("POST", f"/versions/{version_id}/release/")
    if "error" in result:
        return _fmt_error(result)
    return f"Version released: {result.get('name')}"


# =============================================================================
# ISSUE LINKS & WATCHERS
# =============================================================================

@mcp.tool(name="pulse_get_issue_links")
def get_issue_links(task_id: str) -> str:
    """
    Get all issue links for a task (blocks, relates to, duplicates, etc.).

    Args:
        task_id: Task ID, UUID, or item key (e.g. PLAT-74)
    """
    result = api_request("GET", _item_path(task_id, "links"))
    if "error" in result:
        return _fmt_error(result)

    inverse_types = {
        "blocks": "is_blocked_by",
        "is_blocked_by": "blocks",
        "duplicates": "is_duplicated_by",
        "is_duplicated_by": "duplicates",
        "parent_of": "child_of",
        "child_of": "parent_of",
        "clones": "is_cloned_by",
        "is_cloned_by": "clones",
        "relates_to": "relates_to",
    }

    links = []
    if isinstance(result, list):
        for link in result:
            links.append((link, "outward"))
    else:
        for link in result.get("outward_links", []):
            links.append((link, "outward"))
        for link in result.get("inward_links", []):
            links.append((link, "inward"))
        for link in result.get("results", []):
            links.append((link, "outward"))

    if not links:
        return "No links found."

    output = f"{len(links)} link(s):\n\n"
    for link, direction in links:
        if direction == "outward":
            relationship = link.get("link_type", "relates_to")
            key = link.get("target_item_key") or "N/A"
            title = link.get("target_item_title") or ""
            status = (link.get("target_item_status") or "").lower()
        else:
            relationship = inverse_types.get(link.get("link_type", "relates_to"), link.get("link_type", "relates_to"))
            key = link.get("source_item_key") or "N/A"
            title = link.get("source_item_title") or ""
            status = (link.get("source_item_status") or "").lower()

        status_note = f" | status: {status}" if status else ""
        if status in TERMINAL_STATUSES:
            status_note += " (terminal)"
        output += f"- {relationship} -> [{key}] {title}{status_note}\n"

    if any(
        (
            (direction == "outward" and (link.get("target_item_status") or "").lower() == "deferred")
            or (direction == "inward" and (link.get("source_item_status") or "").lower() == "deferred")
        )
        for link, direction in links
    ):
        output += "\nDeferred linked tasks are terminal historical decisions: do not count them as plate work or recreate duplicates.\n"
    return output


@mcp.tool(name="pulse_link_issues")
def link_issues(task_id: str, target_task_id: str, link_type: str = "relates_to") -> str:
    """
    Create a link between two issues.

    Args:
        task_id: Source task ID or item_key (e.g. 'PLAT-23')
        target_task_id: Target task ID or item_key
        link_type: Link type (relates_to, blocks, is_blocked_by, duplicates, is_duplicated_by, clones, is_cloned_by)
    """
    # ItemLinkCreateSerializer accepts `target_item_id` (numeric pk) or
    # `target_item_key` (e.g. "PLAT-25") — NOT a bare `target_item`. PLAT-48:
    # the previous payload used `target_item`, which the serializer ignored,
    # so every call failed validation ("Either target_item_id or
    # target_item_key is required"). Resolve to a numeric pk and send it as
    # `target_item_id`; if resolution can't yield a pk (e.g. an item_key the
    # local lookup missed), fall back to `target_item_key` so the backend
    # links by key or surfaces a clear "Item with key X not found" error.
    target = _resolve_item_pk(target_task_id)
    if isinstance(target, int):
        link_data = {"target_item_id": target, "link_type": link_type}
    else:
        link_data = {"target_item_key": str(target), "link_type": link_type}
    result = api_request("POST", _item_path(task_id, "links"), data=link_data)
    if "error" in result:
        return _fmt_error(result)
    return f"Link created: {link_type}"


@mcp.tool(name="pulse_unlink_issues")
def unlink_issues(task_id: str, target_task_id: str, link_type: str = "") -> str:
    """Remove an issue link between two tasks (PLS-80) — the inverse of pulse_link_issues.

    Use this to clear a STALE or erroneous link, e.g. a `blocks`/`is_blocked_by`
    dependency that was arbitrated away and is now false-blocking a task on every
    auto-block sweep (a blocker linked to a never-terminal epic never auto-resolves).

    Matches the link as seen from `task_id` (same relationship names
    pulse_get_issue_links prints), so direction/inverse is handled for you.

    Args:
        task_id: the task whose links to operate on (ID, UUID, or item key).
        target_task_id: the task on the OTHER end of the link
            (numeric ID, UUID, or item key).
        link_type: optional relationship as shown by pulse_get_issue_links
            (relates_to / blocks / is_blocked_by / duplicates / ...). If omitted,
            removes ALL links between the two tasks.
    """
    inverse_types = {
        "blocks": "is_blocked_by", "is_blocked_by": "blocks",
        "duplicates": "is_duplicated_by", "is_duplicated_by": "duplicates",
        "parent_of": "child_of", "child_of": "parent_of",
        "clones": "is_cloned_by", "is_cloned_by": "clones",
        "relates_to": "relates_to",
    }
    target = str(target_task_id).strip().upper()
    want = (link_type or "").strip().lower()

    def _find_matches(result, other_task_id, wanted_relationship):
        """Resolve links as viewed from the item whose listing was fetched."""
        found = []
        other_task = str(other_task_id).strip().upper()

        def _consider(link, direction):
            if direction == "outward":
                rel = link.get("link_type", "relates_to")
                other_identifiers = (
                    link.get("target_item_key"),
                    link.get("target_item"),
                    link.get("target_item_id"),
                )
            else:
                rel = inverse_types.get(
                    link.get("link_type", "relates_to"),
                    link.get("link_type", "relates_to"),
                )
                other_identifiers = (
                    link.get("source_item_key"),
                    link.get("source_item"),
                    link.get("source_item_id"),
                )
            if not link.get("id"):
                return
            # PLS-673: the tool contract accepts item keys and primary IDs.
            # Link listings expose both forms, so match the caller against the
            # endpoint aliases already authorized in this response. Resolving
            # the other task with a separate GET would regress the private-
            # blocker fallback: a dependent owner may see the edge but not the
            # blocker item itself.
            aliases = {
                str(identifier).strip().upper()
                for identifier in other_identifiers
                if identifier not in (None, "")
            }
            if other_task not in aliases:
                return
            if wanted_relationship and str(rel).lower() != wanted_relationship:
                return
            found.append((link["id"], rel))

        if isinstance(result, list):
            for link in result:
                _consider(link, "outward")
        else:
            for link in result.get("outward_links", []):
                _consider(link, "outward")
            for link in result.get("inward_links", []):
                _consider(link, "inward")
            for link in result.get("results", []):
                _consider(link, "outward")
        return found

    # Discover through the caller-supplied side first. A dependent owner in a
    # private project may intentionally be unable to read the blocker task, so
    # the inverse ``blocker blocks dependent`` form must fall back to the other
    # (dependent) side before giving up. DELETE through the same readable side
    # that exposed the edge; backend authorization normalizes to the dependent.
    result = api_request("GET", _item_path(task_id, "links"))
    primary_error = result if isinstance(result, dict) and "error" in result else None
    matches = [] if primary_error else _find_matches(result, target_task_id, want)
    delete_from = task_id

    if not matches:
        fallback = api_request("GET", _item_path(target_task_id, "links"))
        fallback_error = (
            fallback if isinstance(fallback, dict) and "error" in fallback else None
        )
        if not fallback_error:
            fallback_want = inverse_types.get(want, want) if want else ""
            fallback_matches = _find_matches(fallback, task_id, fallback_want)
            if fallback_matches:
                matches = [
                    (link_id, inverse_types.get(rel, rel))
                    for link_id, rel in fallback_matches
                ]
                delete_from = target_task_id
        elif primary_error:
            return _fmt_error(primary_error)

    if primary_error and not matches:
        return _fmt_error(primary_error)

    if not matches:
        return (f"No matching link found between {task_id} and {target_task_id}"
                + (f" of type '{link_type}'" if want else "") + ".")

    out = []
    for link_id, rel in matches:
        d = api_request(
            "DELETE",
            _item_path(delete_from, f"links/{quote(str(link_id), safe='')}"),
        )
        if isinstance(d, dict) and "error" in d:
            out.append(f"FAILED to remove {rel} -> {target}: {d.get('error')}")
        else:
            out.append(f"removed {rel} -> [{target}]")
    return "Unlinked:\n" + "\n".join(f"- {o}" for o in out)


def _declare_no_pr(item_id: str) -> str:
    """PLS-840 / v21 rule 14: record the task-level no-code declaration.

    Sets the canonical `no-pr` label and nothing else.  No provider request is
    issued (there is no provider), no `metadata.pull_requests` row is written,
    and existing rows are preserved as audit history.  Repeat calls are a no-op.
    """
    result = api_request("GET", _item_path(item_id))
    if "error" in result:
        return _fmt_error(result)

    labels = result.get("labels") or []
    if isinstance(labels, str):
        labels = [part.strip() for part in labels.split(",") if part.strip()]
    labels = [str(x) for x in labels]

    if any(str(x).strip().lower() == "no-pr" for x in labels):
        return (
            f"Task #{item_id} already carries the no-pr declaration "
            f"(no PR row written, no provider request made)"
        )

    patch = api_request(
        "PATCH", _item_path(item_id), data={"labels": labels + ["no-pr"]},
    )
    if "error" in patch:
        return f"Error declaring no-pr on task #{item_id}: {patch.get('error')}"
    return (
        f"Declared no-pr on task #{item_id} "
        f"(task-level flag; no PR row written, no provider request made)"
    )


@mcp.tool(name="pulse_unlink_pr")
def unlink_pr(item_id: str, pr_url: str, reason: str = "") -> str:
    """Remove a linked PR row from a task (PLAT-8392) — the inverse of pulse_link_pr.

    Use this to purge a STALE or retired-hostname PR link (e.g. a
    git.shizuha.com row wedging the PLAT-1296/E review re-entry gate with
    "no head_sha") so the carrier can be re-linked cleanly against the
    canonical Origin URL. The removed row is preserved under
    metadata['removed_pull_requests'] with the reason and timestamp — audit
    history is never deleted.

    Prefer pulse_link_pr re-link (auto-refresh/supersede) when the canonical
    PR is merely missing provenance; use this only when the row itself is
    stale beyond repair (retired hostname, wrong identity).

    Matching: exact URL first; otherwise provider-identity match
    (owner/repo/number) but ONLY for retired-shape rows (legacy
    git.shizuha.com host or legacy singular /pull/N) — a canonical
    origin.shizuha.com /pulls/N row is never removed by identity alone.

    Args:
        item_id: task ID or item key.
        pr_url: the PR URL to remove.
        reason: optional removal reason recorded in the audit trail.
    """
    fetched = api_request("GET", _item_path(item_id))
    if isinstance(fetched, dict) and fetched.get("error"):
        return f"Error fetching task #{item_id}: {fetched.get('error')}"
    if not isinstance(fetched, dict):
        return f"Error fetching task #{item_id}: unexpected response"
    metadata = fetched.get("metadata") or {}
    prs = metadata.get("pull_requests")
    if not isinstance(prs, list) or not prs:
        return f"Task #{item_id} has no linked PR rows"

    want = str(pr_url or '').strip().rstrip('/')
    if not want:
        return "Error: pr_url is required"
    want_identity = parse_linked_pr_identity(want)

    def _is_retired_shape(url: str) -> bool:
        return bool(
            parse_origin_pr_legacy_singular_url(url)
            or parse_forgejo_legacy_host_pr_url(url)
        )

    idx = None
    # Retired-shape rows first (exact, then identity): the purge use-case is
    # removing the stale variant of a carrier whose canonical row is live. A
    # canonical origin.shizuha.com /pulls/N row is only removed by its EXACT
    # URL, and only when no retired-shape variant of the same identity exists.
    for i, row in enumerate(prs):
        if not isinstance(row, dict):
            continue
        raw = str(row.get('url') or '').strip()
        if raw.rstrip('/') == want and _is_retired_shape(raw):
            idx = i
            break
    if idx is None and want_identity:
        for i, row in enumerate(prs):
            if not isinstance(row, dict):
                continue
            raw = str(row.get('url') or '').strip()
            if not _is_retired_shape(raw):
                continue
            if parse_linked_pr_identity(raw) == want_identity:
                idx = i
                break
    if idx is None:
        for i, row in enumerate(prs):
            if not isinstance(row, dict):
                continue
            if str(row.get('url') or '').strip().rstrip('/') == want:
                idx = i
                break
    if idx is None:
        return (
            f"No matching PR row on task #{item_id} for {want} "
            "(exact URL, or identity match on a retired-shape row)"
        )

    removed = prs.pop(idx)
    removed_audit = dict(removed)
    removed_audit['removed_at'] = _dt.datetime.utcnow().isoformat() + 'Z'
    removed_audit['removed_reason'] = str(reason or '').strip() or 'unspecified'
    removed_audit['removed_by'] = 'pulse_unlink_pr'
    archive = metadata.get('removed_pull_requests')
    if not isinstance(archive, list):
        archive = []
    archive.append(removed_audit)
    metadata['removed_pull_requests'] = archive
    metadata['pull_requests'] = prs

    patch_result = api_request("PATCH", _item_path(item_id), data={"metadata": metadata})
    if isinstance(patch_result, dict) and patch_result.get("error"):
        return f"Error unlinking PR from task #{item_id}: {patch_result.get('error')}"
    if patch_result is None:
        return f"Error unlinking PR from task #{item_id}: metadata PATCH failed"
    return (
        f"Unlinked PR {removed.get('url') or want} from task #{item_id} "
        f"(row archived to removed_pull_requests; {len(prs)} linked PR row(s) remain)"
    )


@mcp.tool(name="pulse_link_pr")
def link_pr(item_id: str, pr_url: str, title: Optional[str] = None, repo: Optional[str] = None, pr_number: Optional[int] = None) -> str:
    """
    Link a pull request to a task. Shows in the task's Development sidebar.

    PLS-441 / PLS-496: fetches current ``head.sha`` from GitHub **or Origin
    Forgejo** at link time so the review re-entry gate has provenance.
    Re-linking refreshes/backfills ``head_sha`` and true open/merged/closed
    status rather than returning early.

    Args:
        item_id: Task ID to link the PR to
        pr_url: Full URL of the pull request (e.g., https://github.com/org/repo/pull/12)
        title: PR title (auto-extracted from URL if not provided)
        repo: Repository name (auto-extracted from URL if not provided)
        pr_number: PR number (auto-extracted from URL if not provided)
    """
    import re

    # PLS-840 / Standard v21 rule 14: the exact `no-pr` sentinel is a TASK-LEVEL
    # no-code DECLARATION, not a provider PR tuple.  Normalise it at ingress:
    # set the canonical task flag, append NO metadata.pull_requests row, make NO
    # provider request, and leave existing rows (including a legacy sentinel
    # audit row) untouched.  Idempotent.
    #
    # Before this, every call appended another row whose url was 'no-pr' -- a
    # row with no provider, no head and no possible advancement, which the
    # re-entry gate then evaluated as an unsatisfiable open obligation.  Fixing
    # only the reader would have left this writer recreating the wedge.
    #
    # Recognition is deliberately strict (trim + casefold, exact literal): this
    # branch WAIVES a code-PR obligation, so prefixes, suffixes, paths, queries,
    # fragments and Unicode lookalikes must fall through to normal parsing and
    # fail closed.
    if str(pr_url or '').strip().lower() == 'no-pr':
        return _declare_no_pr(item_id)

    # PLS-834 / PLAT-1296 rule 13: reject every non-canonical Origin PR shape
    # before any persistence path (including repo/pr_number overrides and
    # query/fragment smuggling that bypasses the exact singular fullmatch).
    ingress_err = origin_pr_link_ingress_error(pr_url)
    if ingress_err:
        return ingress_err
    gh_match = re.match(r'https?://github\.com/([^/]+/[^/]+)/pull/(\d+)', pr_url or '')
    if gh_match:
        return (
            "Error: GitHub is a read-only mirror and is not a Pulse code "
            "pull request. Link the Origin pull request that carries this "
            f"change ({_current_topology().origin_public_url.rstrip('/')}/<owner>/<repo>/pulls/<n>). "
            "A github.com pull request is never merge or assignment evidence, "
            "including an open-source contribution — that change is merged "
            "on Origin first."
        )
    # PLAT-4675 Facet 1: post ORIG-18, PRs live on Shizuha Origin (Forgejo). A
    # Forgejo PR URL is https://origin.shizuha.com/<owner>/<repo>/pulls/<n> (note
    # the plural `pulls`, unlike GitHub's singular `pull`). Recognise it so we can
    # populate repo/number AND fetch head_sha from the Forgejo API at link time —
    # without it, Origin PRs get no provenance and the review re-entry gate blocks
    # forever. SECURITY: the pattern is HARD-BOUND to the exact canonical host over
    # HTTPS — the authenticated fetch carries FORGEJO_TOKEN, so it must never be
    # sent to an arbitrary/attacker-supplied host (kai review, PR #8 P1).
    fj_coords = parse_origin_pr_reference(pr_url) if not gh_match else None
    if gh_match and not repo:
        repo = gh_match.group(1)
    if gh_match and not pr_number:
        pr_number = int(gh_match.group(2))
    if fj_coords and not repo:
        repo = f"{fj_coords[0]}/{fj_coords[1]}"
    if fj_coords and not pr_number:
        pr_number = fj_coords[2]
    if not title:
        title = f"PR #{pr_number}" if pr_number else pr_url

    frozen = linked_pr_development_block_reason(pr_url, repo)
    if not frozen and fj_coords:
        frozen = development_repo_block_reason(fj_coords[0], fj_coords[1])
    if frozen:
        return f"ERROR: {frozen}"

    # Fetch current head_sha and state from the PR host's API (PLS-441 /
    # PLAT-4675 / PLS-496).
    head_sha = None
    merge_commit_sha = None
    merged_at = None
    pr_status = None  # open|closed|merged when known
    provider_fetch_confirmed = False
    origin_fetch_confirmed = False
    canonical_origin_url = None
    if gh_match:
        gh_owner_repo = gh_match.group(1)
        gh_num = int(gh_match.group(2))
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            try:
                gh_headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                gh_resp = requests.get(
                    f"https://api.github.com/repos/{gh_owner_repo}/pulls/{gh_num}",
                    headers=gh_headers,
                    timeout=15,
                )
                # PLS-1226 (secondary ruling): a DEFINITIVE not-found from the
                # provider means the PR does not exist — persisting it would
                # mint an open obligation no provider fetch can ever confirm
                # (the PLAT-5671 wedge class). Transient failures (5xx, auth,
                # timeout, no token) keep the deferred-provenance design.
                if getattr(gh_resp, 'status_code', None) == 404:
                    return (
                        f"Error: PR not found on GitHub "
                        f"({gh_owner_repo}#{gh_num}) — refusing to link a "
                        "nonexistent PR as an open obligation. Verify the "
                        "URL; if the PR was deleted, link its replacement."
                    )
                if gh_resp.ok:
                    gh_data = gh_resp.json()
                    head_sha = ((gh_data.get('head') or {}).get('sha') or '').strip()
                    provider_fetch_confirmed = bool(head_sha)
                    merged_at = gh_data.get('merged_at')
                    merge_commit_sha = (gh_data.get('merge_commit_sha') or '').strip()
                    if gh_data.get('merged') or merged_at:
                        pr_status = 'merged'
                    elif (gh_data.get('state') or '').lower() == 'closed':
                        pr_status = 'closed'
                    else:
                        pr_status = 'open'
            except Exception:
                logger.warning("pulse_link_pr: failed to fetch head_sha for %s", pr_url, exc_info=True)
    elif fj_coords:
        # Coordinates are accepted only by the shared exact-canonical parser;
        # the token destination remains a constant trusted Origin API base.
        fj_owner, fj_repo, fj_num = fj_coords
        token = os.environ.get("FORGEJO_TOKEN") or os.environ.get("ORIGIN_TOKEN")
        if token:
            try:
                fj_resp = requests.get(
                    f"{_origin_api_base()}/repos/{fj_owner}/{fj_repo}/pulls/{fj_num}",
                    headers={"Authorization": f"token {token}", "Accept": "application/json"},
                    timeout=15,
                )
                # PLS-1226 (secondary ruling): definitive not-found → the PR
                # does not exist; never mint an unconfirmable open obligation.
                if getattr(fj_resp, 'status_code', None) == 404:
                    return (
                        f"Error: PR not found on Origin "
                        f"({fj_owner}/{fj_repo}#{fj_num}) — refusing to link "
                        "a nonexistent PR as an open obligation. Verify the "
                        "URL; if the PR was deleted, link its replacement."
                    )
                if fj_resp.ok:
                    fj_data = fj_resp.json()
                    head_sha = ((fj_data.get('head') or {}).get('sha') or '').strip()
                    merged_at = fj_data.get('merged_at')
                    merge_commit_sha = (fj_data.get('merge_commit_sha') or '').strip()
                    if fj_data.get('merged') or merged_at or fj_data.get('has_merged'):
                        pr_status = 'merged'
                    elif (fj_data.get('state') or '').lower() == 'closed':
                        pr_status = 'closed'
                    else:
                        pr_status = 'open'
                    # PLS-496 / Standard v8 rule 5: mirror supersession is a
                    # trusted server-side write.  A successful response without
                    # immutable head provenance is not enough to disposition a
                    # frozen GitHub obligation.
                    if head_sha:
                        provider_fetch_confirmed = True
                        origin_fetch_confirmed = True
                        canonical_origin_url = canonical_origin_pr_url(fj_owner, fj_repo, fj_num)
            except Exception:
                logger.warning("pulse_link_pr: failed to fetch Forgejo head_sha for %s", pr_url, exc_info=True)
        else:
            logger.warning("pulse_link_pr: no FORGEJO_TOKEN/ORIGIN_TOKEN — cannot fetch head_sha for %s", pr_url)

    # Get current task
    result = api_request("GET", _item_path(item_id))
    if "error" in result:
        return _fmt_error(result)

    metadata = result.get("metadata") or {}
    prs = metadata.get("pull_requests", [])

    def _pr_identity(url: str):
        """Return provider coordinates for canonical URLs or strict short keys."""
        origin_coords = parse_origin_pr_reference(url)
        if origin_coords:
            return ("origin", origin_coords[0].lower(), origin_coords[1].lower(), origin_coords[2])
        github_match = re.fullmatch(
            r'https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)/?',
            str(url or '').strip(),
        )
        if github_match:
            return (
                "github",
                github_match.group(1).lower(),
                github_match.group(2).lower(),
                int(github_match.group(3)),
            )
        return None

    def _parse_metadata_timestamp(value):
        raw = str(value or '').strip()
        if not raw:
            return None
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        try:
            parsed = _dt.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)

    def _backfill_missing_bounce_head(existing_row):
        """PLS-684: heal one provider-confirmed partial bounce snapshot.

        A populated bounce head is immutable audit history. A null entry (or an
        absent entry for a PR demonstrably linked before the bounce) is a
        capture failure, so a relink may seed it to the freshly confirmed
        provider head. Seeding the current head does not itself prove progress:
        the normal re-entry gate still requires a later advance or an exact
        scoped rebuttal. A PR linked after a no-PR bounce is deliberately not
        synthesized here; Standard rule 7 keeps its stricter first-artifact
        rebuttal path.
        """
        if (
            not provider_fetch_confirmed
            or not head_sha
            or pr_status != 'open'
            or not metadata.get('review_bounce_at')
        ):
            return None

        raw_heads = metadata.get('review_bounce_pr_heads')
        if raw_heads is not None and not isinstance(raw_heads, dict):
            return None
        bounced_heads = dict(raw_heads or {})

        existing_url = str(existing_row.get('url') or '').strip().rstrip('/')
        existing_identity = parse_linked_pr_identity(existing_url)
        matching_key = next(
            (
                raw_key for raw_key in bounced_heads
                if str(raw_key or '').strip().rstrip('/') == existing_url
            ),
            None,
        )
        identity_only_match = False
        if matching_key is None and existing_identity:
            matching_key = next(
                (
                    raw_key for raw_key in bounced_heads
                    if parse_linked_pr_identity(raw_key) == existing_identity
                ),
                None,
            )
            identity_only_match = matching_key is not None

        if matching_key is not None:
            # Never overwrite a real historical snapshot.
            if str(bounced_heads.get(matching_key) or '').strip():
                return None
            if identity_only_match:
                # A null pre-cutover mirror/short key has no historical value
                # to preserve. Replace it with the current row's canonical key
                # so the re-entry validator compares the repaired snapshot to
                # the same live tuple instead of retaining two representations.
                if not existing_url:
                    return None
                bounced_heads.pop(matching_key, None)
                target_key = existing_url
            else:
                target_key = matching_key
        else:
            # A missing key is repairable only when the row was already linked
            # at bounce time. This distinguishes partial capture from the
            # no-PR-bounce -> first-code-artifact contract.
            bounce_at = _parse_metadata_timestamp(metadata.get('review_bounce_at'))
            linked_at = _parse_metadata_timestamp(existing_row.get('linked_at'))
            if not bounce_at or not linked_at or linked_at > bounce_at:
                return None
            target_key = existing_url
            if not target_key:
                return None

        bounced_heads[target_key] = head_sha
        metadata['review_bounce_pr_heads'] = bounced_heads
        metadata['review_bounce_backfilled_at'] = (
            _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        )
        metadata['review_bounce_backfill_source'] = 'pulse_link_pr'
        return target_key

    def _supersede_mirror_rows(pr_list, canonical_url: str | None, *, provider_confirmed: bool):
        """Annotate matching retired mirror/legacy rows without rewriting history.

        Covers (a) pre-cutover GitHub mirrors, (b) already-persisted Origin
        singular ``/pull/N`` rows, and (c) already-persisted legacy
        ``git.shizuha.com``-host rows once a trusted provider confirms the
        canonical plural Origin tuple for the same owner/repo/number
        (PLS-834 / PLAT-1296 v9 + live residual PLAT-7725).
        """
        if not provider_confirmed or not canonical_url:
            return False
        ident = _pr_identity(canonical_url)
        if not ident or ident[0] != "origin":
            return False
        _, owner, repo_name, number = ident
        changed = False
        for row in pr_list:
            raw_url = str(row.get("url") or "").strip()
            other = _pr_identity(raw_url)
            is_github_mirror = bool(other and other[0] == "github" and other[1:] == (owner, repo_name, number))
            legacy = parse_origin_pr_legacy_singular_url(raw_url)
            is_legacy_singular = bool(
                legacy
                and legacy[0].lower() == owner
                and legacy[1].lower() == repo_name
                and legacy[2] == number
            )
            forgejo_legacy = parse_forgejo_legacy_host_pr_url(raw_url)
            is_forgejo_legacy_host = bool(
                forgejo_legacy
                and forgejo_legacy[0].lower() == owner
                and forgejo_legacy[1].lower() == repo_name
                and forgejo_legacy[2] == number
            )
            if not is_github_mirror and not is_legacy_singular and not is_forgejo_legacy_host:
                continue
            if row.get("superseded_by") != canonical_url:
                row["superseded_by"] = canonical_url
                row["last_synced_at"] = _dt.datetime.utcnow().isoformat() + "Z"
                changed = True
        return changed

    # Check for existing entry — refresh/backfill if found (PLS-441)
    existing = None
    incoming_identity = _pr_identity(pr_url)
    for p in prs:
        if p.get("url") == pr_url or (
            incoming_identity and _pr_identity(p.get("url") or "") == incoming_identity
        ):
            existing = p
            break

    if existing:
        changed = []
        if head_sha and existing.get('head_sha') != head_sha:
            existing['head_sha'] = head_sha
            existing['last_synced_at'] = _dt.datetime.utcnow().isoformat() + "Z"
            changed.append('head_sha')
        if merge_commit_sha and existing.get('merge_commit_sha') != merge_commit_sha:
            existing['merge_commit_sha'] = merge_commit_sha
            changed.append('merge_commit_sha')
        if pr_status and existing.get('status') != pr_status:
            existing['status'] = pr_status
            existing['last_synced_at'] = _dt.datetime.utcnow().isoformat() + "Z"
            changed.append('status')
        if merged_at and existing.get('merged_at') != merged_at:
            existing['merged_at'] = merged_at
            changed.append('merged_at')
        if _supersede_mirror_rows(
            prs,
            canonical_origin_url,
            provider_confirmed=origin_fetch_confirmed,
        ):
            changed.append('mirror_superseded')
        if _backfill_missing_bounce_head(existing):
            changed.append('bounce_head')
        if changed:
            metadata["pull_requests"] = prs
            patch_result = api_request("PATCH", _item_path(item_id), data={"metadata": metadata})
            if isinstance(patch_result, dict) and patch_result.get("error"):
                return f"Error refreshing PR on task #{item_id}: {patch_result.get('error')}"
            if patch_result is None:
                return f"Error refreshing PR on task #{item_id}: metadata PATCH failed"
            return f"Refreshed PR #{pr_number or ''} ({repo or pr_url}) on task #{item_id}: {', '.join(changed)}"
        return f"PR already linked to task #{item_id}"

    new_entry = {
        "url": pr_url,
        "title": title,
        "repo": repo,
        "number": pr_number,
        "status": pr_status or "open",
        "linked_at": _dt.datetime.utcnow().isoformat() + "Z",
    }
    if head_sha:
        new_entry["head_sha"] = head_sha
        new_entry["last_synced_at"] = _dt.datetime.utcnow().isoformat() + "Z"
    if merge_commit_sha:
        new_entry['merge_commit_sha'] = merge_commit_sha
    if merged_at:
        new_entry['merged_at'] = merged_at
    prs.append(new_entry)
    _supersede_mirror_rows(
        prs,
        canonical_origin_url,
        provider_confirmed=origin_fetch_confirmed,
    )
    metadata["pull_requests"] = prs

    update_result = api_request("PATCH", _item_path(item_id), data={"metadata": metadata})
    if "error" in update_result:
        return _fmt_error(update_result)

    return f"Linked PR #{pr_number or ''} ({repo or pr_url}) to task #{item_id}"


def _origin_creation_notice(data: dict) -> str:
    metadata = data.get('metadata') or {}
    observations = metadata.get('origin_observations') if isinstance(metadata, dict) else []
    rendered = _fmt_origin_observations({'origin_observations': observations}, include_url=True)
    return f"\nOrigin evidence: {rendered}" if rendered else ""


@mcp.tool(name="pulse_link_origin_run")
def link_origin_run(
    item_id: str,
    run_url: str,
    repo: Optional[str] = None,
    commit_sha: Optional[str] = None,
    ref: Optional[str] = None,
    workflow: Optional[str] = None,
    check: Optional[str] = None,
    image: Optional[str] = None,
) -> str:
    """Attach immutable Shizuha Origin run/commit evidence to an existing task.

    The run URL is enriched from Forgejo into the exact full commit SHA, ref,
    workflow, run id/number and terminal state. Explicit fields are safe
    fallbacks when the run is old or Origin enrichment is temporarily down.
    """
    current = api_request("GET", _item_path(item_id))
    if "error" in current:
        return _fmt_error(current)

    observation = parse_origin_run_url(run_url)
    observation.update({
        key: value for key, value in {
            'run_url': run_url,
            'repo': repo,
            'commit_sha': commit_sha,
            'ref': ref,
            'workflow': workflow,
            'check': check,
            'image': image,
            'observed_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }.items() if value
    })
    try:
        observation = _enrich_origin_observation(observation)
    except ValueError as exc:
        return f"Error: invalid Origin observation: {exc}"

    # Pulse merges this server-side so this tool never round-trips/replaces the
    # task's unrelated metadata from its preceding existence check.
    updated = api_request(
        "PATCH", _item_path(item_id),
        data={'append_origin_observations': [observation]},
    )
    if "error" in updated:
        return _fmt_error(updated)
    rendered = _fmt_origin_observations(
        {'origin_observations': [observation]}, include_url=True
    )
    return f"Linked Origin evidence to [{updated.get('item_key') or item_id}]: {rendered}"


@mcp.tool(name="pulse_watch_item")
def watch_item(task_id: str) -> str:
    """Start watching a task to receive notifications about changes."""
    result = api_request("POST", _item_path(task_id, "watchers"))
    if "error" in result:
        return _fmt_error(result)
    return "Now watching this item."


@mcp.tool(name="pulse_unwatch_item")
def unwatch_item(task_id: str) -> str:
    """Stop watching a task."""
    result = api_request("DELETE", _item_path(task_id, "watchers"))
    if "error" in result:
        return _fmt_error(result)
    return "Stopped watching this item."


# =============================================================================
# BLOCKER ESCALATION
# =============================================================================

# Canonical team → lead-email map. Used as a fallback if admin API doesn't
# return a match. Keys are normalized: lowercase, hyphens (not spaces or
# underscores). Keep in sync with shizuha-pulse/tasks/views.py::_resolve_team_lead.
def _normalize_team_slug(slug: str) -> str:
    """Normalize a team slug/name to lowercase-hyphenated for matching."""
    return (slug or "").strip().lower().replace("_", "-").replace(" ", "-")


_TEAM_ALIASES = {
    # PLS-539 (invariant I1): `ceo-decisions` is a real, org-scoped decision team
    # (e.g. its per-org CEO Decisions team lead) and must NOT be rewritten to the
    # physical-only `human-ops` lane — doing so blocked routing a valid operator
    # decision to the CEO Decisions lead. It resolves by exact slug within the
    # task organization; `human-ops` stays physical-only, explicit-only.
    # operator-gated escalations still fold to human-ops:
    "leads": "human-ops",
    "triage": "human-ops",
    # security pass-through
    "security-triage": "security",
    # 'devops' is the CANONICAL infra/ops team (operator 2026-07-11 renamed
    # platform-ops -> devops for one terminology). The old 'platform-ops'
    # spelling back-compat-aliases to it; devops itself is canonical (below),
    # NOT aliased to engineering — infra work must not route to the code team.
    "platform-ops": "devops",
    # Operator 2026-07-16: one consolidated Research team. Legacy emitters may
    # keep using either short label, but all writes resolve to this slug.
    "analytics": "research-analytics",
    "research": "research-analytics",
}

_CANONICAL_ASSIGNMENT_GROUPS = {
    "human-ops",  # operator-only queue (PLS-74 / PLAT-724)
    "ceo-decisions",  # PLS-539: org-scoped CEO Decisions team (never aliased to human-ops)
    "admin-ops",
    "devops",     # infra/ops team (renamed from platform-ops, operator 2026-07-11)
    "engineering",
    "security",
    "review",
    "merge",
    "qa",
    "architecture",
    "documentation",
    "research-analytics",
    "social-media",
}


def _canonical_team_slug(slug: str) -> str:
    norm = _normalize_team_slug(slug)
    return _TEAM_ALIASES.get(norm, norm)


def _operator_blocker_hygiene_error(team_slug: str, title: str, description: str):
    """Reject raw operator decisions before they become assigned blockers.

    Workflow transitions already enforce PLAT-3136, but this convenience tool
    creates its child directly and previously bypassed that gate. The operator
    receives a prepared recommendation, not an open vendor investigation.
    """
    # PLS-539: the operator-decision hygiene gate governs BOTH operator lanes —
    # physical-only human-ops AND the org-scoped ceo-decisions team. ceo-decisions
    # is no longer canonicalized to human-ops (invariant I1), so name it
    # explicitly here rather than relying on that (removed) alias.
    if _canonical_team_slug(team_slug) not in ("human-ops", "ceo-decisions"):
        return None

    title_text = (title or "").strip().lower()
    body_text = (description or "").strip().lower()
    if "/.../" in body_text:
        return (
            "Error: operator action blockers must contain an exact executable path, "
            "not an ellipsis placeholder. Refresh the artifact/preconditions with the "
            "owning team, then raise the physical-only step. No blocker task was "
            "created and the parent was left unchanged."
        )
    if title_text.startswith(("decide ", "choose ", "select ")) and "recommend" not in body_text:
        return (
            "Error: operator decision blockers must include the owning team's "
            "specific recommendation and rationale before routing to human-ops. "
            "Keep the parent with its owning team, finish the investigation, then "
            "raise one crisp A/B approval ask. No blocker task was created and the "
            "parent was left unchanged."
        )
    return None


@mcp.tool(name="pulse_create_blocker_task")
def create_blocker_task(
    parent_id: str,
    team_slug: str,
    title: str,
    description: str,
    priority: str = "urgent",
    evidence_owner_email: str = "",
    required_evidence: str = "",
    due_date: str = "",
    human_lane_tag: str = "",
) -> str:
    """
    Escalate a blocker: file a new decision/action task on the right team,
    link it as blocking the parent, and transition the parent to blocked.

    Use this when your in-progress task cannot proceed without an action
    from another team — typically the CEO (billing, legal, privacy toggles,
    org-level credentials), Security Triage (security finding), or a sibling
    engineering team. One call creates the child task assigned to that team's
    lead, records a blocks-link from child to parent, and marks the parent
    blocked so the board reflects the true state.

    Prefer this over pulse_create_task + pulse_link_issues + pulse_execute_transition
    — this tool does all three correctly and reports any partial failures.

    Args:
        parent_id: The current task's ID (UUID or item_key like "PLAT-9") that
                   is blocked and needs someone else to act.
        team_slug: Target team. Prefer canonical teams: human-ops, admin-ops,
                   ceo-decisions, engineering, security, review, merge, qa,
                   architecture, documentation, analytics. Aliases are accepted
                   and canonicalized: leads/triage -> human-ops,
                   security-triage -> security. (PLS-539: `ceo-decisions` is a
                   real, org-scoped CEO Decisions team and is NEVER aliased to the
                   physical-only human-ops lane — route operator DECISIONS there,
                   not physical operator actions.)
                   - human-ops = operator-only actions (billing, legal, privacy,
                     org-level creds, physical hardware) — routes to the operator
                   - admin-ops = CoS / admin operations (non-operator team tasks)
                   - security = security findings, new vulns, credential leaks
                   - engineering = full-stack / infra asks
                   - architecture = HLD/API contract design questions
        title: One-line decision statement for the receiving team. Example:
               "Decide sara2574 GitHub tier: Pro ($4/mo) or make hikari public"
        description: Full markdown body: what's blocked, what you tried, options,
                     the owning team's specific recommendation + rationale,
                     acceptance criteria for closing, and urgency.
        priority: low, normal, high, urgent (defaults to urgent — blockers usually are)
        evidence_owner_email: Optional named external evidence owner. When set,
            required_evidence and due_date are also required; the child is marked
            as an event-driven evidence-request blocker and assigned directly to
            this owner rather than transiently team-resolved.
        required_evidence: Exact evidence the named owner must supply.
        due_date: ISO-8601 deadline for the evidence request.
        human_lane_tag: Required when team_slug is human-ops. One of
            physical-hardware or role-gated-operator. The human-ops validator
            rejects filings that cannot carry this tag (HIVE-2523 / PLS-1397).

    Returns:
        Human-readable string summarizing what was created/linked/transitioned,
        including any partial failures.

    Example:
        pulse_create_blocker_task(
            parent_id="PLAT-9",
            team_slug="ceo-decisions",
            title="Decide sara2574 GitHub tier: upgrade to Pro OR make hikari repo public",
            description=(
                "## Why this is on you\\n"
                "Branch-protection API returns 403 ... only CEO can decide.\\n\\n"
                "## Recommendation\\nUpgrade to Pro because the repo must stay private.\\n\\n"
                "## Options\\n- A. Upgrade to Pro (~$4/mo)\\n- B. Make repo public\\n\\n"
                "## Acceptance\\n- [ ] Pick A/B in a comment, close this task."
            ),
        )
    """
    hygiene_error = _operator_blocker_hygiene_error(team_slug, title, description)
    if hygiene_error:
        return hygiene_error

    lane_tag = (human_lane_tag or "").strip().lower().replace("_", "-")
    if _canonical_team_slug(team_slug) == "human-ops" and lane_tag not in {
        "physical-hardware", "role-gated-operator",
    }:
        return (
            "Error: human-ops filings must carry human_lane_tag="
            "physical-hardware or role-gated-operator. "
            "The validator rejects an untagged filing (uncarryable tag: "
            "role-gated-operator). No blocker task was created."
        )

    # Resolve parent (accept item_key like PLAT-9 or UUID)
    parent = api_request("GET", _item_path(parent_id))
    if "error" in parent:
        return f"Error: parent task lookup failed for {parent_id!r}: {parent.get('error')}"
    parent_uuid = parent.get("id")
    parent_key = parent.get("item_key") or parent_id
    parent_project_id = (
        parent.get("project_id")
        or (parent.get("project") or {}).get("id")
        if isinstance(parent.get("project"), dict)
        else parent.get("project")
    )
    if not parent_project_id:
        return (
            f"Error: could not determine project_id from parent {parent_key}. "
            "The blocker task needs the same project as its parent."
        )

    norm = _canonical_team_slug(team_slug)
    if norm not in _CANONICAL_ASSIGNMENT_GROUPS:
        return (
            f"Error: unknown team {team_slug!r}. Known teams: "
            f"{sorted(_CANONICAL_ASSIGNMENT_GROUPS)}. Aliases: "
            f"{sorted(_TEAM_ALIASES.keys())}."
        )

    evidence_values = [
        bool((evidence_owner_email or "").strip()),
        bool((required_evidence or "").strip()),
        bool((due_date or "").strip()),
    ]
    if any(evidence_values) and not all(evidence_values):
        return (
            "Error: evidence_owner_email, required_evidence, and due_date must "
            "all be provided for an evidence-request blocker."
        )

    # Normalize literal escape sequences. Agents sometimes pass the description with
    # literal "\n"/"\t" two-char sequences instead of real newlines, which then render
    # as a wall of "\n" in the UI (operator-flagged 2026-06-22, the VEN-24 description).
    # Only unescape when the body has NO real newline, so already-correct markdown is
    # never mangled.
    if "\n" not in description and "\\n" in description:
        description = (
            description.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        )

    # Build the child task. Use the simple workflow — this is a decision/ask,
    # not software-dev work, so it doesn't need the autonomous-dev lifecycle.
    child_tags = "blocker,decision-required,escalation"
    if lane_tag:
        child_tags = f"{child_tags},{lane_tag}"
    child_data = {
        "mode": "task",
        "title": title,
        "description": (
            description.rstrip()
            + f"\n\n---\n_Escalated from {parent_key} via pulse_create_blocker_task._"
        ),
        "priority": priority,
        "project": parent_project_id,
        "workflow_name": "simple",
        "assignment_group": norm,
        "tags": child_tags,
    }
    if all(evidence_values):
        owner = evidence_owner_email.strip().lower()
        if owner == os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower():
            return (
                "Error: evidence-request deadline escalation cannot target the CEO. "
                "Route genuine operator-only decisions through the normal escalation gate."
            )
        child_data.update({
            "assignee_email": owner,
            "direct_assignment_reason": (
                f"Named evidence owner for blocker of {parent_key}; event-driven due escalation"
            ),
            "due_date": due_date.strip(),
            "tags": child_tags + ",evidence-request",
            "metadata": {
                "evidence_request": {
                    "owner_email": owner,
                    "required_evidence": required_evidence.strip(),
                }
            },
        })
    # Stable across retries of the same filing. Not a fresh uuid (pulse#735)
    # and not only the transport hash — the caller can search this key if the
    # create landed before the timeout.
    create_dedup = _transport_dedup_key(
        "create_blocker",
        parent_key,
        {"title": title, "team": norm, "lane": lane_tag},
    )
    child_data.setdefault("metadata", {})
    if isinstance(child_data["metadata"], dict):
        child_data["metadata"]["create_dedup_key"] = create_dedup
    child = api_request("POST", "/items/", data=child_data)
    if child.get("error_code") == "pulse_transport_timeout" or child.get("error_class") == "request_timeout":
        return (
            "Error: create_timeout retryable=true "
            f"error_code=pulse_transport_timeout dedup_key={create_dedup} "
            f"parent={parent_key} team={norm}. "
            "The create may have landed. Retry the same filing and search "
            f"create_dedup_key before filing a second blocker. detail={child.get('error')}"
        )
    if "error" in child:
        return _fmt_error(child)
    child_id = child.get("id")
    child_key = child.get("item_key", "?")

    # Assign via the dedicated endpoint so team→available-agent resolution,
    # email→user_id resolution, and Connect DM fire. Degrade to a warning if
    # this fails — the task exists and still carries assignment_group.
    warnings = []
    if not all(evidence_values):
        assign_res = api_request(
            "POST",
            _item_path(child_id, "assign"),
            data={"assignment_group": norm},
        )
        if "error" in assign_res:
            warnings.append(
                f"Child task carries assignment_group={norm}, but /assign/ team routing failed: "
                f"{assign_res.get('error')}. Run pulse_assign_task({child_key}, assignment_group={norm!r})."
            )

    # Link child → blocks → parent. (child blocks parent; parent is blocked by child.)
    # The ItemLinkCreateSerializer accepts `target_item_key` (e.g. "PLAT-25") or
    # `target_item_id` (integer pk) — NOT a UUID via `target_item`.
    link_res = api_request(
        "POST",
        _item_path(child_id, "links"),
        data={"target_item_key": parent_key, "link_type": "blocks"},
    )
    if "error" in link_res:
        warnings.append(
            f"Link {child_key} blocks {parent_key} failed: {link_res.get('error')}. "
            f"Run pulse_link_issues({child_key}, {parent_key}, link_type='blocks')."
        )

    # Transition parent to blocked via the Escalate to Leads transition.
    # If the parent's workflow doesn't have that transition (e.g. simple
    # workflow), swallow the error — the child task + link are enough to
    # surface the blocker; the parent just stays where it was.
    txn_res = api_request(
        "POST",
        _item_path(parent_uuid, "execute-transition"),
        data={
            "to_status": "blocked",
            "comment": f"Blocked on {child_key} ({norm}). Waiting on decision/action.",
        },
    )
    if "error" in txn_res:
        warnings.append(
            f"Parent {parent_key} not transitioned to blocked "
            f"(its workflow may not allow it): {txn_res.get('error')}. "
            "The child task + blocks-link are still in place."
        )

    assigned_to = (
        evidence_owner_email.strip().lower()
        if all(evidence_values)
        else (assign_res.get('assignee_email') or child.get('assignee_email') or 'team queue')
    )
    lines = [
        f"✓ Escalation filed.",
        f"  Child task: [{child_key}] {title}",
        f"  Team: {norm} → assigned to {assigned_to}",
        f"  Parent: {parent_key} is now blocked by {child_key}",
    ]
    if warnings:
        lines.append("")
        lines.append("Partial issues (task still created — resolve these manually):")
        for w in warnings:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


# =============================================================================
# WORK LOGS (TIME TRACKING)
# =============================================================================

@mcp.tool(name="pulse_log_work")
def log_work(task_id: str, time_spent_minutes: int, description: str = "", work_date: Optional[str] = None) -> str:
    """
    Log time spent on a task.

    Args:
        task_id: Item ID (numeric PK) or item_key (e.g., "AT-83")
        time_spent_minutes: Minutes spent
        description: What was done
        work_date: Date of work (ISO format, defaults to now)
    """
    # WorkLogCreateSerializer expects duration_minutes / worked_date and a
    # numeric item PK. Keep the MCP-facing argument names stable while mapping
    # to the backend contract, including item-key resolution for recurring
    # project tasks such as SM-1.
    worked_date = work_date or _dt.date.today().isoformat()
    if "T" in str(worked_date):
        # DateField accepts YYYY-MM-DD; trim common ISO datetimes supplied by
        # agents without changing the public tool signature.
        worked_date = str(worked_date).split("T", 1)[0]

    data = {
        "item": _resolve_item_pk(task_id),
        "duration_minutes": time_spent_minutes,
        "worked_date": worked_date,
    }
    if description: data["description"] = description
    result = api_request("POST", "/work-logs/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Logged {time_spent_minutes} minutes (ID: {result.get('id')})"


@mcp.tool(name="pulse_list_work_logs")
def list_work_logs(task_id: Optional[str] = None, limit: int = 20) -> str:
    """
    List work logs, optionally for a specific task.

    Args:
        task_id: Item ID (numeric PK) or item_key (e.g., "AT-83")
        limit: Max results
    """
    params = {"page_size": limit}
    if task_id:
        params["item"] = _resolve_item_pk(task_id)
    result = api_request("GET", "/work-logs/", params=params)
    if "error" in result:
        return _fmt_error(result)
    logs = result.get("results", [])
    if not logs:
        return "No work logs found."
    output = f"{len(logs)} work log(s):\n\n"
    for log in logs:
        output += f"- {log.get('duration_minutes', 0)}min by {log.get('user_email', 'unknown')} on {log.get('worked_date', 'N/A')}\n"
        if log.get("description"): output += f"  {log['description'][:80]}\n"
    return output


# =============================================================================
# SAVED FILTERS
# =============================================================================

@mcp.tool(name="pulse_list_saved_filters")
def list_saved_filters(project_id: Optional[int] = None) -> str:
    """
    List saved JQL filters.

    Args:
        project_id: Filter by project
    """
    params = {}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/saved-filters/", params=params)
    if "error" in result:
        return _fmt_error(result)
    filters = result.get("results", [])
    if not filters:
        return "No saved filters."
    output = f"{len(filters)} filter(s):\n\n"
    for f in filters:
        output += f"- {f.get('name')} (ID: {f.get('id')})\n"
        if f.get("jql_query"): output += f"  JQL: {f['jql_query']}\n"
    return output


@mcp.tool(name="pulse_execute_saved_filter")
def execute_saved_filter(filter_id: str) -> str:
    """
    Execute a saved filter and return matching items.

    Args:
        filter_id: Saved filter ID
    """
    result = api_request("GET", f"/saved-filters/{filter_id}/execute/")
    if "error" in result:
        return _fmt_error(result)
    items = (result if isinstance(result, list) else result.get("results", []))
    return _fmt_items(items)


@mcp.tool(name="pulse_create_saved_filter")
def create_saved_filter(
    name: str,
    jql: str,
    project_id: Optional[int] = None,
    description: str = "",
    is_shared: bool = False,
    as_user: bool = False,
) -> str:
    """
    Create a saved JQL filter.

    Args:
        name: Filter name (must be unique per owner + project scope)
        jql: JQL query string (validated before saving; malformed JQL is rejected)
        project_id: Project scope (omit for global scope)
        description: Optional filter description
        is_shared: Whether the filter is shared with the team
        as_user: Operate as the chatting user (delegation) instead of the agent
    """
    data = {"name": name, "jql": jql}
    if project_id is not None:
        data["project"] = project_id
    if description:
        data["description"] = description
    if is_shared:
        data["is_shared"] = is_shared
    result = api_request("POST", "/saved-filters/", data=data, as_user=as_user)
    if "error" in result:
        return _fmt_error(result)
    return f"Saved filter created: {result.get('name')} (ID: {result.get('id')})"


@mcp.tool(name="pulse_update_saved_filter")
def update_saved_filter(
    filter_id: str,
    name: Optional[str] = None,
    jql: Optional[str] = None,
    description: Optional[str] = None,
    is_shared: Optional[bool] = None,
    project_id: Optional[int] = None,
    as_user: bool = False,
) -> str:
    """
    Update an existing saved JQL filter. Only provided fields change.

    Args:
        filter_id: Saved filter ID (UUID)
        name: New filter name (duplicate names rejected)
        jql: New JQL query string (validated; malformed JQL rejected)
        description: New description
        is_shared: New shared flag
        project_id: New project scope
        as_user: Operate as the chatting user (delegation) instead of the agent
    """
    data = {}
    if name is not None:
        data["name"] = name
    if jql is not None:
        data["jql"] = jql
    if description is not None:
        data["description"] = description
    if is_shared is not None:
        data["is_shared"] = is_shared
    if project_id is not None:
        data["project"] = project_id
    result = api_request(
        "PATCH", f"/saved-filters/{filter_id}/", data=data, as_user=as_user
    )
    if "error" in result:
        return _fmt_error(result)
    return f"Saved filter updated: {result.get('name')} (ID: {result.get('id')})"


@mcp.tool(name="pulse_delete_saved_filter")
def delete_saved_filter(filter_id: str, as_user: bool = False) -> str:
    """
    Delete a saved JQL filter permanently.

    Args:
        filter_id: Saved filter ID (UUID)
        as_user: Operate as the chatting user (delegation) instead of the agent
    """
    result = api_request("DELETE", f"/saved-filters/{filter_id}/", as_user=as_user)
    if "error" in result:
        return _fmt_error(result)
    return "Saved filter deleted successfully."


# =============================================================================
# WORKFLOWS
# =============================================================================

@mcp.tool(name="pulse_list_workflows")
def list_workflows() -> str:
    """
    List all workflows. Each workflow defines statuses and allowed transitions.
    Workflows are assigned to projects via Workflow Schemes.
    """
    result = api_request("GET", "/workflows/")
    if "error" in result:
        return _fmt_error(result)
    workflows = (result if isinstance(result, list) else result.get("results", []))
    if not workflows:
        return "No workflows found."
    output = f"{len(workflows)} workflow(s):\n\n"
    for w in workflows:
        output += f"- **{w.get('name')}** (ID: {w.get('id')}, slug: {w.get('slug')})\n"
        if w.get("description"): output += f"  {w['description'][:100]}\n"
        output += f"  Statuses: {w.get('status_count', '?')} | Transitions: {w.get('transition_count', '?')}"
        if w.get("initial_status_name"): output += f" | Initial: {w['initial_status_name']}"
        output += "\n"
    return output


@mcp.tool(name="pulse_get_available_transitions")
def get_available_transitions(item_id: Union[int, str]) -> str:
    """
    Get available status transitions for a specific task/item.
    Returns transitions the current user can execute based on the item's
    current status and workflow (respects conditions, roles, etc.).

    Args:
        item_id: Item/task ID, item_key (e.g. 'PLAT-23'), UUID, or numeric PK
    """
    result = api_request("GET", _item_path(item_id, "available-transitions"))
    if "error" in result:
        return _fmt_error(result)
    current = result.get("current_status", "unknown")
    transitions = result.get("transitions", [])
    if not transitions:
        return f"No transitions available from '{current}' for item #{item_id}."
    output = f"Item #{item_id} — current status: {current}\nAvailable transitions:\n\n"
    # Detect to_status values shared by >1 transition so we can flag that the
    # caller MUST pass transition_id to disambiguate.
    from collections import Counter as _Counter
    _to_counts = _Counter(t.get("to_status") for t in transitions)

    def _status_hint(to_status: str) -> str:
        if to_status == "blocked":
            return (
                "  Use for real blockers only. For linked blockers, create/link the "
                "active blocker (`BLOCKER --blocks--> THIS`); Pulse auto-blocks linked "
                "work when the workflow supports it. For escalation transitions, follow "
                "the transition article and explain what no agent can unblock.\n"
            )
        if to_status == "cancelled":
            return (
                "  Terminal discard path for invalid/test/no-longer-needed artifacts. "
                "Use Deferred, not Cancelled, for valid work intentionally parked by an operator.\n"
            )
        if to_status == "deferred":
            return (
                "  Terminal parked-decision path. Deferred tasks are not actionable plate work, "
                "but they still count as existing work for duplicate prevention.\n"
            )
        return ""

    for t in transitions:
        name = t.get("name", "")
        to_status = t.get("to_status", t.get("to_status_label", "unknown"))
        to_label = t.get("to_status_label", to_status)
        tid = t.get("id", "")
        output += f"- **{name}** → {to_label} (slug: {to_status}, id: {tid})"
        if _to_counts.get(to_status, 0) > 1:
            output += "  ⚠️ shares this target with another transition — pass transition_id to execute"
        if t.get("screen_fields"):
            # PLS-756: expose each required screen field with its exact enum so
            # callers know the accepted payload shape before executing (the
            # Cancel/Discard reason_category contract must not be discoverable
            # only from the frontend).
            parts = []
            payload = {}
            for f in t["screen_fields"]:
                name = f.get("field") or f.get("label") or f.get("key") or "?"
                enum = f.get("enum") or []
                if enum:
                    parts.append(f"{name} ({'|'.join(enum)})")
                else:
                    parts.append(name)
                if f.get("required"):
                    payload[name] = "<value>"
            output += f" [requires: {', '.join(parts)}]"
            output += "\n"
            if payload:
                import json as _json
                output += (
                    f"  Execute payload (required fields): "
                    f"screen_data={_json.dumps(payload, sort_keys=True)}\n"
                )
        else:
            output += "\n"
        # Show description (instructions for this transition)
        if t.get("description"):
            output += f"  Instructions: {t['description']}\n"
        hint = _status_hint(to_status)
        if hint:
            output += hint
        # Show knowledge articles (MUST read before executing). PLS-224:
        # _safe_json_list so a legacy raw-string field never iterates chars.
        for ka in _safe_json_list(t.get("knowledge_articles")):
            ka_type = ka.get("type", "inline")
            ka_title = ka.get("title", "")
            if ka_type == "inline":
                output += f"  📋 **{ka_title}**: {ka.get('content', '')}\n"
            elif ka_type in ("wiki", "link"):
                output += f"  🔗 **{ka_title}**: {ka.get('url', '')}\n"
    return output


@mcp.tool(name="pulse_prepare_external_wait")
def prepare_external_wait(
    item_id: str,
    release_policy: str,
    local_display_reference: Optional[str] = None,
) -> str:
    """Prepare a one-use cross-org wait capability for a local carrier."""
    result = api_request(
        "POST",
        _item_path(item_id, "prepare-external-wait"),
        data={
            "release_policy": release_policy,
            "local_display_reference": local_display_reference or "",
        },
    )
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, sort_keys=True)


@mcp.tool(name="pulse_accept_external_wait")
def accept_external_wait(provider_item_id: str, handoff_token: str) -> str:
    """Accept a capability on an authorized provider; returns no carrier tuple."""
    result = api_request(
        "POST",
        _item_path(provider_item_id, "accept-external-wait"),
        data={"handoff_token": handoff_token},
    )
    if "error" in result:
        return "Error: external_wait_not_accepted"
    return f"External wait accepted (receipt: {result.get('receipt', '')})."


@mcp.tool(name="pulse_release_external_wait")
def release_external_wait(provider_item_id: str) -> str:
    """Emit an explicit-ready provider release when the binding policy permits."""
    result = api_request(
        "POST", _item_path(provider_item_id, "release-external-wait"), data={}
    )
    if "error" in result:
        return _fmt_error(result)
    return f"External release accepted ({result.get('release_count', 0)} binding(s))."


@mcp.tool(name="pulse_revoke_external_wait")
def revoke_external_wait(item_id: str, reason: str) -> str:
    """Revoke a local carrier binding without asserting provider completion."""
    result = api_request(
        "POST",
        _item_path(item_id, "revoke-external-wait"),
        data={"reason": reason},
    )
    if "error" in result:
        return _fmt_error(result)
    return f"External wait revoked (receipt: {result.get('receipt', '')})."


@mcp.tool(name="pulse_release_human_wait")
def release_human_wait(item_id: str) -> str:
    """Resume a human-held wait into work; use settlement for a fulfilled ask."""
    result = api_request(
        "POST", _item_path(item_id, "release-human-wait"), data={}
    )
    if "error" in result:
        return _fmt_error(result)
    return f"Human external wait released (receipt: {result.get('receipt', '')})."


@mcp.tool(name="pulse_settle_human_wait")
def settle_human_wait(
    item_id: str,
    receipt: str,
    wait_generation: int,
    transition_id: str,
    summary: str,
    resolutions: list[dict],
    screen_data: Optional[dict] = None,
) -> str:
    """Record fulfillment of a human-held ask without starting holder work.

    Use its current receipt/generation and configured resolved_elsewhere
    transition. Supply one immutable parent comment reference per blocker
    parent: {parent_id, comment_id, sha256}. Record the actual answer on each
    parent first. The API enforces permissions, evidence, workflow and replay
    checks; it does not approve a decision or waive unmet acceptance.
    """
    result = api_request(
        "POST", _item_path(item_id, "settle-human-wait"),
        data={
            "receipt": receipt,
            "wait_generation": wait_generation,
            "transition_id": transition_id,
            "summary": summary,
            "resolutions": resolutions,
            "screen_data": screen_data if screen_data is not None else {},
        },
    )
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, sort_keys=True)


@mcp.tool(name="pulse_admit_external_response_hold")
def admit_external_response_hold(
    item_id: str,
    checkpoint_at: str,
    source_evidence: dict,
    display_reference: dict,
    correlation_key: str,
) -> str:
    """Admit a bounded typed external-response hold on a local carrier."""
    result = api_request(
        "POST",
        _item_path(item_id, "admit-external-response-hold"),
        data={
            "checkpoint_at": checkpoint_at,
            "source_evidence": source_evidence,
            "display_reference": display_reference,
            "correlation_key": correlation_key,
        },
    )
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, sort_keys=True)


@mcp.tool(name="pulse_wake_external_response_hold")
def wake_external_response_hold(
    item_id: str,
    wait_generation: int,
    source_evidence: dict,
    correlation_key: str,
) -> str:
    """Record typed same-org evidence and enqueue a generation-fenced wake."""
    result = api_request(
        "POST",
        _item_path(item_id, "wake-external-response-hold"),
        data={
            "wait_generation": wait_generation,
            "source_evidence": source_evidence,
            "correlation_key": correlation_key,
        },
    )
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, sort_keys=True)


@mcp.tool(name="pulse_revoke_external_response_hold")
def revoke_external_response_hold(
    item_id: str,
    wait_generation: int,
    correlation_key: str,
) -> str:
    """Enqueue a generation-safe owner/current-lane revoke."""
    result = api_request(
        "POST",
        _item_path(item_id, "revoke-external-response-hold"),
        data={
            "wait_generation": wait_generation,
            "correlation_key": correlation_key,
        },
    )
    if "error" in result:
        return _fmt_error(result)
    return json.dumps(result, sort_keys=True)


@mcp.tool(name="pulse_execute_transition")
def execute_transition(item_id: str, to_status: Optional[str] = None, transition_id: Optional[str] = None, resolution: Optional[str] = None, comment: Optional[str] = None, screen_data: Optional[dict] = None) -> str:
    """
    Execute a workflow transition on a task/item. Moves it to a new status.
    The transition must be valid per the item's workflow (use pulse_get_available_transitions first).

    Args:
        item_id: Item/task ID to transition
        to_status: Target status slug (e.g., 'in_progress', 'done', 'closed').
                   Optional if transition_id is given.
        transition_id: Exact transition id (from pulse_get_available_transitions).
                   REQUIRED when two transitions from the current status share
                   the same target status — otherwise the transition is
                   ambiguous and the call is rejected with the candidate ids.
        resolution: Resolution slug for done/closed transitions (e.g., 'fixed', 'wont_fix')
        comment: Optional comment to add during the transition
        screen_data: Screen field values required by the transition (e.g., {"priority": "high"}).
                     Check pulse_get_available_transitions to see which fields are required
                     (the renderer lists each required field with its exact enum).

                     Cancel/Discard transitions additionally require a reason
                     category — pass screen_data={"reason_category": "<one of
                     duplicate|invalid|obsolete|test-artifact>"} with a matching
                     comment; the exact enum is reported by
                     pulse_get_available_transitions. An unknown or missing
                     category is rejected with the accepted values listed.

                     Operator-bound transitions such as ``Raise to Admin Ops``
                     require ``screen_data.escalation_evidence`` with this exact
                     shape (all keys are required). ``evidence_class`` must be
                     one the validator can accept (PLS-1397 — do not invent a
                     class, and do not omit a class the validator accepts):

                     - ``human_held_source_evidence`` | ``operator_decision`` |
                       ``operator_held_credential_access`` with holder_type
                       ``operator`` | ``admin_ops`` | ``external_authority``
                     - ``human_physical_action`` with holder_type
                       ``physical_operator`` and requested_kind ``physical_action``
                     - ``internal_capability_gap`` with holder_type
                       ``service_team`` and requested_kind ``technical_action``

                     requested_kind is one of fact, artifact, signoff, decision,
                     credential_access, physical_action, technical_action.

                     {
                       "schema": "shizuha.pulse.escalation-evidence.v1",
                       "request_id": "<fresh UUID>",
                       "expected_generation": <latest receipt generation, or 0>,
                       "dependency": {
                         "evidence_class": "human_held_source_evidence",
                         "requested_kind": "artifact",
                         "requested_subject": "<one exact atomic ask>",
                         "holder_type": "operator",
                         "holder_ref": "<exact holder>",
                         "acceptance_check": "<observable completion check>",
                         "source_refs": [{"kind": "task", "id": "<this task key>"}]
                       },
                       "technical_action": null
                     }

                     Missing or malformed typed evidence creates an owned
                     schema-repair HOLD; it is not an operator escalation and
                     must not be manually routed.
    """
    if not to_status and not transition_id:
        return "Error: provide either to_status or transition_id (get ids from pulse_get_available_transitions)."
    data = {}
    if to_status: data["to_status"] = to_status
    if transition_id: data["transition_id"] = transition_id
    if resolution: data["resolution"] = resolution
    if comment: data["comment"] = comment
    if screen_data:
        if isinstance(screen_data, dict):
            data["screen_data"] = screen_data
        elif isinstance(screen_data, str):
            try:
                data["screen_data"] = json.loads(screen_data)
            except (json.JSONDecodeError, TypeError):
                pass
    result = api_request("POST", _item_path(item_id, "execute-transition"), data=data)
    if "error" in result:
        return _fmt_error(result)
    if result.get("pending") is True:
        return _pending_workflow_message(result, item_id)
    new_status = result.get("status", to_status)
    title = result.get("title", f"Item #{item_id}")

    # Format knowledge articles as '📋 inline' or '🔗 link'.
    def _fmt_articles(articles):
        lines = []
        for a in _safe_json_list(articles):
            t = a.get("type", "inline")
            title_ = a.get("title", "")
            if t == "inline":
                content = (a.get("content", "") or "").strip()
                lines.append(f"  📋 **{title_}**\n     {content}")
            elif t in ("wiki", "link"):
                lines.append(f"  🔗 **{title_}**: {a.get('url', '')}")
        return "\n".join(lines)

    out = [f"✓ Transitioned '{title}' to status '{new_status}'."]

    # A successful review submission is an ownership handoff, not a reason for
    # the author to occupy the turn polling Origin.  Put the stop condition in
    # the transition receipt itself so every MCP client sees it immediately,
    # including already-running agents whose inline skills predate the rule.
    if new_status == "in_review":
        out.append(
            "\n### Author handoff complete — END THIS TURN\n"
            "The item is now owned by the Review team. Do not sleep or poll for "
            "reviews, merge, or deploy; event-driven workflow notifications and "
            "the next automated turn own those steps."
        )

    receipt = result.get("escalation_receipt") or {}
    if receipt:
        out.append(
            "\n### Typed escalation receipt\n"
            f"- Classification: `{receipt.get('classification')}`\n"
            f"- Outcome/reason: `{receipt.get('outcome')}` / `{receipt.get('reason_code')}`\n"
            f"- Generation: `{receipt.get('generation')}`\n"
            f"- Final owner: `{receipt.get('final_status')}` / `{receipt.get('final_team')}` / "
            f"`{receipt.get('final_assignee_email') or receipt.get('final_assignee_id')}`\n"
            f"- Receipt/disposition: `{receipt.get('receipt_id')}` / `{receipt.get('disposition_id')}`\n"
            f"- Child: `{receipt.get('child_item_key') or 'none'}`\n"
            f"- Replay/no-op: `{bool(receipt.get('replayed'))}`; "
            f"cross-actor convergence: `{bool(receipt.get('replayed_disposition'))}`"
        )

    # Knowledge articles for the transition just executed — helpful when the
    # caller skipped pulse_get_available_transitions and fired blind.
    exec_t = result.get("executed_transition") or {}
    exec_articles = _safe_json_list(exec_t.get("knowledge_articles"))
    if exec_t:
        out.append(f"\n### Transition executed: {exec_t.get('name','?')}")
    if exec_t.get("description"):
        out.append(f"Instructions: {exec_t['description']}")
    if exec_articles:
        out.append("**Knowledge articles for this transition (READ — may contain stop conditions):**")
        out.append(_fmt_articles(exec_articles))

    # Next-hop transitions + their articles — prevents blind chaining of
    # multiple transitions in the same turn. Agents MUST check these before
    # firing another transition from the new status.
    next_ts = result.get("next_transitions") or []
    if next_ts:
        out.append(f"\n### Next available transitions from '{new_status}' (do NOT auto-fire without reading the articles):")
        for t in next_ts:
            header = f"- **{t.get('name','?')}** → {t.get('to_status','?')}"
            if t.get("id"):
                header += f" (id: {t.get('id')})"
            roles = t.get("allowed_roles") or []
            if roles:
                header += f"  [roles: {', '.join(roles)}]"
            out.append(header)
            if t.get("description"):
                out.append(f"  Instructions: {t.get('description')}")
            if t.get("screen_fields"):
                fields = [f.get("field", f.get("label", f.get("key", "?"))) for f in _safe_json_list(t.get("screen_fields"))]
                out.append(f"  Requires: {', '.join(fields)}")
            articles = _safe_json_list(t.get("knowledge_articles"))
            if articles:
                out.append(_fmt_articles(articles))
    else:
        out.append(f"\n(No further transitions available from '{new_status}' — terminal state.)")

    return "\n".join(out)


@mcp.tool(name="pulse_get_workflow_detail")
def get_workflow_detail(workflow_id: str) -> str:
    """
    Get full details of a workflow including all statuses and transitions.

    Args:
        workflow_id: Workflow UUID
    """
    result = api_request("GET", f"/workflows/{workflow_id}/")
    if "error" in result:
        return _fmt_error(result)
    output = f"**{result.get('name')}** (slug: {result.get('slug')})\n"
    if result.get("description"): output += f"{result['description']}\n"
    output += f"\nInitial status: {result.get('initial_status_name', '?')}\n"

    statuses = result.get("statuses", [])
    if statuses:
        output += f"\n**Statuses** ({len(statuses)}):\n"
        for s in statuses:
            output += f"  - {s.get('status_name', '?')} [{s.get('status_category', '?')}] ({s.get('status_slug', '?')})\n"

    # Knowledge base (workflow-level guidelines). PLS-224: _safe_json_list so a
    # legacy raw-string knowledge_base never iterates chars.
    kb = _safe_json_list(result.get("knowledge_base"))
    if kb:
        output += "\n**Knowledge Base** (READ BEFORE PROCEEDING):\n"
        for ka in kb:
            ka_type = ka.get("type", "inline")
            ka_title = ka.get("title", "")
            if ka_type == "inline":
                output += f"  📋 **{ka_title}**: {ka.get('content', '')}\n"
            elif ka_type in ("wiki", "link"):
                output += f"  🔗 **{ka_title}**: {ka.get('url', '')}\n"

    transitions = result.get("transitions", [])
    if transitions:
        output += f"\n**Transitions** ({len(transitions)}):\n"
        for t in transitions:
            frm = t.get("from_status_label", t.get("from_status", "?"))
            to = t.get("to_status_label", t.get("to_status", "?"))
            output += f"  - {t.get('name', '?')}: {frm} → {to}"
            if t.get("is_global"): output += " [global]"
            # PLS-224: count via _safe_json_list so a legacy raw-string field
            # reports the real article count, never a character count.
            _pf = _safe_json_list(t.get("post_functions"))
            if _pf: output += f" [{len(_pf)} post-functions]"
            _ka = _safe_json_list(t.get("knowledge_articles"))
            if _ka: output += f" [📋 {len(_ka)} knowledge article(s)]"
            output += "\n"
            if t.get("description"):
                output += f"    Instructions: {t['description']}\n"
    return output


# =============================================================================
# WORKFLOW CHANGE MANAGEMENT
# =============================================================================

def _accessible_projects_for_proposal() -> tuple[list, Optional[str]]:
    """Fetch the caller's writable projects for workflow-change CR binding.

    Returns ``(projects, error)``: ``projects`` is the list of raw project dicts
    the caller can create items in (all pages, same source as
    ``pulse_list_projects`` and positively proven by its ``can_create`` bit);
    ``error`` is a formatted string if the projects fetch itself failed (else
    None). Used by ``propose_workflow_change`` (PLS-574) to (a) list candidate
    projects when ``project_id`` is missing and (b) authorize an explicit
    ``project_id`` at the MCP boundary before creating the CR.
    """
    base = {"page_size": 200}
    projects: list = []
    page = 1
    while True:
        result = api_request("GET", "/projects/", params={**base, "page": page})
        if isinstance(result, dict) and "error" in result:
            return [], _fmt_error(result)
        if isinstance(result, list):
            projects.extend(result)
            break
        projects.extend(result.get("results", []))
        if not result.get("next"):
            break
        page += 1
        if page > 50:  # safety cap (50 * 200 = 10k projects)
            break
    # Visibility is not authorization. Missing/false capability data is denied
    # so a stale/mixed-version project-list response cannot fail open.
    return [project for project in projects if project.get("can_create") is True], None


@mcp.tool(name="pulse_propose_workflow_change")
def propose_workflow_change(
    title: str,
    target_workflow: str,
    change_spec: str,
    reason: str,
    impact: str = "",
    project_id: Optional[int] = None,
) -> str:
    """
    Propose a change to a workflow. Creates a change request task that goes through
    the workflow-change approval process (proposed → review → approved → applied).

    Risk-tiered approval (PLAT-189): ROUTINE/additive changes (knowledge articles, DoD/screen
    text, reminder gates, label/description tweaks — no new statuses/transitions, no blocking
    validators, no permission/security/irreversible impact) route to the governance approver
    (Shizuha, Chief of Staff). RISKY changes (structural/new states/transitions, validators,
    permission/security, irreversible/data) require operator (admin-ops) approval. Default is
    RISKY when uncertain.

    Args:
        title: Short title for the change (e.g., "Add awaiting_merge status to software-development")
        target_workflow: Slug of the workflow to modify (e.g., "software-development")
        change_spec: PREFERRED: a TYPED op envelope (PLAT-218 contract, ENG wiki
            d328824d): '{"version":1,"operations":[{"op":"set_knowledge_article",
            "workflow":"autonomous-dev","transition":"Merge","content":"..."}],
            "rationale":"display-only"}'. Tier derives from op TYPES (routine text
            ops -> governance; structural/permission/destructive ops -> operator);
            `rationale` is never parsed for tier. LEGACY: free YAML/prose is still
            accepted and always classifies RISKY (operator).
        reason: Why this change is needed
        impact: Which agents/teams are affected
        project_id: REQUIRED (PLS-574, contract d328824d §3.9). The organization
            project the change-request ITEM is tracked in — its tracking/authz
            home. This is NOT the scope of the target workflow's effect (a
            workflow may be shared across many projects/orgs); it only says where
            the CR lives. Explicit `project_id` is required even when you have
            exactly one writable project, because this is an authorization/audit
            boundary — the tool never silently picks "the first"/an org default.
            Call `pulse_list_projects` to choose one. A missing/foreign/unwritable
            id fails at the MCP boundary with actionable candidates and creates
            no item (never falls back to another project).
    """
    # PLS-574 (architecture contract d328824d §3.9 — org project binding): a
    # workflow-change CR is an org-scoped `mode=task` item, so POST /items/ now
    # rejects it without a project ("A task must belong to an organization
    # project."). Resolve + AUTHORIZE the project BEFORE creating the item, at
    # the MCP boundary, so a missing/foreign id never leaks through as the
    # backend's generic 400 and never silently defaults to a wrong project.
    _proj_candidates, _proj_err = _accessible_projects_for_proposal()
    if not project_id:
        _hint = ""
        if _proj_candidates:
            _lines = "\n".join(
                f"  • [{p.get('key')}] {p.get('name')} → project_id={p.get('id')} "
                f"(org {p.get('org_id')}{(' ' + p['organization_name']) if p.get('organization_name') else ''})"
                for p in _proj_candidates[:25]
            )
            _more = f"\n  … and {len(_proj_candidates) - 25} more (call pulse_list_projects)" if len(_proj_candidates) > 25 else ""
            _hint = f"\nWritable projects you can file this CR in:\n{_lines}{_more}"
        elif _proj_err:
            _hint = f"\n(Could not list your projects right now: {_proj_err})"
        return (
            "Error: `project_id` is required — a workflow-change proposal must be "
            "tracked in an organization project (its tracking/authz home; this is "
            "NOT the scope of the target workflow's effect). No item was created. "
            "Call `pulse_list_projects` to choose one, then pass its id as "
            "`project_id`." + _hint
        )
    # Authorize the supplied id against the caller's positively writable set.
    # Any discovery/authz error fails closed: never POST and never rely on a
    # downstream object-permission path that may not run during create.
    if _proj_err:
        return (
            "Error: could not verify project write permission, so the workflow-"
            f"change proposal was not created (fail closed): {_proj_err}. Retry "
            "after `pulse_list_projects` succeeds."
        )
    _writable_ids = {p.get("id") for p in _proj_candidates}
    if project_id not in _writable_ids:
        return (
            f"Error: project_id={project_id} is not a project you can file a "
            f"workflow-change CR in (not among your accessible/writable "
            f"projects). No item was created and no other project was "
            f"substituted. Pick one from `pulse_list_projects`."
        )
    # PLAT-218: typed envelope first. A valid op-typed spec classifies by OP
    # TYPE (table lookup, fail-closed per the contract §3) — prose/rationale is
    # provably never inspected. Invalid-but-typed specs are REJECTED with an
    # actionable error (no CR created); non-JSON falls through to the legacy
    # prose classifier below (always risky-leaning, unchanged).
    _envelope = _tcs.parse_envelope(change_spec)
    _typed = _envelope is not None
    if _typed:
        # PLAT-253 #3 (§1 version strictness): a typed-looking envelope with
        # version != 1 REJECTS with an actionable error instead of silently
        # parsing as typed (or degrading to the prose path).
        _verr = _tcs.version_error(_envelope)
        if _verr:
            return f"Error: change request REJECTED (not created): {_verr}"
        # §3.6 (v1.1): every op must address the declared target_workflow —
        # the approver's display and the execution must be the same object.
        _check = _tcs.validate_and_classify(
            _envelope, fetch_workflow=_fetch_workflow_by_slug, target_workflow=target_workflow)
        if _check['reject']:
            return f"Error: change request REJECTED (not created): {_check['reject']}"
        _tier = _check['tier']
        _spec_block = f"```json\n{json.dumps(_envelope, indent=2)}\n```"
        _spec_note = "**Spec format**: TYPED (op-typed contract — tier from op types)"
    else:
        _spec_block = f"```yaml\n{change_spec}\n```"
        _spec_note = "**Spec format**: legacy free text (prose classifier, deny-by-default)"

    description = f"""## Workflow Change Request

**Target Workflow**: `{target_workflow}`
{_spec_note}

### Reason
{reason}

### Impact
{impact or "TBD"}

### Change Specification
{_spec_block}

### Rollback
Revert by removing the added statuses/transitions via admin tools.
"""

    # PLAT-189: risk-tier the routing (policy PLAT-188). DENY-BY-DEFAULT, TOKEN-STRUCTURAL.
    # The change_spec is free YAML/prose, so substring-marker scanning leaks (Codex found
    # 4 under-classification evasions across rounds — the DANGEROUS direction, since a
    # mis-classified-routine change is routed to + approvable by Shizuha). So classify by
    # TOKENS (split on non-alphanumerics, which also breaks `remove_screen_field`,
    # `drop_*`, articled `add a status`, etc.): a CR is ROUTINE only if it touches a known
    # safe-ADDITIVE target (knowledge-article / DoD / label / description / reminder /
    # wording text) AND mentions NO structural target and NO destructive op. ANY structural
    # target token (status/transition/validator/permission/role/state/gate/screen/field/
    # workflow) OR any destructive token (delete/remove/drop/migrate/rename/…) OR a
    # structural YAML key -> RISKY. Unparseable / structural-mention / empty -> RISKY.
    # Over-classification (a routine edit that merely NAMES a transition -> operator) is the
    # SAFE direction and accepted; the durable breadth-restoring fix is a STRUCTURED propose
    # contract (op-typed spec) — tracked as a PLAT-189 follow-up. ROUTINE -> governance
    # (Shizuha); RISKY -> admin-ops (operator).
    import re as _re
    _STRUCTURAL_TARGETS = {
        'status', 'statuses', 'state', 'states', 'transition', 'transitions',
        'validator', 'validators', 'permission', 'permissions', 'role', 'roles',
        'gate', 'gates', 'screen', 'field', 'fields', 'workflow', 'workflows',
    }
    _DESTRUCTIVE = {'delete', 'remove', 'drop', 'migrate', 'migration', 'irreversible', 'rename'}
    # §4.2 security hard-signal (restored from round-2; lost in the token rewrite). A
    # proposer-DECLARED security impact must be RISKY even with a safe-additive word present
    # (a real security change usually also trips permission/role, but the honest
    # "security impact:" note must be caught on its own too).
    _SECURITY = {'security', 'auth', 'authentication', 'authorization', 'credential',
                 'credentials', 'secret', 'secrets', 'token', 'tokens', 'acl', 'privacy'}
    # Additional risky operation/prose tokens (validators, bulk/data ops, automation,
    # conditions, approvals) — the prose forms of risky operations.
    _RISKY_OPS = {
        'validation', 'rule', 'rules', 'blocking', 'approval', 'approvals',
        'backfill', 'bulk', 'data', 'sla', 'automation', 'condition', 'conditions',
        # Behavioral / post-function ops (round-6): the apply path is PROSE-INTERPRETIVE
        # (an agent applier reads the descriptive change_spec and runs admin_* tools), so a
        # prose-described post-function / notification / escalation / reassignment change is
        # genuinely applicable -> RISKY.
        'trigger', 'triggers', 'hook', 'hooks', 'webhook', 'webhooks',
        'notify', 'notification', 'notifications', 'escalate', 'escalation',
        'reassign', 'reassignment',
    }
    # Substring catches for multi-word/hyphenated ops that tokenization splits (e.g.
    # "post-function"/"post_function" -> {post, function}, neither a clean risky token). The
    # KEY form `post_function:` is already caught by the key-allowlist; this also catches
    # the PROSE form.
    _RISKY_SUBSTRINGS = ('post_function', 'post-function', 'postfunction')
    _SAFE_ADDITIVE = {
        'knowledge', 'article', 'articles', 'dod', 'label', 'labels', 'description',
        'reminder', 'reminders', 'instructions', 'wording', 'reword', 'clarify',
        'clarification', 'text', 'typo', 'narration', 'guidance', 'comment', 'doc',
        'docs', 'documentation',
    }
    # GENERIC structural-key allowlist (PLAT-189, aoi): per-token enumeration is a losing
    # game (8 evasions across 5 rounds). Deny-by-default on SPEC KEYS: ANY `word:` key in
    # the change_spec NOT in the known-safe-additive key set -> RISKY. Closes an entire
    # class in one rule — `assignee_only:` (SoD-gate weakening — serious), `conditions:`,
    # `reporter_only:`, `validator:`, `statuses:`/`transitions:`, `backfill:`, `remove_*:`,
    # `post_function:` etc. — no per-key whack-a-mole. (`\w` includes underscore, so
    # snake_case keys match whole.) The conservative over-rejection (a routine spec using a
    # non-safe key -> operator) is the SAFE direction; the durable breadth-restorer is the
    # structured op-typed contract (PLAT-218).
    _SAFE_KEYS = {
        'knowledge_article', 'knowledge_articles', 'description', 'label', 'labels',
        'dod', 'reminder', 'reminders', 'text', 'instructions', 'wording',
        # PLAT-189 (aoi #1): nested CONTENT keys of the canonical routine types
        # (knowledge articles / screens / DoD bodies). Without these a well-formed
        # `knowledge_articles:` with `title:`/`content:` children gets a non-safe key
        # -> _has_unsafe_key -> RISKY -> admin-ops, so the PRIMARY delegated routine
        # type (knowledge-article edits) would NEVER delegate. Safe to allow: structural
        # INTENT is still caught by the token sets (status/transition/validator/
        # permission/role/screen/field/...) regardless of these content keys, so e.g.
        # `status: {name: ...}` is still RISKY via the `status` token. Conservative
        # over-rejection of any non-listed nested key remains the SAFE direction.
        'title', 'content', 'body', 'name', 'summary',
    }
    _spec_keys = set(_re.findall(r'(\w+)\s*:', change_spec.lower()))
    _has_unsafe_key = bool(_spec_keys - _SAFE_KEYS)
    _blob = f"{change_spec}\n{reason}\n{impact}".lower()
    _tokens = set(_re.split(r'[^a-z0-9]+', _blob))
    _has_risky = (
        _has_unsafe_key
        or bool(_tokens & _STRUCTURAL_TARGETS)
        or bool(_tokens & _DESTRUCTIVE)
        or bool(_tokens & _SECURITY)
        or bool(_tokens & _RISKY_OPS)
        or any(_s in _blob for _s in _RISKY_SUBSTRINGS)
    )
    _has_safe_additive = bool(_tokens & _SAFE_ADDITIVE)
    if not _typed:
        # Legacy free-text path — byte-identical behavior to PLAT-189 (#22).
        _tier = "routine" if (_has_safe_additive and not _has_risky) else "risky"

    # SoD (PLAT-187/188): approver != proposer. The governance approver is Shizuha, so a
    # ROUTINE CR PROPOSED BY Shizuha must NOT route to governance (she can't approve her
    # own) — send it to admin-ops instead. This also keeps the solo governance roster from
    # resolving back to the proposer (the #3 self-fallback case). Proposer identity comes
    # from the forwarded caller JWT (_get_my_user_id).
    _proposer_uid, _proposer_email = _get_my_user_id()
    _proposer_is_governance = (_proposer_email or "").strip().lower() == "shizuha@shizuha.com"
    if _tier == "routine" and not _proposer_is_governance:
        assignment_group = "governance"
    else:
        assignment_group = "admin-ops"

    data = {
        "mode": "task",
        "title": f"[Workflow Change] {title}",
        "description": description,
        "workflow_name": "workflow-change",
        "assignment_group": assignment_group,
        "priority": "high",
        # A workflow-change CR is a governance proposal, NEVER a code PR — so it must
        # carry a 'no-pr'-class flag for the pr_linked_or_flagged validator that the
        # workflow-change 'Submit for Review' transition runs (WorkflowService.
        # _is_reviewable checks labels+tags). Without it, the seal-bearing auto-submit
        # below 400s ("Cannot submit for review without a linked PR") and EVERY typed
        # CR ships UNSEALED → pulse_admin_apply_change_request refuses it (PLAT-448).
        "tags": f"no-pr,workflow-change,target:{target_workflow},tier:{_tier}" + (",typed" if _typed else ""),
        # PLS-574 (§3.9): bind the CR to its tracking/authz home project. The
        # id was required + authorized above; this is what makes POST /items/
        # accept the org-scoped item.
        "project": project_id,
    }
    if _typed:
        # The typed envelope IS the apply plan: pulse_admin_apply_change_request
        # executes it 1:1 post-approval (metadata primary; the fenced JSON block
        # in the description is the parse fallback).
        data["metadata"] = {"typed_change_spec": _envelope}

    result = api_request("POST", "/items/", data=data)
    if "error" in result:
        return _fmt_error(result)

    item_id = result.get("id")
    item_key = result.get("item_key") or f"#{item_id}"

    # §3.7 (v1.1): seal the typed spec — sha256(canonical ops) + tier — into an
    # APPEND-ONLY comment. Metadata/description are editable post-approval; the
    # applier verifies against this seal and refuses anything that drifted.
    #
    # DELIVERY (PLAT-218 live-verify finding): the seal rides the auto-submit
    # TRANSITION comment, not a separate POST /comments/. Workflow-change CRs
    # have project=None and the comments API permission check denies even the
    # reporter on project-less items ("You don't have access to comment") — the
    # execute-transition view creates its Comment row directly, bypassing that
    # check, and is still append-only + server-timestamped + pre-approval by
    # construction (submit happens before any approval can exist).
    _submit_comment = f"Proposing workflow change: {title}"
    if _typed:
        _submit_comment += "\n\n" + _tcs.format_seal(_envelope["operations"], _tier)

    # Auto-submit for review — a proposal without submission is useless
    if item_id:
        submit_result = api_request("POST", _item_path(item_id, "execute-transition"), data={
            "to_status": "under_review",
            "comment": _submit_comment,
            "screen_data": {"comment": _submit_comment},
        })
        if "error" not in submit_result:
            assignee = submit_result.get("assignee_email", assignment_group)
            # PLAT-189: PRESERVE GOVERNANCE ROUTING THROUGH AUTO-SUBMIT (Codex L2474 /
            # governance-approver report). The 'Submit for Review' transition's assign_team
            # post-function (migration 0044) force-assigns the CR to admin-ops on
            # proposed->under_review, which CLOBBERS the tier routing set at creation — so a
            # routine CR would never reach Shizuha and the whole governance delegation ships
            # INERT. Re-assert the governance assignment AFTER submit for routine CRs so the
            # delegation actually fires. (Risky CRs are correctly left on admin-ops, matching
            # the post-function — no re-assert.) The assign endpoint resolves governance ->
            # Shizuha (excluding the proposer); if she's unresolvable/unavailable it returns
            # an error and we keep the post-function's admin-ops routing (graceful fallback,
            # never unassigned), reporting admin-ops so the approver text stays honest.
            if assignment_group == "governance":
                reassign = api_request("POST", _item_path(item_id, "assign"), data={
                    "assignment_group": "governance",
                })
                if "error" not in reassign:
                    assignee = reassign.get("assignee_email", assignee)
                else:
                    assignment_group = "admin-ops"
            # Derive the approver from the FINAL routing (assignment_group), NOT _tier:
            # a routine CR proposed BY governance (Shizuha) is routed to admin-ops by the
            # SoD rule above, so reporting "Shizuha will review" off _tier would lie.
            approver = "Shizuha (governance — Chief of Staff)" if assignment_group == "governance" else "Admin Ops (operator)"
            # PLAT-218 §5: nudge legacy free-text proposers toward the typed form.
            _tip = "" if _typed else f"\nTip: submit a typed change_spec ({_tcs.CONTRACT_REF}) for routine-tier auto-routing."
            return f"Change request {item_key} created and submitted for review (tier: {_tier}).\nAssigned to: {assignee}\n{approver} will review and approve or reject the change.{_tip}"
        # PLS-574 R3 (contract §3.9, atomicity/compensation): for a TYPED CR the
        # seal rides the submit comment, so a failed submit leaves an UNSEALED
        # proposal. The create + typed_change_spec write are one POST (atomic);
        # the seal is the third step. Fail CLOSED by COMPENSATING — delete the
        # just-created item so NO usable unsealed workflow-change CR can remain
        # (the applier refusing an unsealed CR is the second line of defense;
        # this removes the artifact entirely, so a partial "item exists, seal
        # missing" state is impossible).
        if _typed:
            _del = api_request("DELETE", _item_path(item_id))
            if "error" not in _del:
                return (f"Error: change request NOT created — sealing failed and the "
                        f"partial proposal {item_key} was rolled back (deleted). No "
                        f"unsealed proposal remains. Re-propose.")
            return (f"Error: proposal {item_key} was created but sealing FAILED and "
                    f"rollback ALSO failed ({_error_detail_text(_del) or 'delete error'}). "
                    f"This typed CR is UNSEALED — the applier will refuse it. Manually "
                    f"delete {item_key}; do NOT approve it.")
        # Legacy free-text CRs carry no seal, so there is nothing to fail closed
        # on — leave the proposed item for a manual transition (unchanged).
        return f"Change request {item_key} created (status: proposed).\nNote: auto-submit for review failed. Please manually transition to under_review."

    return f"Change request created: {item_key}\nWorkflow: workflow-change (status: proposed)"


@mcp.tool(name="pulse_revise_workflow_change")
async def revise_workflow_change(change_request_id: int, change_spec: str) -> str:
    """Atomically replace and re-submit a proposed typed workflow change.

    This is the only supported way to change the operations of an existing
    typed workflow-change request.  It validates through the same contract as
    proposal, writes metadata + rendered description + a fresh pre-approval
    seal in one row transaction, then exposes that sealed state to review.
    Request Revision (or the apply-failure recovery transition) must first
    return the CR to ``proposed``.

    Args:
        change_request_id: Existing workflow-change task id.
        change_spec: Corrected version-1 op-typed JSON envelope.
    """
    from asgiref.sync import sync_to_async

    current = await sync_to_async(api_request)("GET", _item_path(change_request_id))
    if "error" in current:
        return _fmt_error(current)
    if current.get("workflow_name") != "workflow-change":
        return (
            f"Error: task #{change_request_id} is not a workflow-change request "
            f"(workflow={current.get('workflow_name') or 'unknown'})."
        )

    envelope = _tcs.parse_envelope(change_spec)
    if envelope is None:
        return (
            "Error: revision REJECTED — change_spec must be a non-empty "
            f"version-1 typed envelope ({_tcs.CONTRACT_REF})."
        )
    version_error = _tcs.version_error(envelope)
    if version_error:
        return f"Error: revision REJECTED — {version_error}"

    tags = str(current.get("tags") or "")
    target_workflow = next(
        (
            token.split(":", 1)[1].strip()
            for token in tags.split(",")
            if token.strip().startswith("target:")
        ),
        None,
    )
    if not target_workflow:
        return (
            f"Error: revision REJECTED — #{change_request_id} has no target:<workflow> "
            "tag, so header/executor consistency cannot be verified."
        )
    check = _tcs.validate_and_classify(
        envelope,
        fetch_workflow=_fetch_workflow_by_slug,
        target_workflow=target_workflow,
    )
    if check["reject"]:
        return f"Error: revision REJECTED — {check['reject']}"
    tier = check["tier"]

    caller_id, caller_email = _get_my_user_id()
    caller_email = (caller_email or "").strip().lower()
    allowed_editors = {
        str(current.get("created_by_email") or "").strip().lower(),
        str(current.get("assignee_email") or "").strip().lower(),
    }
    allowed_editors.discard("")

    def _is_exact_org_admin_ops_backup(item, email):
        # PLAT-6037: the backup is a DB-backed exact-org team authority, not a
        # hard-coded identity and not generic organization membership.
        if not email or getattr(item, "project_id", None) is None:
            return False
        try:
            from tasks.services.workflow_service import WorkflowService
            return WorkflowService._is_team_member(
                email, "admin-ops", item=item,
            )
        except Exception:
            return False

    # The unlocked snapshot is only a usability preflight. Admin Ops backup
    # authority is re-evaluated from the locked Item below before any write.
    if not caller_email:
        return "Error: revision REJECTED — caller identity is unavailable."

    # Serialize this write with workflow-change transitions.  The transition
    # engine takes the same row lock for this workflow (PLS-559), preventing a
    # concurrent approval from racing a proposal revision.
    #
    # PLAT-6050: FastMCP executes sync tools INLINE in the async event loop
    # (no worker thread), so direct Django ORM here raised SynchronousOnlyOperation
    # ("You cannot call this from an async context"). Run the ORM write in a
    # worker thread via sync_to_async so it can execute from the live async MCP
    # serving context.
    from django.db import transaction
    from tasks.models import Comment, Item

    seal = _tcs.format_seal(envelope["operations"], tier)
    seal_comment = (
        "Authoritative typed-spec revision committed before review exposure.\n\n"
        + seal
    )

    def _commit_revision() -> str | None:
        """Persist the revised envelope + seal atomically; None on success."""
        try:
            with transaction.atomic():
                item = Item.objects.select_for_update().get(pk=current.get("id"))
                if item.workflow_name != "workflow-change":
                    return "Error: revision REJECTED — task workflow changed during revision."
                if (item.status or "").strip().lower() != "proposed":
                    return (
                        f"Error: revision REJECTED — #{change_request_id} is "
                        f"status={item.status or item.workflow_status}; first use Request Revision "
                        "or Revise After Apply Failure to return it to proposed."
                    )
                item_tags = str(item.tags or "")
                locked_target = next(
                    (
                        token.split(":", 1)[1].strip()
                        for token in item_tags.split(",")
                        if token.strip().startswith("target:")
                    ),
                    None,
                )
                if locked_target != target_workflow:
                    return "Error: revision REJECTED — target workflow changed during revision."

                # The unlocked API snapshot is only a preflight.  Assignment can
                # change before this transaction acquires the row lock, so derive
                # authorization again from the locked Item immediately before the
                # authoritative payload is mutated.
                locked_allowed_editors = {
                    str(item.created_by_email or "").strip().lower(),
                    str(item.assignee_email or "").strip().lower(),
                }
                locked_allowed_editors.discard("")
                is_admin_ops_backup = _is_exact_org_admin_ops_backup(
                    item, caller_email,
                )
                if (
                    not caller_email
                    or (
                        caller_email not in locked_allowed_editors
                        and not is_admin_ops_backup
                    )
                ):
                    return (
                        "Error: revision REJECTED — only the workflow-change proposer, "
                        "current assignee, or exact-org Admin Ops backup may revise "
                        "this request."
                    )
                metadata = dict(item.metadata or {})
                metadata["typed_change_spec"] = envelope
                item.metadata = metadata
                item.description = project_typed_envelope(item.description, envelope)
                tag_tokens = [token.strip() for token in item_tags.split(",") if token.strip()]
                tag_tokens = [token for token in tag_tokens if not token.startswith("tier:")]
                tag_tokens.append(f"tier:{tier}")
                item.tags = ",".join(tag_tokens)
                item._activity_actor = (caller_id or 0, caller_email)
                item.save(update_fields=["metadata", "description", "tags", "updated_at"])
                # The seal is part of the same durable transaction as the revised
                # executable envelope and its rendered projection.  The subsequent
                # transition endpoint commits status before its optional comment,
                # so carrying the seal on that comment could expose under_review
                # without a matching pre-approval seal when comment persistence or
                # response delivery fails.
                effective_seal_comment = seal_comment
                if is_admin_ops_backup and caller_email not in locked_allowed_editors:
                    effective_seal_comment = (
                        "Delegated Admin Ops execution (PLAT-6037): exact-org backup "
                        f"{caller_email} performed the atomic revision/re-seal.\n\n"
                        + seal_comment
                    )
                Comment.objects.create(
                    item=item,
                    author_id=caller_id or 0,
                    author_email=caller_email,
                    content=effective_seal_comment,
                )
        except Item.DoesNotExist:
            return f"Error: change request #{change_request_id} not found."
        except ValueError as exc:
            return f"Error: revision REJECTED — {exc}."
        return None

    orm_error = await sync_to_async(_commit_revision)()
    if orm_error:
        return orm_error

    submit_comment = (
        "Re-submit after atomic typed-spec revision. The fresh typed-spec seal "
        "was committed with the authoritative envelope before review exposure."
    )
    submitted = await sync_to_async(api_request)(
        "POST",
        _item_path(change_request_id, "execute-transition"),
        data={
            "to_status": "under_review",
            "comment": submit_comment,
            "screen_data": {"comment": submit_comment},
        },
    )
    if "error" in submitted:
        durable = await sync_to_async(api_request)("GET", _item_path(change_request_id))
        durable_status = (
            str(durable.get("status") or durable.get("workflow_status") or "")
            .strip()
            .lower()
            .replace(" ", "_")
            if "error" not in durable
            else ""
        )
        if durable_status and durable_status != "proposed":
            return (
                f"Revision and fresh seal were stored atomically on #{change_request_id}. "
                f"Re-submit response failed after the durable state reached "
                f"{durable_status}: {_error_detail_text(submitted)}. "
                "Do not retry the revision while it is outside proposed; the sealed "
                "authoritative payload is safe for the current review state."
            )
        if durable_status == "proposed":
            return (
                f"Revision and fresh seal were stored atomically on #{change_request_id}, "
                f"but re-submit failed before a durable status change: "
                f"{_error_detail_text(submitted)}. The CR remains proposed and unapproved; "
                "retry this revision call to submit the already sealed payload."
            )
        return (
            f"Revision and fresh seal were stored atomically on #{change_request_id}, "
            f"but re-submit returned an error and the durable status could not be "
            f"confirmed: {_error_detail_text(submitted)}. Fetch the CR before retrying; "
            "the authoritative payload itself remains sealed."
        )

    # Submit for Review's historical post-function sends every CR to admin-ops.
    # Re-assert the tier-derived governance lane exactly as proposal does.
    assignment_group = "governance" if tier == "routine" else "admin-ops"
    assignee = submitted.get("assignee_email", assignment_group)
    if assignment_group == "governance":
        reassigned = await sync_to_async(api_request)(
            "POST",
            _item_path(change_request_id, "assign"),
            data={"assignment_group": assignment_group},
        )
        if "error" not in reassigned:
            assignee = reassigned.get("assignee_email", assignee)
        else:
            assignment_group = "admin-ops"

    return (
        f"Revised and re-submitted change request #{change_request_id} "
        f"(tier: {tier}, sha256={_tcs.ops_digest(envelope['operations'])}).\n"
        f"Authoritative metadata and rendered description now contain the same operations.\n"
        f"Assigned to: {assignee or assignment_group}"
    )


@mcp.tool(name="pulse_admin_migrate_task_workflow")
def admin_migrate_task_workflow(
    target_workflow: str,
    task_keys: Optional[list] = None,
    task_key: str = "",
    dry_run: bool = True,
) -> str:
    """Migrate one or more tasks to a DIFFERENT workflow (PLS-54).

    Use this to fix mis-filed tasks — e.g. a code task stuck on `simple` that
    can't route to review → move it to `autonomous-dev`. Each task's status is
    remapped to the target workflow's status of the SAME category (e.g.
    in_progress → the target's in-progress status), never dropped into an
    invalid state.

    dry_run is DEFAULT True — it returns the planned old→new (workflow, status)
    for every task WITHOUT changing anything. ALWAYS preview a bulk sweep first.
    Applying (dry_run=False) is admin/CoS-only (PLAT-218 destructive-class) and
    records a reversible pre-migration record on each task (so a migration can
    be undone). Pass an EXPLICIT list of keys — there is no "migrate all" mode.

    Args:
        target_workflow: destination workflow name or slug (e.g. "autonomous-dev").
        task_keys: explicit list of item keys, e.g. ["SCLI-63", "PLAT-9"].
        task_key: convenience single key (alternative to task_keys).
        dry_run: preview only (default True); set False to apply.
    """
    keys = list(task_keys or [])
    if task_key and task_key not in keys:
        keys.append(task_key)
    if not keys:
        return "Error: provide task_keys (a list) or task_key."
    if not str(target_workflow).strip():
        return "Error: target_workflow is required."
    canonical_keys = [_resolve_item_pk(key) for key in keys]
    result = api_request("POST", "/items/migrate-workflow/", data={
        "task_keys": canonical_keys, "target_workflow": target_workflow, "dry_run": dry_run,
    })
    if "error" in result:
        return _fmt_error(result)
    mode = "DRY-RUN (no changes made)" if result.get("dry_run") else "APPLIED"
    out = (f"Workflow migration [{mode}] → {result.get('target_workflow')} "
           f"({result.get('count', 0)} task(s)):\n\n")
    for r in result.get("results", []):
        if r.get("error"):
            out += f"- {r.get('task_key')}: ERROR — {r['error']}\n"
            continue
        frm, to = r.get("from", {}), r.get("to", {})
        out += (f"- {r.get('task_key')}: {frm.get('workflow_name')} [{frm.get('status')}] "
                f"→ {to.get('workflow_name')} [{to.get('status')}]"
                f"{' ✓ applied' if r.get('applied') else ''}\n")
    if result.get("dry_run"):
        out += "\nPreview only. Re-run with dry_run=false to apply (admin/CoS only)."
    return out


@mcp.tool(name="pulse_admin_create_status")
def admin_create_status(
    name: str,
    slug: str,
    category: str,
    change_request_id: int,
    color: str = "#6b7280",
    description: str = "",
) -> str:
    """
    Create a new global status. Requires an approved change request.

    Args:
        name: Display name (e.g., "Awaiting Merge")
        slug: Machine-readable slug (e.g., "awaiting_merge")
        category: Status category: "todo", "in_progress", or "done"
        change_request_id: ID of the approved workflow change request task
        color: Hex color code
        description: Description of the status
    """
    # Verify change request is approved
    check = _verify_change_request(change_request_id)
    if check:
        return check

    result = api_request("POST", "/statuses/", data={
        "name": name, "slug": slug, "category": category,
        "color": color, "description": description,
    })
    if "error" in result:
        return _fmt_error(result)
    return f"Status created: {name} (slug={slug}, category={category})"


@mcp.tool(name="pulse_admin_add_workflow_status")
def admin_add_workflow_status(
    workflow_id: str,
    status_slug: str,
    position: int,
    change_request_id: int,
) -> str:
    """
    Add an existing status to a workflow. Requires an approved change request.

    Args:
        workflow_id: Workflow UUID
        status_slug: Slug of the status to add
        position: Position in the workflow (0 = first)
        change_request_id: ID of the approved workflow change request task
    """
    check = _verify_change_request(change_request_id)
    if check:
        return check

    result = api_request("POST", f"/workflows/{workflow_id}/add_status/", data={
        "status_slug": status_slug, "position": position,
    })
    if "error" in result:
        return _fmt_error(result)
    return f"Status {status_slug} added to workflow at position {position}"


def _coerce_json_list(field, value):
    """WRITE boundary: normalize a transition list-field arg to a native list
    (PLS-224, per the aoi contract ruling).

    The canonical stored representation is a native JSON array — a raw JSON
    string is NEVER a valid stored value. So this accepts a native list (MCP
    passed a JSON array) or a JSON-array string, parses ONCE, and REQUIRES the
    result to be a list; a double-encoded string (parses to a str) or a scalar
    is REJECTED (not silently normalized). Making the field type
    ``Union[str, list]`` also lets a native list pass the tool boundary, fixing
    "Input should be a valid string". Returns ``(list, None)`` or
    ``(None, error_message)``. Legacy raw-string rows already in the DB are
    handled defensively at the READ boundary by ``_safe_json_list``.
    """
    if isinstance(value, list):
        return value, None
    if not isinstance(value, str):
        return None, f"Error: {field} must be a JSON array (list), got {type(value).__name__}"
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None, f"Error: {field} must be a valid JSON array"
    if isinstance(parsed, list):
        return parsed, None
    return None, (f"Error: {field} must be a JSON array (list), not a "
                  f"{type(parsed).__name__} (double-encoded strings and scalars are rejected)")


def _safe_json_list(value, item_type="dict"):
    """READ boundary: defensively coerce a stored transition list-field to a
    real list of typed ITEMS for RENDER/COUNT sites (PLS-224, PLS-263, per the
    aoi contract ruling).

    Legacy/corrupted rows (or writes that predated the write-side normalizer)
    may hold a raw JSON STRING in the JSONField. ``len()``-ing or iterating that
    counts/walks CHARACTERS — the "762 knowledge article(s)" symptom, and an
    ``AttributeError`` when a render loop calls ``.get()`` on a character.

    PLS-263: the ITEMS are coerced too, not just the outer container. A stored
    list whose elements are JSON-encoded article strings (or bare text) — the
    malformed ``knowledge_articles`` shape on the security-finding transition
    PLAT-1610 reached — otherwise flows into a render loop's ``item.get(...)``
    and raises ``'str' object has no attribute 'get'``. With the default
    ``item_type='dict'`` (every current render/count site consumes dict items)
    string items are parsed once and non-dict items are dropped, so a single
    malformed article can never crash transition listing or execution. This
    ALWAYS returns a list and NEVER raises: read paths degrade gracefully, they
    do not reject (rejection is the write boundary's job). A double-encoded
    legacy string is unwrapped one extra level.
    """
    # Outer coercion: always resolve to a Python list of raw items.
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, str):  # double-encoded legacy row
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                return []
        items = parsed if isinstance(parsed, list) else []
    else:
        return []

    # PLS-263 item coercion: render/count sites consume typed items, so a
    # string element must be parsed (or dropped) here — never handed to a
    # caller that will `.get()` it.
    if item_type == "dict":
        normalized = []
        for item in items:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(item, dict):
                normalized.append(item)
        return normalized
    if item_type == "str":
        return [item for item in items if isinstance(item, str)]
    return items


@mcp.tool(name="pulse_admin_create_transition")
def admin_create_transition(
    workflow_id: str,
    from_status: str,
    to_status: str,
    name: str,
    change_request_id: int,
    description: str = "",
    post_functions: Optional[Union[str, list]] = None,
    knowledge_articles: Optional[Union[str, list]] = None,
    screen_fields: Optional[Union[str, list]] = None,
    allowed_roles: Optional[Union[str, list]] = None,
    button_style: str = "default",
    position: int = 0,
) -> str:
    """
    Create a new workflow transition. Requires an approved change request.

    Args:
        workflow_id: Workflow UUID
        from_status: Source status slug
        to_status: Target status slug
        name: Transition name (e.g., "Submit for Review")
        change_request_id: ID of the approved workflow change request task
        description: Instructions for this transition
        post_functions: JSON array of post-functions (e.g., '[{"type":"assign_team","value":"review"}]')
        knowledge_articles: JSON array of knowledge articles
        screen_fields: JSON array of screen fields
        allowed_roles: JSON array of allowed roles (e.g., '["admin","ceo"]')
        button_style: Button style: "default", "primary", "success", "warning", "danger"
        position: Display order
    """
    check = _verify_change_request(change_request_id)
    if check:
        return check

    data = {
        "workflow": workflow_id,
        "from_status": from_status,
        "to_status": to_status,
        "name": name,
        "description": description,
        "button_style": button_style,
        "position": position,
    }

    # Parse JSON list fields (PLS-224: accept native lists + JSON-array strings,
    # reject anything that does not resolve to a list).
    for field, value in [("post_functions", post_functions), ("knowledge_articles", knowledge_articles),
                          ("screen_fields", screen_fields), ("allowed_roles", allowed_roles)]:
        if value:
            coerced, err = _coerce_json_list(field, value)
            if err:
                return err
            data[field] = coerced

    result = api_request("POST", f"/workflows/{workflow_id}/add_transition/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Transition created: {from_status} → {to_status} [{name}]"


@mcp.tool(name="pulse_admin_update_transition")
def admin_update_transition(
    transition_id: str,
    change_request_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    validators: Optional[Union[str, List[dict]]] = None,
    post_functions: Optional[Union[str, List[dict]]] = None,
    knowledge_articles: Optional[Union[str, List[dict]]] = None,
    screen_fields: Optional[Union[str, List[dict]]] = None,
    allowed_roles: Optional[Union[str, List[str]]] = None,
    button_style: Optional[str] = None,
) -> str:
    """
    Update an existing workflow transition. Requires an approved change request.

    Args:
        transition_id: Transition UUID
        change_request_id: ID of the approved workflow change request task
        name: New transition name
        description: New description/instructions
        validators: JSON array of validators (string or native list)
        post_functions: JSON array of post-functions (string or native list)
        knowledge_articles: JSON array of knowledge articles (string or native list)
        screen_fields: JSON array of screen fields (string or native list)
        allowed_roles: JSON array of allowed role strings (string or native list)
        button_style: Button style
    """
    check = _verify_change_request(change_request_id)
    if check:
        return check

    data = {}
    if name is not None: data["name"] = name
    if description is not None: data["description"] = description
    if button_style is not None: data["button_style"] = button_style

    list_fields = [
        ("validators", validators, "dict"),
        ("post_functions", post_functions, "dict"),
        ("knowledge_articles", knowledge_articles, "dict"),
        ("screen_fields", screen_fields, "dict"),
        ("allowed_roles", allowed_roles, "str"),
    ]
    for field, value, item_type in list_fields:
        if value is not None:
            coerced, err = _coerce_json_list(field, value)
            if err:
                return err
            data[field] = coerced

    if not data:
        return "No fields to update"

    result = api_request("PATCH", f"/transitions/{transition_id}/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Transition {transition_id} updated"


@mcp.tool(name="pulse_admin_update_workflow")
def admin_update_workflow(
    workflow_id: str,
    change_request_id: int,
    description: Optional[str] = None,
    knowledge_base: Optional[Union[str, list]] = None,
) -> str:
    """
    Update a workflow's metadata (description, knowledge base). Requires an approved change request.

    Args:
        workflow_id: Workflow UUID
        change_request_id: ID of the approved workflow change request task
        description: New workflow description
        knowledge_base: JSON array of knowledge base articles
    """
    check = _verify_change_request(change_request_id)
    if check:
        return check

    data = {}
    if description is not None: data["description"] = description
    if knowledge_base is not None:
        coerced, err = _coerce_json_list("knowledge_base", knowledge_base)
        if err:
            return err
        data["knowledge_base"] = coerced

    if not data:
        return "No fields to update"

    result = api_request("PATCH", f"/workflows/{workflow_id}/", data=data)
    if "error" in result:
        return _fmt_error(result)
    return f"Workflow {workflow_id} updated"


def _typed_api(method: str, path: str, payload: Optional[dict]) -> dict:
    """api_request adapter for the typed-change-spec module's injected I/O."""
    return api_request(method, path, data=payload)


def _fetch_workflow_by_slug(slug: str) -> Optional[dict]:
    """Resolve a workflow slug to its full detail (statuses + transitions),
    for typed-change-spec target validation + the 1:1 applier (PLAT-218).
    Drains ALL pages — /workflows/ is DRF-paginated (PAGE_SIZE=10); a
    page-1-only read false-rejects later-page workflows (v1.1 P2)."""
    if not slug:
        return None
    workflows = _tcs.get_all_pages(_typed_api, "/workflows/")
    if not isinstance(workflows, list):
        return None
    for wf in workflows:
        if str(wf.get("slug", "")).strip().lower() == slug.strip().lower():
            detail = api_request("GET", f"/workflows/{wf.get('id')}/")
            return None if "error" in detail else detail
    return None


@mcp.tool(name="pulse_admin_apply_change_request")
def admin_apply_change_request(change_request_id: int) -> str:
    """
    Apply an APPROVED op-typed change request 1:1 (PLAT-218 contract).

    Executes the CR's typed operations sequentially via the mapped admin
    endpoints — zero prose interpretation; what was classified is exactly what
    executes. Stops at the first failure (no partial-silent-success).
    Destructive/data ops (remove_*, migrate, backfill, change_permission) are
    manual-only and refuse to auto-apply. Legacy free-text CRs are not
    appliable by this tool — use the individual pulse_admin_* tools.

    Args:
        change_request_id: ID of the APPROVED workflow change request task
    """
    check = _verify_change_request(change_request_id)
    if check:
        return check

    item = api_request("GET", _item_path(change_request_id))
    if "error" in item:
        return _fmt_error(item)

    env = (item.get("metadata") or {}).get("typed_change_spec")
    if not (isinstance(env, dict) and isinstance(env.get("operations"), list) and env["operations"]):
        return (
            "Error: REFUSED — #%s carries no authoritative metadata.typed_change_spec. "
            "The rendered description is a non-authoritative projection and is never "
            "parsed for execution. Re-propose or revise through the typed workflow-change "
            "primitive (%s)." % (change_request_id, _tcs.CONTRACT_REF)
        )
    # PLAT-253 #3: the metadata-stored envelope is never re-parsed, so version
    # strictness must be enforced here too — refuse to apply any contract
    # version this applier doesn't implement.
    _verr = _tcs.version_error(env)
    if _verr:
        return f"Error: REFUSED — {_verr}"

    # §3.7 anti-TOCTOU (contract v1.1): metadata/description are EDITABLE post-
    # approval, so they cannot be trusted as-is. The proposal flow sealed
    # sha256(canonical ops) + tier into an append-only comment; recompute over
    # what we just loaded and refuse on any mismatch — then RE-classify and
    # require the tier to still match the sealed tier (an edited spec must
    # never execute under a stale approval).
    # §3.7b temporal binding: anchor on the server-recorded approval instant —
    # only seals created BEFORE it are trusted (comment authorship can't
    # discriminate forgeries; append-only + server timestamps can).
    activities = _tcs.get_all_pages(_typed_api, _item_path(change_request_id, "activity"))
    approved_at = _tcs.latest_approval_time(activities if isinstance(activities, list) else [])
    if not approved_at:
        return (f"Error: REFUSED — no status_changed→approved activity found on "
                f"#{change_request_id}; the §3.7b seal window can't be anchored. "
                f"(An 'applying' CR must still carry its approval record.)")
    comments = _tcs.get_all_pages(_typed_api, f"/comments/?item={item.get('id') or change_request_id}")
    seal, forged = _tcs.select_trusted_seal(comments if isinstance(comments, list) else [], approved_at)
    tamper_note = ""
    if forged:
        tamper_note = (f"\n⚠ TAMPER SIGNAL: {len(forged)} {_tcs.SEAL_MARKER} comment(s) posted "
                       f"AFTER the approval ({approved_at}) at {forged} — ignored (§3.7b). "
                       f"Post-approval seals are never trusted; investigate who posted them.")
        logger.warning(
            f"PLAT-218 §3.7b: post-approval seal(s) on CR #{change_request_id} at {forged} "
            f"(approved_at={approved_at}) — possible tamper attempt, ignored")
    if not seal:
        return (f"Error: REFUSED — no pre-approval {_tcs.SEAL_MARKER} comment found on "
                f"#{change_request_id} (approval anchor {approved_at}). Typed CRs are sealed at "
                f"proposal time (§3.7); without a trusted seal the spec's approval integrity "
                f"can't be verified. Re-propose the CR typed.{tamper_note}")
    current_digest = _tcs.ops_digest(env["operations"])
    if current_digest != seal.get("sha256"):
        return (f"Error: REFUSED — spec changed after approval (§3.7). "
                f"sealed sha256={seal.get('sha256')} != current sha256={current_digest}. "
                f"The operations stored on #{change_request_id} are NOT the ones that were "
                f"approved. Re-propose and re-approve.{tamper_note}")
    _tags = str(item.get("tags") or "")
    _target_wf = next((t.split(":", 1)[1] for t in _tags.split(",") if t.strip().startswith("target:")), None)
    recheck = _tcs.validate_and_classify(env, fetch_workflow=_fetch_workflow_by_slug, target_workflow=_target_wf)
    if recheck["reject"]:
        return f"Error: REFUSED — spec no longer validates at apply time (§3.7): {recheck['reject']}"
    if recheck["tier"] != seal.get("tier"):
        return (f"Error: REFUSED — re-classification at apply time yields tier="
                f"'{recheck['tier']}' but the sealed/approved tier is '{seal.get('tier')}' "
                f"(§3.7). Re-propose and re-approve.")

    results = []
    for i, op in enumerate(env["operations"]):
        ok, msg = _tcs.apply_operation(
            op,
            lambda method, path, payload: api_request(method, path, data=payload),
            _fetch_workflow_by_slug,
        )
        results.append(f"[{i}] {op.get('op')}: {'OK' if ok else 'FAILED'} — {msg}")
        if not ok:
            results.append(
                f"STOPPED at operation[{i}] — remaining ops not executed (no partial-silent-success). "
                f"Fix the cause and re-run, or apply the rest via the individual admin tools.")
            break
    else:
        results.append(f"All {len(env['operations'])} operation(s) applied.")
    return "\n".join(results) + tamper_note


def _verify_change_request(change_request_id: int) -> Optional[str]:
    """Verify that a change request task exists and is in approved/applying status."""
    result = api_request("GET", _item_path(change_request_id))
    if "error" in result:
        return f"Change request #{change_request_id} not found"

    wf = result.get("workflow_name", "")
    status = result.get("status", "")
    wf_status = result.get("workflow_status", status)

    if wf != "workflow-change":
        return f"Task #{change_request_id} is not a workflow change request (workflow={wf})"

    # Gate on the canonical status SLUG (`status`, stable + lowercase), NOT the
    # mutable workflow_status DISPLAY NAME (models.py sets it to s.name, e.g.
    # "Applying"/"Approved"). Trusting the label both (a) caused the #1550 casing
    # deadlock ("Applying" != "applying") and (b) is unsafe — display names are
    # mutable/non-unique, so a CR could slip through after a transition into a
    # label-alike state (Codex P2). Fall back to the name only if the slug is
    # absent; lowercase defensively either way.
    gate = (status or wf_status or "").strip().lower()
    if gate not in ("approved", "applying"):
        return f"Change request #{change_request_id} is not approved (status={wf_status}). It must be approved by the CEO before changes can be applied."

    return None  # All good


# =============================================================================
# ISSUE TYPES
# =============================================================================

@mcp.tool(name="pulse_list_issue_types")
def list_issue_types() -> str:
    """List all available issue types (Epic, Story, Bug, Task, Sub-task, etc.)."""
    result = api_request("GET", "/issue-types/")
    if "error" in result:
        return _fmt_error(result)
    types = (result if isinstance(result, list) else result.get("results", []))
    if not types:
        return "No issue types found."
    output = f"{len(types)} issue type(s):\n\n"
    for t in types:
        subtask = " [subtask]" if t.get("is_subtask") else ""
        output += f"- {t.get('name')} (slug: {t.get('slug')}, level: {t.get('hierarchy_level', 'N/A')}{subtask})\n"
        output += f"  ID: {t.get('id')}\n"
    return output


# =============================================================================
# CUSTOM FIELDS
# =============================================================================

@mcp.tool(name="pulse_list_custom_fields")
def list_custom_fields() -> str:
    """List all custom fields and their types."""
    result = api_request("GET", "/custom-fields/")
    if "error" in result:
        return _fmt_error(result)
    fields = (result if isinstance(result, list) else result.get("results", []))
    if not fields:
        return "No custom fields defined."
    output = f"{len(fields)} custom field(s):\n\n"
    for f in fields:
        req = " [required]" if f.get("is_required") else ""
        output += f"- {f.get('name')} (type: {f.get('field_type')}{req}) — ID: {f.get('id')}\n"
    return output


@mcp.tool(name="pulse_set_custom_field_value")
def set_custom_field_value(item_id: str, custom_field_id: str, value: str) -> str:
    """
    Set a custom field value on an item.

    Args:
        item_id: Item/task ID
        custom_field_id: Custom field ID
        value: Value to set (string, will be stored in appropriate format)
    """
    result = api_request("POST", "/custom-field-values/", data={
        "item": item_id,
        "custom_field": custom_field_id,
        "value_text": value,
    })
    if "error" in result:
        return _fmt_error(result)
    return f"Custom field value set (ID: {result.get('id')})"


@mcp.tool(name="pulse_get_custom_field_values")
def get_custom_field_values(item_id: str) -> str:
    """
    Get all custom field values for an item.

    Args:
        item_id: Item/task ID
    """
    result = api_request("GET", "/custom-field-values/", params={"item_id": item_id})
    if "error" in result:
        return _fmt_error(result)
    values = (result if isinstance(result, list) else result.get("results", []))
    if not values:
        return "No custom field values set."
    output = f"{len(values)} custom field value(s):\n\n"
    for v in values:
        output += f"- {v.get('custom_field_name', v.get('custom_field', 'unknown'))}: {v.get('value_text', v.get('value_json', 'N/A'))}\n"
    return output


# =============================================================================
# BOARDS
# =============================================================================

@mcp.tool(name="pulse_list_boards")
def list_boards(project_id: Optional[int] = None) -> str:
    """
    List boards (scrum/kanban).

    Args:
        project_id: Filter by project
    """
    params = {}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/boards/", params=params)
    if "error" in result:
        return _fmt_error(result)
    boards = (result if isinstance(result, list) else result.get("results", []))
    if not boards:
        return "No boards found."
    output = f"{len(boards)} board(s):\n\n"
    for b in boards:
        output += f"- {b.get('name')} ({b.get('board_type', 'kanban')}) — ID: {b.get('id')}\n"
        if b.get("swimlane_by"): output += f"  Swimlane: {b['swimlane_by']}\n"
    return output


# =============================================================================
# DASHBOARDS
# =============================================================================

@mcp.tool(name="pulse_list_dashboards")
def list_dashboards() -> str:
    """List all dashboards."""
    result = api_request("GET", "/dashboards/")
    if "error" in result:
        return _fmt_error(result)
    dashboards = (result if isinstance(result, list) else result.get("results", []))
    if not dashboards:
        return "No dashboards found."
    output = f"{len(dashboards)} dashboard(s):\n\n"
    for d in dashboards:
        shared = " [shared]" if d.get("is_shared") else ""
        output += f"- {d.get('name')}{shared} — ID: {d.get('id')}\n"
    return output


# =============================================================================
# AUTOMATION RULES
# =============================================================================

@mcp.tool(name="pulse_list_automation_rules")
def list_automation_rules(project_id: Optional[int] = None) -> str:
    """
    List automation rules.

    Args:
        project_id: Filter by project
    """
    params = {}
    if project_id: params["project"] = project_id
    result = api_request("GET", "/automation-rules/", params=params)
    if "error" in result:
        return _fmt_error(result)
    rules = (result if isinstance(result, list) else result.get("results", []))
    if not rules:
        return "No automation rules found."
    output = f"{len(rules)} rule(s):\n\n"
    for r in rules:
        status = "enabled" if r.get("is_enabled") else "disabled"
        output += f"- {r.get('name')} ({r.get('trigger_type')}) [{status}] — ID: {r.get('id')}\n"
        output += f"  Executions: {r.get('execution_count', 0)}\n"
    return output


@mcp.tool(name="pulse_toggle_automation_rule")
def toggle_automation_rule(rule_id: str) -> str:
    """
    Toggle an automation rule on/off.

    Args:
        rule_id: Automation rule ID
    """
    result = api_request("POST", f"/automation-rules/{rule_id}/toggle/")
    if "error" in result:
        return _fmt_error(result)
    status = "enabled" if result.get("is_enabled") else "disabled"
    return f"Rule '{result.get('name')}' is now {status}."


@mcp.tool(name="pulse_get_automation_logs")
def get_automation_logs(rule_id: str, limit: int = 10) -> str:
    """
    Get execution logs for an automation rule.

    Args:
        rule_id: Automation rule ID
        limit: Max entries
    """
    result = api_request("GET", f"/automation-rules/{rule_id}/logs/", params={"page_size": limit})
    if "error" in result:
        return _fmt_error(result)
    logs = (result if isinstance(result, list) else result.get("results", []))
    if not logs:
        return "No execution logs."
    output = f"{len(logs)} log(s):\n\n"
    for log in logs:
        output += f"- {log.get('status', 'unknown')} at {log.get('triggered_at', 'N/A')} — {log.get('items_affected', 0)} items affected\n"
        if log.get("error_message"): output += f"  Error: {log['error_message']}\n"
    return output


# =============================================================================
# SLA
# =============================================================================

@mcp.tool(name="pulse_list_sla_definitions")
def list_sla_definitions() -> str:
    """List all SLA definitions with target hours and conditions."""
    result = api_request("GET", "/sla-definitions/")
    if "error" in result:
        return _fmt_error(result)
    slas = (result if isinstance(result, list) else result.get("results", []))
    if not slas:
        return "No SLA definitions found."
    output = f"{len(slas)} SLA(s):\n\n"
    for s in slas:
        output += f"- {s.get('name')} — Target: {s.get('target_hours')}h, Warning: {s.get('warning_threshold_percent', 80)}%\n"
        output += f"  ID: {s.get('id')}\n"
    return output


@mcp.tool(name="pulse_list_sla_instances")
def list_sla_instances(item_id: Optional[str] = None, status: Optional[str] = None, limit: int = 20) -> str:
    """
    List SLA tracking instances. Shows whether items are meeting their SLAs.

    Args:
        item_id: Filter by item ID
        status: Filter by status (active, paused, met, breached)
        limit: Max results
    """
    params = {"page_size": limit}
    if item_id: params["item"] = item_id
    if status: params["status"] = status
    result = api_request("GET", "/sla-instances/", params=params)
    if "error" in result:
        return _fmt_error(result)
    instances = (result if isinstance(result, list) else result.get("results", []))
    if not instances:
        return "No SLA instances found."
    output = f"{len(instances)} SLA instance(s):\n\n"
    for i in instances:
        breached = " [BREACHED]" if i.get("is_breached") else ""
        output += f"- {i.get('sla_name', 'N/A')} for item {i.get('item_key', i.get('item', 'N/A'))} — {i.get('status', 'active')}{breached}\n"
        if i.get("elapsed_time"): output += f"  Elapsed: {i['elapsed_time']}\n"
    return output


# =============================================================================
# BULK OPERATIONS
# =============================================================================

@mcp.tool(name="pulse_bulk_operation")
def bulk_operation(
    operation: str,
    item_ids: str,
    status: Optional[str] = None,
    assignee_email: Optional[str] = None,
    priority: Optional[str] = None,
    sprint_id: Optional[str] = None,
    # PLS-1248: FastMCP/Pydantic eagerly decodes JSON-looking arguments (same
    # coercion PLS-712 documented for labels), so a string-only annotation
    # rejects the already-parsed object before this tool runs — the documented
    # `no-pr` label declaration path was unusable from PATCH-less seats.
    # Accept both shapes and normalize below.
    fields: Optional[Union[str, dict]] = None,
) -> str:
    """
    Perform bulk operations on multiple items at once.

    Args:
        operation: Operation type (transition, assign, edit, delete, move_sprint)
        item_ids: Comma-separated list of item IDs
        status: Target status (for transition)
        assignee_email: Target assignee (for assign)
        priority: Target priority (for edit)
        sprint_id: Target sprint (for move_sprint)
        fields: Fields to update (for edit), e.g. {"labels": ["frontend", "bug"]}
                or {"priority": "high"}. Pass an object; a JSON string is also
                accepted for backward compatibility.
    """
    # PLAT-933: resolve task keys ('PLAT-921') -> numeric pks like every other write
    # tool, and NEST the operation parameters under `data` using the field names the
    # /items/bulk/ endpoint actually reads (to_status, assignee_email, priority,
    # sprint_id, edit-fields). The previous payload sent these at the TOP LEVEL and
    # used `status` instead of `to_status`, so assign/transition/edit/move_sprint
    # silently no-op'd or 400'd — only `delete` ever worked through this tool.
    ids = [_resolve_item_pk(i.strip()) for i in item_ids.split(",") if i.strip()]
    op_data = {}
    if status: op_data["to_status"] = status
    if assignee_email: op_data["assignee_email"] = assignee_email
    if priority: op_data["priority"] = priority
    if sprint_id: op_data["sprint_id"] = sprint_id
    if fields:
        # PLS-1248: normalize the two accepted shapes — a pre-parsed object
        # (what the transport delivers after its eager JSON decode) or a JSON
        # string (older callers). Anything else fails closed with a clear
        # message instead of a pydantic coercion error.
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except json.JSONDecodeError:
                return "Error: 'fields' must be valid JSON."
        if not isinstance(fields, dict):
            return "Error: 'fields' must be a JSON object of fields to update (e.g. {\"labels\": [\"frontend\"]})."
        op_data.update(fields)
    payload = {"operation": operation, "item_ids": ids, "data": op_data}

    result = api_request("POST", "/items/bulk/", data=payload)
    if "error" in result:
        return _fmt_error(result)
    affected = result.get("updated", result.get("deleted", result.get("affected", len(ids))))
    return f"Bulk {operation} completed: {affected} items affected."


# =============================================================================
# NOTIFICATIONS
# =============================================================================

@mcp.tool(name="pulse_list_notifications")
def list_notifications(unread_only: bool = False, limit: int = 20) -> str:
    """
    List in-app notifications.

    Args:
        unread_only: Only show unread notifications
        limit: Max results
    """
    params = {"page_size": limit}
    if unread_only: params["is_read"] = "false"
    result = api_request("GET", "/notifications/", params=params)
    if "error" in result:
        return _fmt_error(result)
    notifs = result.get("results", [])
    if not notifs:
        return "No notifications."
    output = f"{len(notifs)} notification(s):\n\n"
    for n in notifs:
        read = "" if n.get("is_read") else " [UNREAD]"
        task = f" (task {n['item_key']})" if n.get("item_key") else ""
        # Surface the notification UUID — it is the id pulse_mark_notification_read
        # needs (PLAT-2691: agents could not discover it before).
        output += f"- [{n.get('id')}] {n.get('message', 'N/A')}{task}{read}\n"
        output += f"  {n.get('created_at', '')}\n"
    return output


@mcp.tool(name="pulse_get_unread_count")
def get_unread_count() -> str:
    """Get the count of unread notifications."""
    result = api_request("GET", "/notifications/unread_count/")
    if "error" in result:
        return _fmt_error(result)
    return f"Unread notifications: {result.get('count', 0)}"


@mcp.tool(name="pulse_mark_notification_read")
def mark_notification_read(notification_id: str) -> str:
    """
    Mark a notification as read.

    Args:
        notification_id: The notification's UUID (from pulse_list_notifications,
            shown as [<uuid>]). As a convenience a task key like "PLS-237" is
            also accepted — every UNREAD notification about that task is resolved
            to its UUID and marked read (PLAT-2691).
    """
    import re
    nid = (notification_id or "").strip()

    # PLAT-2691: the Pulse API needs the notification UUID, but callers routinely
    # pass a task key (the tool contract was ambiguous). Detect a task-key shape
    # and resolve it to the real notification UUID(s) instead of 404ing.
    if re.match(r"^[A-Z][A-Z0-9]*-\d+$", nid):
        listing = api_request(
            "GET", "/notifications/", params={"page_size": 100, "is_read": "false"}
        )
        if "error" in listing:
            return _fmt_error(listing)
        matches = [
            n for n in listing.get("results", [])
            if str(n.get("item_key") or "").upper() == nid.upper()
        ]
        if not matches:
            return (
                f"No unread notification found for task {nid}. Use "
                f"pulse_list_notifications to see notification UUIDs (shown as "
                f"[<uuid>]), or pulse_mark_all_notifications_read."
            )
        marked = 0
        for n in matches:
            r = api_request("POST", f"/notifications/{n['id']}/read/")
            if "error" not in r:
                marked += 1
        return f"Marked {marked} notification(s) for task {nid} as read."

    result = api_request("POST", f"/notifications/{nid}/read/")
    if "error" in result:
        if result.get("status_code") == 404:
            return (
                f"Notification '{nid}' not found. Pass the notification UUID from "
                f"pulse_list_notifications (shown as [<uuid>]), not a task key."
            )
        return _fmt_error(result)
    return "Notification marked as read."


@mcp.tool(name="pulse_mark_all_notifications_read")
def mark_all_notifications_read() -> str:
    """Mark all notifications as read."""
    result = api_request("POST", "/notifications/read_all/")
    if "error" in result:
        return _fmt_error(result)
    return "All notifications marked as read."


# =============================================================================
# SCHEMES (Admin/Config - read-only for most agents)
# =============================================================================

@mcp.tool(name="pulse_list_permission_schemes")
def list_permission_schemes() -> str:
    """List permission schemes and their grant counts."""
    result = api_request("GET", "/permission-schemes/")
    if "error" in result:
        return _fmt_error(result)
    schemes = (result if isinstance(result, list) else result.get("results", []))
    if not schemes:
        return "No permission schemes found."
    output = f"{len(schemes)} scheme(s):\n\n"
    for s in schemes:
        output += f"- {s.get('name')} (slug: {s.get('slug')}) — ID: {s.get('id')}\n"
    return output


@mcp.tool(name="pulse_list_notification_schemes")
def list_notification_schemes() -> str:
    """List notification schemes."""
    result = api_request("GET", "/notification-schemes/")
    if "error" in result:
        return _fmt_error(result)
    schemes = (result if isinstance(result, list) else result.get("results", []))
    if not schemes:
        return "No notification schemes found."
    output = f"{len(schemes)} scheme(s):\n\n"
    for s in schemes:
        output += f"- {s.get('name')} (slug: {s.get('slug')}) — ID: {s.get('id')}\n"
    return output


@mcp.tool(name="pulse_list_security_schemes")
def list_security_schemes() -> str:
    """List security schemes and their security levels."""
    result = api_request("GET", "/security-schemes/")
    if "error" in result:
        return _fmt_error(result)
    schemes = (result if isinstance(result, list) else result.get("results", []))
    if not schemes:
        return "No security schemes found."
    output = f"{len(schemes)} scheme(s):\n\n"
    for s in schemes:
        output += f"- {s.get('name')} (slug: {s.get('slug')}) — ID: {s.get('id')}\n"
    return output


@mcp.tool(name="pulse_get_project_configuration")
def get_project_configuration(project_id: str) -> str:
    """
    Get the scheme configuration for a project (issue type scheme, permission scheme, etc.).

    Args:
        project_id: Project ID
    """
    result = api_request("GET", "/project-configurations/", params={"project": project_id})
    if "error" in result:
        return _fmt_error(result)
    configs = (result if isinstance(result, list) else result.get("results", []))
    if not configs:
        return "No configuration found for this project."
    return json.dumps(configs[0] if configs else {}, indent=2)


# =============================================================================
# WEBHOOKS
# =============================================================================

@mcp.tool(name="pulse_list_webhook_keys")
def list_webhook_keys() -> str:
    """List webhook API keys for external integrations."""
    result = api_request("GET", "/webhook-keys/")
    if "error" in result:
        return _fmt_error(result)
    keys = (result if isinstance(result, list) else result.get("results", []))
    if not keys:
        return "No webhook keys configured."
    output = f"{len(keys)} webhook key(s):\n\n"
    for k in keys:
        active = "active" if k.get("is_active") else "inactive"
        output += f"- {k.get('name', 'unnamed')} [{active}] — ID: {k.get('id')}\n"
        if k.get("source"): output += f"  Source: {k['source']}\n"
    return output


def main():
    """Run the MCP server."""
    if not PULSE_JWT_TOKEN:
        logger.info(
            "SHIZUHA_PULSE_JWT_TOKEN not set; expecting Authorization header per request"
        )
    if PULSE_USER_JWT_TOKEN:
        logger.info("User delegation token available for user-context tools")

    logger.info(f"Starting Shizuha Pulse MCP server")
    logger.info(f"API URL: {PULSE_API_URL}")

    if "--http-port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http-port") + 1])
        host = "0.0.0.0"
        logger.info(f"Streamable HTTP transport on {host}:{port}")
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    elif "--sse-port" in sys.argv:
        # Legacy SSE support (deprecated)
        port = int(sys.argv[sys.argv.index("--sse-port") + 1])
        logger.info(f"SSE transport on 0.0.0.0:{port} (deprecated, use --http-port)")
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
