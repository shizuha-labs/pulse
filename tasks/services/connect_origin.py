"""PLS-102: signed origin token for Pulse → Connect system messages.

Connect's ``InternalSystemMessageView`` (``connect/views/internal_api.py``)
hardens ``/internal/system-message/`` with ``_validate_system_origin_token``: in
addition to the ``X-Internal-Service`` header it requires a short-lived HMAC
``X-System-Message-Token`` = ``<unix_ts>:<hex_hmac>`` signed with the shared
``CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET`` over
``sender_service|recipient_user_id|sha256(content)|ts`` (service allowlisted,
age <= CONNECT_SYSTEM_MESSAGE_TOKEN_MAX_AGE_SECONDS, default 300s).

No Pulse caller sent this token, so every Pulse-originated system DM (assignment,
reminder, alert, @-mention) would 403 once the hardened Connect image ships.

This module is the single source of truth: callers build their request body, then
pass it to ``system_message_headers(body)`` so the signed token is derived from
the *exact* content/recipient that Connect will re-hash. When no secret is
configured (today's non-enforcing Connect) the token is omitted and behaviour is
unchanged.
"""
import hashlib
import hmac
import time

from django.conf import settings

# Sender-service identity used for both attribution and the token allowlist /
# signature. Must be present in Connect's CONNECT_SYSTEM_MESSAGE_ALLOWLIST.
SYSTEM_MESSAGE_SENDER_SERVICE = 'shizuha-pulse'
INTERNAL_SERVICE_HEADER_VALUE = 'shizuha-pulse'


def system_message_origin_token(recipient_user_id, content, sender_service=SYSTEM_MESSAGE_SENDER_SERVICE, ts=None):
    """Return ``<ts>:<hmac>`` matching Connect's verifier, or ``None`` when the
    origin secret is unset (Connect does not enforce the token in that case)."""
    secret = getattr(settings, 'CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET', '') or ''
    if not secret:
        return None
    ts = int(ts if ts is not None else time.time())
    body_hash = hashlib.sha256((content or '').encode('utf-8')).hexdigest()
    payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
    sig = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'{ts}:{sig}'


def system_message_headers(body, extra_headers=None):
    """Headers for a Connect ``/internal/system-message/`` POST.

    Always includes ``X-Internal-Service``; adds the signed
    ``X-System-Message-Token`` (derived from this very ``body``) when the origin
    secret is configured. Pass the SAME dict you send as ``json=`` so the token's
    content/recipient match exactly.
    """
    headers = {'X-Internal-Service': INTERNAL_SERVICE_HEADER_VALUE}
    if extra_headers:
        headers.update(extra_headers)
    recipient = body.get('recipient_user_id')
    if recipient is not None:
        token = system_message_origin_token(
            recipient,
            body.get('content') or '',
            body.get('sender_service', SYSTEM_MESSAGE_SENDER_SERVICE),
        )
        if token:
            headers['X-System-Message-Token'] = token
    return headers
