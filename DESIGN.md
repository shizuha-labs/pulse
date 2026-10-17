# DESIGN.md - Shizuha Pulse

Current architecture and implementation of the Pulse (Task/Item Management) service.

## Tech Stack

- **Backend**: Django 4.2+ + DRF 3.14+ + Celery + PostgreSQL + Redis
- **Frontend**: React 18 + Vite 5 + TanStack Query + Tailwind CSS
- **Testing**: Playwright for E2E
- **Docs**: Sphinx (.rst files)
- **Authentication**: Federated JWT via Shizuha ID

## Project Structure

```
shizuha-pulse/
├── taskmanager/           # Django project settings
├── tasks/                 # Main Django app
│   ├── models.py          # Unified Item model + rules
│   ├── views.py           # REST API endpoints
│   ├── serializers.py     # DRF serializers
│   ├── services/          # Business logic
│   │   ├── deduplication.py
│   │   ├── silencing.py
│   │   ├── escalation.py
│   │   ├── inhibition.py
│   │   └── permissions.py
│   ├── auth_views.py      # Auth endpoints
│   └── webhooks/          # External webhook handlers
├── frontend/              # React Vite app
│   └── tests/             # Playwright E2E tests
├── docs/                  # Sphinx documentation
└── docker-compose.yml
```

## Data Models

### Core Entities

- **Organization**: Multi-tenant container (Jira-style)
- **Project**: Collection of items within organization
- **Item**: Unified model with three modes:
  - `task`: Personal reminders, to-dos, calendar events
  - `action`: System-generated with approval workflows
  - `alert`: Monitoring alerts with deduplication

### Membership & Permissions

- **OrganizationMembership**: Roles (owner, admin, member, viewer)
- **ProjectMembership**: Project-level granular permissions

### Rules & Automation

- **ApprovalRule**: Auto-approve/reject actions based on conditions
- **SilenceRule**: Suppress notifications while tracking items
- **EscalationRule**: Auto-escalate unhandled items
- **InhibitionRule**: Suppress target alerts when source alerts fire
- **FingerprintConfig**: Alert deduplication configuration

### Integrations

- **Subscription**: Event subscriptions (webhooks, Redis, push)
- **WebhookKey**: External webhook authentication (Grafana, Prometheus)
- **UserNotificationChannel**: Discord/Telegram per user
- **Comment**: Threaded discussions on items

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/items/` | Unified item CRUD |
| `/api/tasks/` | Backward-compatible task endpoint |
| `/api/alerts/` | Alert submission and management |
| `/api/actions/submit/` | Action creation |
| `/api/rules/approval/` | Approval rules |
| `/api/rules/silence/` | Silence rules |
| `/api/rules/escalation/` | Escalation rules |
| `/api/rules/inhibition/` | Inhibition rules |
| `/api/subscriptions/` | Event subscriptions |
| `/api/organizations/` | Organization management |
| `/api/projects/` | Project management |
| `/api/webhooks/grafana/<key>/` | Grafana webhook |
| `/api/webhooks/alertmanager/<key>/` | Prometheus Alertmanager; log output must redact `<key>` as `/api/webhooks/alertmanager/<redacted>/` until header/HMAC auth replaces path-key auth. |
| `/api/webhooks/generic/<key>/` | Generic webhook |

## Key Features

### Unified Item Model
Single model handles tasks, actions, and alerts with mode-specific logic:
- Tasks: Due dates, reminders, recurrence
- Actions: Approval workflows, callbacks
- Alerts: Deduplication, severity, firing state

### Alert Deduplication
Configurable fingerprint strategies to group related alerts:
- Labels-based fingerprinting
- Custom fingerprint functions
- Automatic resolve on recovery

### Escalation Engine
Unacknowledged items escalate through stages:
- Configurable time intervals
- Notification channels per stage
- Auto-assignment options

### Inhibition Rules
Reduce alert noise during cascading failures:
- Source/target matchers
- Automatic suppression

## Platform Integration

- Uses Shizuha ID for JWT authentication
- Subscriptions can publish push notifications via shizuha-id
- Organization hierarchy shared with shizuha-admin


## Webhook secret logging

Alertmanager webhook keys are legacy path credentials and must never be printed
or pasted in operational output. Runtime logging installs
`WebhookSecretRedactionFilter`, which rewrites any
`/api/webhooks/alertmanager/<key>/` (including prefixed paths such as
`/pulse/api/webhooks/alertmanager/<key>/`) to
`/api/webhooks/alertmanager/<redacted>/` before console/Pulse alert handlers format
records. Operators should still use no-print secret handling for Kubernetes
secret updates and should prefer a future non-secret URL plus header/HMAC auth
when Alertmanager configuration is migrated.

### Page-path organization metrics

Org progress uses grouped or bounded projections over the caller-authorized
queryset. Throughput reads in-window exits plus one preceding transition per
item; current dwell reads one latest transition per active item. Do not add a
per-item history query or correlated predecessor lookup per exit: the latter
can still rescan/sort thousands of historical rows inside one SQL statement.
Preserve carry-in durations and verify query plans/timing on representative data,
not just query count. `/api/items/statistics/?projection=status_counts` returns
complete grouped counts and status categories through the normal organization,
permission, mode and active filters; dashboard totals must not sum a list page.

The status-window read has a concurrent partial time/item/id index with the
exact `field_name = 'Status'` predicate. Keep the existing item/time index for
per-item carry-in/latest reads. Verify the production plan and completion time
after the governed index migration; a warm best-case timing alone is not an
upper-bound latency claim.
