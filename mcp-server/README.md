# Shizuha Pulse MCP Server

An MCP (Model Context Protocol) server that provides AI agents with tools to interact with Shizuha Pulse, the task and project management service.

## Features

This MCP server exposes the following tools:

| Tool | Description |
|------|-------------|
| `get_my_identity` | Get the authenticated user's identity from JWT |
| `list_tasks` | List tasks with optional filtering |
| `get_task` | Get detailed task information |
| `create_task` | Create a new task |
| `update_task` | Update an existing task |
| `complete_task` | Mark a task as complete |
| `add_comment` | Add a comment to a task |
| `list_comments` | List comments on a task |
| `list_projects` | List available projects |
| `search_tasks` | Search tasks by title/description |
| `get_my_tasks` | Get active tasks assigned to current user, ordered by the agent queue policy (default top 5; optional `limit`) |

## Agent Queue Policy

Default active task queries (`pulse_get_my_tasks`, `pulse_get_user_tasks`, and `pulse_list_tasks` without an explicit `status`) exclude terminal/scheduled work before pagination. They return work in this order:

1. Ready work before waiting work.
2. Highest priority first: `urgent`, `high`, `normal`/`medium`, then `low`.
3. FIFO by `created_at` inside the selected priority bucket.

`pulse_get_my_tasks` defaults to **limit=5** (queue-ordered head) so heartbeats do not dump large personal queues into model context. Pass `limit=N` (max 200) for a wider window; when more active work exists, the tool reports total remaining so a short page is not a false-empty queue.

Agents should work the first task they can move. If it is misrouted, blocked, or outside their pod, they should triage/forward it and continue to the next eligible task.

## Installation

```bash
cd mcp-server
pip install -r requirements.txt
```

## Configuration

The server requires the following environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `PULSE_JWT_TOKEN` | JWT token for authentication (required) | - |
| `PULSE_API_URL` | Base URL for Pulse API | `http://localhost:8002` |

## Usage

### Running Standalone

```bash
export PULSE_JWT_TOKEN="your-jwt-token"
export PULSE_API_URL="http://localhost:8002"
python pulse_server.py
```

### With Claude Code / Shizuha Agent

Configure the MCP server in the agent's `mcp_servers` field:

```json
{
  "shizuha-pulse": {
    "command": "python",
    "args": ["/path/to/pulse_server.py"],
    "env": {
      "PULSE_JWT_TOKEN": "${AGENT_JWT_TOKEN}",
      "PULSE_API_URL": "http://shizuha-pulse:8002"
    }
  }
}
```

### With Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "shizuha-pulse": {
      "command": "python",
      "args": ["/path/to/pulse_server.py"],
      "env": {
        "PULSE_JWT_TOKEN": "your-jwt-token",
        "PULSE_API_URL": "http://localhost:8002"
      }
    }
  }
}
```

## Tool Examples

### List pending tasks

```
list_tasks(status="pending", limit=10)
```

### Create a task

```
create_task(
    title="Implement feature X",
    description="Add the new feature as discussed",
    priority="high",
    project_id=1
)
```

### Complete a task

```
complete_task(task_id="uuid-here")
```

### Add a comment

```
add_comment(
    task_id="uuid-here",
    content="I've completed the implementation. Ready for review."
)
```

## Development

### Testing the server

```bash
# Install dependencies
pip install -r requirements.txt

# Run with test token
export PULSE_JWT_TOKEN="test-token"
python pulse_server.py
```

### Protocol

This server uses the MCP stdio transport. It reads JSON-RPC messages from stdin and writes responses to stdout. All logging goes to stderr.

## Schema ↔ serializer contract tests (drift prevention — PLAT-1355)

Each write tool builds a POST payload whose keys must match the fields the
backend DRF serializer accepts and requires. When they diverge, the write fails
(or silently drops a field) **only at runtime, in production** — the recurring
drift class behind ID-38 / PLS-224 / PLAT-1171, and locally AT-84 (`log_work`
sent `time_spent_minutes`/`work_date` instead of `duration_minutes`/`worked_date`)
and PLAT-48 (`link_issues` sent a bare `target_item`).

To catch this before deploy, `tasks/tests/test_mcp_schema_contract.py` captures
each tool's **actual** outgoing payload (with `api_request` mocked) and checks it
against the target serializer, introspected live (the serializer is the single
source of truth, so the check can't go stale):

1. **No unknown keys** — every payload key is a writable serializer field.
2. **No missing required** — every serializer-required field is present
   (honouring declared either/or groups such as `duration_minutes`/`duration_hours`).

Run it (needs the Django backend deps + a Postgres for the test runner; the
tests themselves touch no DB):

```bash
pip install -r requirements.txt              # backend
pip install -r mcp-server/requirements.txt   # mcp deps
python manage.py test tasks.tests.test_mcp_schema_contract
```

CI runs this on every push/PR via `.forgejo/workflows/mcp-contract-tests.yml`
(alongside the standalone `test_work_logs.py` / `test_link_issues.py` regression
tests), so drift fails before deployment.

### Opting a new MCP write tool into the contract test

Add **one entry** to `CONTRACT_CASES` in
`tasks/tests/test_mcp_schema_contract.py`:

```python
{
    "tool": "log_work",                    # pulse_server function name
    "kwargs": dict(task_id="AT-1", time_spent_minutes=5),  # a minimal VALID call
    "endpoint_suffix": "/work-logs/",      # the POST path the tool hits
    "serializer": WorkLogCreateSerializer, # the backend create serializer
    # optional: sets where >=1 field must be present (serializer enforces in validate())
    "either_or_groups": [{"duration_minutes", "duration_hours"}],
},
```

The test inspects only the outgoing payload shape (never the DB), so `kwargs`
just needs to be a representative valid call.

## License

Part of the Shizuha Stack.
