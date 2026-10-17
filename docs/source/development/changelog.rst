=========
Changelog
=========

All notable changes to Shizuha Hub will be documented in this file.

The format is based on `Keep a Changelog <https://keepachangelog.com/>`_.

[1.0.0] - 2024-01-15
--------------------

Initial release of Shizuha Hub - Unified Task, Action, and Alert Management Service.

Added
^^^^^

**Core Features**

- Unified ``Item`` model handling tasks, actions, and alerts
- Mode-specific behavior with shared infrastructure
- Hierarchical item types (e.g., ``task.reminder``, ``action.hedging.short``)

**Task Management**

- Create, update, complete tasks
- Due date and reminder support
- Recurring tasks with flexible patterns
- Priority levels and tagging

**Action Queue**

- Action submission with approval workflows
- Auto-approval rules with JSON Logic conditions
- Callback/webhook execution
- Retry handling for failed executions

**Alert Management**

- Alert submission with fingerprint deduplication
- Occurrence tracking for deduplicated alerts
- Acknowledge and resolve workflows
- Severity levels (info, warning, error, critical)

**Rules Engine**

- Approval rules for auto-approve/reject
- Silence rules for notification suppression
- Escalation rules with multi-stage workflows
- Inhibition rules for cascading failure handling

**Services**

- ``AlertDeduplicationService`` - Fingerprint-based deduplication
- ``EscalationService`` - Time-based escalation
- ``SilencingService`` - Rule evaluation and management
- ``InhibitionService`` - Alert suppression logic

**API**

- RESTful API with Django REST Framework
- Filtered endpoints for tasks, actions, alerts
- Bulk operations for acknowledge/resolve
- Statistics endpoint

**Infrastructure**

- Docker Compose deployment
- Celery for async task processing
- PostgreSQL database
- Redis for caching and task queue

**Documentation**

- Sphinx documentation with auto-generation
- API reference with examples
- Model and service documentation
- Deployment guides

Changed
^^^^^^^

- N/A (initial release)

Deprecated
^^^^^^^^^^

- N/A (initial release)

Removed
^^^^^^^

- N/A (initial release)

Fixed
^^^^^

- N/A (initial release)

Security
^^^^^^^^

- N/A (initial release)

[Unreleased]
------------

Planned for future releases:

- Authentication and authorization
- Rate limiting
- Audit logging
- Prometheus metrics export
- Grafana dashboard templates
- Slack/PagerDuty integrations
- WebSocket support for real-time updates
