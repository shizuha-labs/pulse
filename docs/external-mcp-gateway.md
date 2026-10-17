# External MCP Gateway

Pulse exposes a tenant-scoped external MCP facade at:

- `GET /api/external-mcp/tools/`
- `POST /api/external-mcp/call/`

Authentication uses an `Authorization: Bearer <token>` header backed by `ExternalMcpClient` and `ExternalMcpToken` rows. Every token must include `task.read`; each client is constrained to its `org_id` and `allowed_project_ids`.

## Scopes

Read tools require `task.read` and are filtered to the token's allowed projects:

- `pulse_get_task`
- `pulse_search_tasks`
- `pulse_list_tasks`
- `pulse_get_available_transitions`
- `pulse_list_comments`
- `pulse_get_issue_links`

Write tools are only advertised/executable when the token has the matching explicit scope:

| Tool | Scope | Notes |
| --- | --- | --- |
| `pulse_create_task` | `task.write` | Requires duplicate review and an idempotency key. |
| `pulse_update_task` | `task.write` | Non-state fields only; status changes must use transitions. |
| `pulse_link_issues` | `task.write` | Both tasks must be visible to the token. |
| `pulse_add_comment` | `comment.write` | Adds an external-MCP provenance footer. |
| `pulse_execute_transition` | `transition.execute` | Calls the workflow engine; validators/conditions still apply. |

## Idempotency

All write calls require an idempotency key via either `Idempotency-Key` header, top-level `idempotency_key`, or `arguments.idempotency_key`. A repeated successful write with the same request hash replays without mutating again. Reusing the same key with a different payload returns `409 idempotency_key_conflict`.

## Duplicate discipline

`pulse_create_task` searches visible tasks in the target project using `duplicate_query` (or the title). If candidates are found, creation returns `409 duplicate_candidates_found` until the caller retries with `duplicate_acknowledged: true`.

## Write policy hooks

`ExternalMcpClient.rate_limit_policy.write_policy` supports product/security guardrails:

```json
{
  "write_policy": {
    "require_confirmation": true,
    "draft_only": true
  }
}
```

- `require_confirmation` requires `arguments.confirmed: true` before any write.
- `draft_only` marks created tasks as `external_mcp_draft` and denies workflow transitions.

## Audit and provenance

Every discovery/call attempt records `ExternalMcpCallAudit` with actor/client/org/tool/result, request hash, and idempotency key. Mutating tools also leave a task comment that names the external client, actor, tool, and idempotency key so AI-originated changes are visible in Pulse history.
