"""PLAT-5928 — plan-time exact-org recipient resolution for alert envelopes.

In V1 the recipient set for a webhook is derived from its default assignment
group within the exact organization (reducible/overridable by the authorizer).
An empty exact-org roster produces ``suppressed_unrouted``, never an ordinary
fallback target. No data from a foreign org is consulted, and only recipient
ids + team + lane are returned — never a tenant payload.
"""
from __future__ import annotations

from typing import Any


def resolve_recipients_for_webhook(webhook_key) -> list[dict[str, Any]]:
    """Return plan-time authorized recipients for a webhook key.

    Each entry: ``{"recipient_id": int, "team": str, "delivery_lane": str}``.

    The default implementation resolves the webhook key's configured on-call /
    assignment-group membership via the pluggable settings resolver, or returns
    [] when unset/empty.
    """
    group = getattr(webhook_key, 'default_assignment_group', None) or ''
    resolver = getattr(webhook_key, '_recipient_resolver', None)
    if resolver is not None:
        return resolver()
    if not group:
        return []
    return _resolve_group_members(group)


def _resolve_group_members(group: str) -> list[dict[str, Any]]:
    """Resolve members of an on-call/assignment group within the exact org.

    Kept intentionally narrow and override-able: for the durable slice the
    concrete roster wiring (Admin/ID directory + Pulse roster rev) plugs in
    here without changing the acceptance/relay contract.
    """
    from django.conf import settings
    resolver_path = getattr(settings, 'PULSE_ALERT_RECIPIENT_RESOLVER', '')
    if resolver_path:
        module, _, attr = resolver_path.rpartition('.')
        if module and attr:
            import importlib
            fn = getattr(importlib.import_module(module), attr)
            return fn(group)
    return []
