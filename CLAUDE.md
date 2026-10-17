# CLAUDE.md - Shizuha Pulse

## Overview

**Shizuha Pulse** is the task and item management service for the Shizuha platform (similar to Jira).

| Setting | Value |
|---------|-------|
| Backend Port | 8002 |
| Frontend Port | 5173 |
| Database | shizuha_pulse |
| Redis DB | 2 |
| URL Path | `/pulse/` |

## Access

| URL | Description |
|-----|-------------|
| `/pulse/` | Task management UI |
| `/pulse/api/` | REST API |
| `/pulse/admin/` | Django Admin |
| `/pulse/api/docs/` | API Documentation |

## API Endpoints

### Projects
```
GET/POST   /pulse/api/projects/           # List/create projects
GET/PATCH  /pulse/api/projects/{id}/      # Project details
```

### Items (Tasks/Alerts)
```
GET/POST   /pulse/api/items/              # List/create items
GET/PATCH  /pulse/api/items/{id}/         # Item details
POST       /pulse/api/items/{id}/assign/  # Assign to user
POST       /pulse/api/items/{id}/transition/ # Change status
```

### Webhooks
```
POST /pulse/api/webhooks/alertmanager/    # Prometheus Alertmanager
POST /pulse/api/webhooks/grafana/         # Grafana alerts
POST /pulse/api/webhooks/generic/         # Generic webhook
```

## Key Models

- **Item** - Unified model for tasks, actions, and alerts (via `mode` field)
- **Project** - Container for items (Jira-style)
- **Organization** - Top-level grouping
- **WebhookKey** - API keys for external integrations

## Features

- Task/action/alert modes via single Item model
- Webhook integrations (Alertmanager, Grafana, PagerDuty-style)
- Deduplication, silencing, escalation, inhibition
- Celery workers for background processing

## Assignment Invariant

Workflow-created and agent-created work should route through teams wherever possible. Use `assignment_group` values such as `engineering`, `architecture`, `review`, `merge`, `qa`, `security`, `documentation`, `analytics`, `admin-ops`, or project-specific teams like `social-media`.

Avoid directly assigning new tasks to concrete people unless the operator explicitly asked for that person or no team exists. If direct assignment is unavoidable, leave an audit comment explaining why the team route was not used.

It is valid for both fields to be populated after routing: `assignment_group=engineering` plus `assignee_email=ryo@shizuha.com` means Pulse resolved the current available owner from the team. Non-terminal workflow post-functions should use `assign_team`; review approval routes PR landing to `merge`, and merge blockers escalate to `admin-ops`.

## Research Workflow Convention

Research and analytics are one team. Route discovery, synthesis, measurement, and wiki-upkeep work through the canonical `assignment_group=research-analytics`; the legacy `research` and `analytics` labels are compatibility aliases only.

- Use `workflow='simple'` for recurring wiki-only/read-only research maintenance where the deliverable is a wiki/comment update and code, deployment, runtime, and configuration changes are explicitly out of scope.
- Use `workflow='reviewed-task'` for research briefs that affect decisions and should be independently reviewed before being treated as reliable.
- Use `workflow='feature-request-rfc'` for cross-system or product discovery that frames future implementation work.
- If research discovers a bug, security issue, or implementation opportunity, keep it out of the research task unless the task explicitly allows follow-up creation. Document it in wiki/comments and let Architecture, the operator, or the owning workflow create separate `autonomous-bug`, `security-finding`, or implementation work.

## Commands

All development commands run from the `compose/` directory using the master docker-compose.yaml:

```bash
# Start service with frontend
cd compose && docker compose up -d shizuha-pulse shizuha-pulse-frontend

# Run migrations
cd compose && docker compose exec shizuha-pulse python manage.py migrate

# Run tests
cd compose && docker compose exec shizuha-pulse python manage.py test

# View logs
cd compose && docker compose logs -f shizuha-pulse

```

## Project Structure

```
shizuha-pulse/
├── taskmanager/          # Django project settings
├── tasks/                # Main app
│   ├── models.py         # Item, Project, Organization
│   ├── views.py          # DRF ViewSets
│   ├── serializers.py    # DRF serializers
│   ├── webhooks.py       # Webhook handlers
│   └── services/         # Business logic
├── frontend/             # React UI
└── docs/                 # Sphinx documentation
```

Also reference [../CLAUDE.md](../CLAUDE.md) for platform-wide rules.
