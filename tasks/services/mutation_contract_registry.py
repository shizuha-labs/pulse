"""Mutation-contract registry (PLAT-5343 HLD v12 §6.4).

Classifies every existing agent-callable MCP mutator and its REST route as
``local_transaction`` | ``transactional_outbox`` | ``naturally_idempotent_with_receipt``
| ``async_only``. No unclassified mutation may be advertised retryable or
pass the CI registry gate; a new mutation fails CI until classified. Read-only
tools are outside the receipt contract.

This is the registry slice 2's enforcement activation depends on — coordinate,
do not duplicate (parent PLAT-5343).
"""

LOCAL_TRANSACTION = 'local_transaction'
TRANSACTIONAL_OUTBOX = 'transactional_outbox'
NATURALLY_IDEMPOTENT_WITH_RECEIPT = 'naturally_idempotent_with_receipt'
ASYNC_ONLY = 'async_only'

CONTRACT_KINDS = frozenset({
    LOCAL_TRANSACTION,
    TRANSACTIONAL_OUTBOX,
    NATURALLY_IDEMPOTENT_WITH_RECEIPT,
    ASYNC_ONLY,
})


class MutationContract:
    """One classified mutator: MCP tool name + REST route + contract kind."""

    __slots__ = ('tool_name', 'rest_route', 'kind', 'note')

    def __init__(self, tool_name, rest_route, kind, note=''):
        if kind not in CONTRACT_KINDS:
            raise ValueError(f'unknown contract kind: {kind}')
        self.tool_name = tool_name
        self.rest_route = rest_route
        self.kind = kind
        self.note = note

    def as_dict(self):
        return {
            'tool_name': self.tool_name,
            'rest_route': self.rest_route,
            'kind': self.kind,
            'note': self.note,
        }


# Mandatory initial set (§6.4): every existing agent-callable MCP mutator.
# Attachment finalize is included — raw byte transfer is governed by the
# object-store upload ID, not stored in the receipt.
MUTATION_CONTRACTS = (
    MutationContract('shizuha.task.create', 'POST /api/tasks/', LOCAL_TRANSACTION),
    MutationContract('shizuha.task.edit', 'PATCH /api/tasks/{key}/', LOCAL_TRANSACTION),
    MutationContract('shizuha.task.edit', 'PUT /api/tasks/{key}/', LOCAL_TRANSACTION),
    MutationContract('shizuha.transition.execute',
                     'POST /api/tasks/{key}/transitions/', LOCAL_TRANSACTION),
    MutationContract('shizuha.assignment.set', 'PATCH /api/tasks/{key}/', LOCAL_TRANSACTION),
    MutationContract('shizuha.comment.create', 'POST /api/tasks/{key}/comments/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.issue.link', 'POST /api/tasks/{key}/links/', LOCAL_TRANSACTION),
    MutationContract('shizuha.issue.unlink', 'DELETE /api/tasks/{key}/links/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.worklog.create', 'POST /api/tasks/{key}/worklogs/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.watch.set', 'POST /api/tasks/{key}/watch/', LOCAL_TRANSACTION),
    MutationContract('shizuha.notification.state', 'PATCH /api/notifications/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.sprint.configure', 'PATCH /api/sprints/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.version.configure', 'PATCH /api/versions/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.component.configure', 'PATCH /api/components/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.workflow.configure', 'PATCH /api/workflows/{id}/',
                     LOCAL_TRANSACTION),
    MutationContract('shizuha.attachment.finalize', 'POST /api/attachments/{id}/finalize/',
                     LOCAL_TRANSACTION,
                     note='raw byte transfer governed by the object-store upload ID'),
)

REGISTRY_BY_TOOL = {}
for _contract in MUTATION_CONTRACTS:
    REGISTRY_BY_TOOL.setdefault(_contract.tool_name, []).append(_contract)


def classify(tool_name, rest_route=None):
    """Return the contract kind for a mutator, or None when unclassified.

    An unclassified mutation may not be advertised retryable (§6.4).
    """
    rows = REGISTRY_BY_TOOL.get(tool_name) or []
    if rest_route is None:
        return rows[0].kind if rows else None
    for row in rows:
        if row.rest_route == rest_route:
            return row.kind
    return None


def registry_gate():
    """CI registry gate (§6.4).

    Fails (returns the offending list) when any registered contract is
    malformed — unknown kind, missing route, or duplicate (tool, route)
    pairs. A new mutation fails CI until classified; the caller adds it to
    MUTATION_CONTRACTS with the correct kind.
    """
    offending = []
    seen = set()
    for contract in MUTATION_CONTRACTS:
        if contract.kind not in CONTRACT_KINDS:
            offending.append(f'unknown kind: {contract.tool_name}')
        if not contract.rest_route:
            offending.append(f'missing rest route: {contract.tool_name}')
        pair = (contract.tool_name, contract.rest_route)
        if pair in seen:
            offending.append(f'duplicate (tool, route): {pair}')
        seen.add(pair)
    return offending
