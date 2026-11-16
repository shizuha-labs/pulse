"""Who may mint a deploy-verify waiver without a user-delegation token.

PLAT-12577. The MCP adapter used to require a user-delegation token before
Pulse ever saw the call, so a role-qualified caller (operator, or a member of
admin-ops / devops / qa) with no delegation was refused with "User delegation
token not available". PLS-675 already names those roles as the authority.
This module only decides whether the caller's OWN token carries one of those
roles. Pulse remains the authorizer: a forged claim still fails the roster
check, and automation identities stay fail-closed even if a role claim is
present.
"""

from __future__ import annotations

import base64
import json

_WAIVER_ROLE_CLAIMS = frozenset({
    "operator",
    "admin-ops",
    "devops",
    "qa",
})
_WAIVER_AUTOMATION_EMAILS = frozenset({
    "system@pulse",
    "workflow@pulse",
    "scheduler@pulse.shizuha.com",
    "pr-sync@shizuha.local",
})


def _jwt_claims(token: str) -> dict:
    """Best-effort unverified claims. Authorization still happens in Pulse.

    The payload is base64, not a signature check. A token that does not decode
    is treated as no authority, which keeps the user-delegation requirement.
    """
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".", 2)[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _claim_values(payload: dict, *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, str):
            parts = [raw]
        elif isinstance(raw, (list, tuple, set)):
            parts = list(raw)
        else:
            continue
        for part in parts:
            text = str(part or "").strip().lower()
            if text:
                values.add(text)
    return values


def _is_automation_identity(payload: dict) -> bool:
    emails = _claim_values(payload, "email")
    if emails & _WAIVER_AUTOMATION_EMAILS:
        return True
    for email in emails:
        if email.startswith("system@") or "webhook" in email:
            return True
    kinds = _claim_values(payload, "token_use", "principal_kind", "identity_kind")
    return bool(kinds & {"automation", "service", "system"})


class AutomationWaiverRefusal(Exception):
    """The caller is an automation identity and must not reach Pulse."""


def waiver_call_uses_user_delegation(token: str, *, has_user_delegation: bool) -> bool:
    """Whether the MCP call should set ``as_user``.

    A present user-delegation token acts as that user. With none, the agent's
    own token is sent and Pulse's roster check is the authority. Automation
    raises before either call.
    """
    if _is_automation_identity(_jwt_claims(token)):
        raise AutomationWaiverRefusal("automation identities cannot mint a deploy-verify waiver")
    return bool(has_user_delegation)


def caller_may_mint_deploy_waiver(token: str) -> bool:
    """True when the caller's own token already carries waiver authority.

    Operator and admin-ops/devops/qa callers reach Pulse with no user-delegation
    token (MCP connector sessions, live seat rows whose legacy row holds the
    only delegation). Their own token is the authority PLS-675 checks. A missing
    role claim, or any automation identity, stays fail-closed.
    """
    payload = _jwt_claims(token)
    if not payload or _is_automation_identity(payload):
        return False
    roles = _claim_values(
        payload,
        "role",
        "roles",
        "groups",
        "team",
        "teams",
        "assignment_group",
    )
    if roles & _WAIVER_ROLE_CLAIMS:
        return True
    if payload.get("is_operator") is True or payload.get("operator") is True:
        return True
    return False
