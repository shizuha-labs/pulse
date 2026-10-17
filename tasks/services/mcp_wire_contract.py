"""MCP JSON-RPC wire contract (PLAT-5343 HLD v12 §7).

Server errors: ``-32008 shizuha.request_deadline_exceeded``,
``-32009 shizuha.server_draining``, ``-32010 shizuha.attempt_unresolved``.
Bounded ``data``: ``code``, ``retryable``, ``request_id``, ``retry_after_ms``,
``reuse_idempotency_key`` — never stacks, URLs, tokens, subjects, membership
data, or stored bodies.

Closed admission → HTTP 503 + ``Retry-After: 1`` + ``Cache-Control: no-store``
+ matching ``-32009`` when a JSON-RPC ID exists. Accepted-and-proved-terminal
→ HTTP 200 so the client parses the protocol object. Committed SSE → exactly
one result/error for the original ID, flush, close. Unproved terminal at wire
cutoff → no JSON-RPC failure may claim retry safety: HTTP/1.1 closes without
terminal body, HTTP/2 resets only that stream, SSE closes without terminal
event (fenced transport failure, attempt stays FENCING_UNPROVEN).

Retries: reads may use a new ID; mutations use a new JSON-RPC ID but the
exact prior idempotency key and semantic arguments. Key carried in
``params._meta["shizuha.io/idempotency-key"]`` and propagated as REST
``Idempotency-Key``.
"""

CODE_REQUEST_DEADLINE_EXCEEDED = -32008
CODE_SERVER_DRAINING = -32009
CODE_ATTEMPT_UNRESOLVED = -32010

MESSAGE_REQUEST_DEADLINE_EXCEEDED = 'shizuha.request_deadline_exceeded'
MESSAGE_SERVER_DRAINING = 'shizuha.server_draining'
MESSAGE_ATTEMPT_UNRESOLVED = 'shizuha.attempt_unresolved'

IDEMPOTENCY_KEY_META = 'shizuha.io/idempotency-key'
IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key'

DRAIN_RETRY_AFTER_SECONDS = 1
DRAIN_RETRY_AFTER_MS = 1000


def bounded_error_data(code, retryable, request_id='', retry_after_ms=None,
                       reuse_idempotency_key=None):
    """Bounded ``data`` payload per §7 — the ONLY fields allowed.

    Never stacks, URLs, tokens, subjects, membership data, or stored bodies.
    """
    data = {
        'code': code,
        'retryable': bool(retryable),
        'request_id': str(request_id or '')[:128],
    }
    if retry_after_ms is not None:
        data['retry_after_ms'] = int(retry_after_ms)
    if reuse_idempotency_key is not None:
        data['reuse_idempotency_key'] = bool(reuse_idempotency_key)
    return data


def jsonrpc_error(rpc_id, code, message, data):
    """JSON-RPC error object bound to the original request ID."""
    return {
        'jsonrpc': '2.0',
        'id': rpc_id,
        'error': {'code': code, 'message': message, 'data': data},
    }


def request_deadline_exceeded(rpc_id, request_id=''):
    """-32008: the request deadline exceeded before terminal proof."""
    return jsonrpc_error(
        rpc_id, CODE_REQUEST_DEADLINE_EXCEEDED, MESSAGE_REQUEST_DEADLINE_EXCEEDED,
        bounded_error_data(CODE_REQUEST_DEADLINE_EXCEEDED, retryable=True,
                           request_id=request_id),
    )


def server_draining(rpc_id, request_id=''):
    """-32009: closed admission — drain in progress, retry after 1s."""
    return jsonrpc_error(
        rpc_id, CODE_SERVER_DRAINING, MESSAGE_SERVER_DRAINING,
        bounded_error_data(CODE_SERVER_DRAINING, retryable=True,
                           request_id=request_id,
                           retry_after_ms=DRAIN_RETRY_AFTER_MS),
    )


def attempt_unresolved(rpc_id, request_id='', reuse_key=True):
    """-32010: the prior attempt is unresolved — retry with the EXACT prior
    idempotency key and semantic arguments (never a new key)."""
    return jsonrpc_error(
        rpc_id, CODE_ATTEMPT_UNRESOLVED, MESSAGE_ATTEMPT_UNRESOLVED,
        bounded_error_data(CODE_ATTEMPT_UNRESOLVED, retryable=True,
                           request_id=request_id,
                           retry_after_ms=DRAIN_RETRY_AFTER_MS,
                           reuse_idempotency_key=bool(reuse_key)),
    )


def closed_admission_response(rpc_id=None, request_id=''):
    """HTTP surface for closed admission (§7).

    HTTP 503 + ``Retry-After: 1`` + ``Cache-Control: no-store``; the
    matching ``-32009`` body only when a JSON-RPC ID exists.
    """
    headers = {
        'Retry-After': str(DRAIN_RETRY_AFTER_SECONDS),
        'Cache-Control': 'no-store',
    }
    body = None
    if rpc_id is not None:
        body = server_draining(rpc_id, request_id)
    return 503, headers, body


def unproved_terminal_transport_abort(rpc_id=None):
    """Fenced transport abort for unproved terminal at wire cutoff (§7).

    No JSON-RPC failure may claim retry safety: HTTP/1.1 closes without a
    terminal body (None), HTTP/2 resets only that stream, SSE closes without
    a terminal event. The attempt stays FENCING_UNPROVEN.
    """
    return None


def extract_idempotency_key(params):
    """Read the idempotency key from ``params._meta`` (§7)."""
    if not isinstance(params, dict):
        return ''
    meta = params.get('_meta') or {}
    if not isinstance(meta, dict):
        return ''
    return str(meta.get(IDEMPOTENCY_KEY_META) or '')[:128]
