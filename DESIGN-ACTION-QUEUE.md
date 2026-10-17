# Shizuha Pulse - Unified Task & Action Service

## Overview

A standalone, generic service that unifies:
1. **Personal Task Management** - Calendar events, reminders, to-dos, recurring tasks
2. **Automated Action Queue** - System-generated actions requiring approval/execution
3. **MCP Integration** - Claude can manage both tasks and actions

This service lives in `shizuha-stack` and is independent of any specific system (not tied to arbiter).

## Design Philosophy

Rather than multiple separate systems, we use a **unified Item model** with different "modes":
- **Task mode**: Personal items (reminders, calendar events, to-dos)
- **Action mode**: System-generated items with approval workflows and callbacks
- **Alert mode**: System alerts requiring acknowledgment, with deduplication and escalation

This allows:
- Shared infrastructure (notifications, scheduling, UI)
- Mixed views (see tasks, pending actions, AND active alerts in one place)
- Same MCP interface for Claude to manage everything
- Unified silencing/snoozing across all modes
- Leveraging existing shizuha-pulse foundation

**Future flexibility**: Modes may be merged or reorganized as patterns emerge. The `item_type` field provides the real semantic distinction.

## Core Concepts

### Item (unified model)
An Item can be a personal task, automated action, OR system alert:

| Field | Task Mode | Action Mode | Alert Mode |
|-------|-----------|-------------|------------|
| `mode` | `task` | `action` | `alert` |
| `title` | "Call dentist" | "Short 100 PEPE" | "High CPU on web-01" |
| `description` | Notes | Action details | Alert message/context |
| `status` | pending/in_progress/completed | pending/approved/executing/completed/failed | firing/acknowledged/resolved |
| `due_date` | When to do it | Expiration time | Auto-resolve time |
| `callback_url` | null | Webhook to execute | Resolution webhook |
| `requires_approval` | false | true/false | false (uses ack) |
| `source` | manual/calendar/jira | arbiter/some-service | monitoring/app-name |

### Item Types (extensible)
```
# Personal task types
task.generic      - Basic to-do
task.reminder     - Time-based reminder
task.calendar     - Calendar event
task.meeting      - Meeting with participants
task.deadline     - Important deadline
task.recurring    - Repeating task

# Action types (system-generated)
action.hedging.short   - Short futures position
action.hedging.long    - Long futures position
action.hedging.sell    - Sell spot assets
action.hedging.buy     - Buy spot assets
action.transfer        - Asset transfer
action.approval        - Generic approval request
action.custom          - Custom action type

# Alert types (system-generated)
alert.error       - Error condition
alert.warning     - Warning condition
alert.critical    - Critical/urgent alert
alert.info        - Informational alert
alert.recovery    - Recovery notification (can auto-resolve related alerts)
alert.custom      - Custom alert type
```

### Approval Rules (action mode)
Rules that auto-approve/reject actions:
- Amount thresholds
- Source whitelists
- Time-based rules
- Custom conditions (JSON Logic)

### Silencing Rules (all modes)
Rules that suppress notifications for matching items:
- Time-based (silence until X)
- Pattern-based (silence all alerts matching fingerprint pattern)
- Source-based (silence everything from service X)
- Recurring (silence "Take Medicine" task on weekends)

Silencing does NOT prevent item creation - it only suppresses notifications.

### Escalation Rules (alert mode primarily, but applicable to all)
Rules that escalate items if not handled within a timeframe:
- Bump severity after N minutes
- Add notification channels (telegram → telegram + phone call)
- Reassign to different person/team
- Create a new high-priority action item

### Inhibition Rules (alert mode)
Rules that suppress child alerts when a parent alert is firing:
- If "Database down" is firing, suppress "API timeout" alerts
- Reduces noise during cascading failures
- Can be based on labels/tags matching

### Subscriptions
Subscribe to item events for notifications:
- Push notifications (shizuha-notify)
- Webhooks
- Redis pub/sub

## Data Model

### Item (core model)
```python
class Item(models.Model):
    # Identity
    id = UUIDField(primary_key=True)
    mode = CharField(choices=['task', 'action', 'alert'])  # Core distinction
    item_type = CharField(max_length=100)  # e.g., "task.reminder", "action.hedging.short", "alert.error"

    # Basic info
    title = CharField(max_length=400)
    description = TextField(blank=True)

    # Source tracking
    source = CharField(max_length=100)  # "manual", "google_calendar", "arbiter-backend"
    source_id = CharField(null=True)  # External reference
    source_url = URLField(null=True)  # Link to source

    # Assignment
    assignee_id = IntegerField(null=True)
    assignee_email = EmailField(null=True)
    created_by_id = IntegerField(null=True)

    # Status
    status = CharField(max_length=20)
    # Task statuses: pending, in_progress, completed, cancelled, blocked
    # Action statuses: pending, approved, executing, completed, failed, rejected, expired
    # Alert statuses: firing, acknowledged, resolved, silenced

    priority = CharField(choices=PRIORITY_CHOICES, default='normal')
    severity = CharField(choices=['info', 'warning', 'error', 'critical'], null=True)  # For alerts

    # Timing
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    due_date = DateTimeField(null=True)  # For tasks: when due; For actions: expiration
    start_date = DateTimeField(null=True)
    reminder_time = DateTimeField(null=True)
    completed_at = DateTimeField(null=True)

    # === Task-specific fields ===
    # Recurrence
    is_recurring = BooleanField(default=False)
    frequency = CharField(choices=FREQUENCY_CHOICES, null=True)
    recurrence_pattern = JSONField(null=True)
    recurrence_end_date = DateTimeField(null=True)
    parent_item_id = ForeignKey('self', null=True)

    # Effort tracking
    estimated_hours = DecimalField(null=True)
    actual_hours = DecimalField(null=True)

    # === Action-specific fields ===
    # Payload
    payload = JSONField(default=dict)  # Action/alert-specific data

    # Approval
    requires_approval = BooleanField(default=False)
    auto_approved = BooleanField(default=False)
    approved_by = CharField(null=True)  # 'auto', 'user:xxx', 'mcp:claude'
    approved_at = DateTimeField(null=True)
    approval_rule_id = ForeignKey('ApprovalRule', null=True)
    rejection_reason = TextField(blank=True)

    # Execution
    callback_url = URLField(null=True)
    callback_method = CharField(default='POST')
    callback_headers = JSONField(default=dict)
    execution_result = JSONField(null=True)
    error_message = TextField(blank=True)
    retry_count = IntegerField(default=0)
    max_retries = IntegerField(default=3)

    # === Alert-specific fields ===
    # Deduplication
    fingerprint = CharField(max_length=255, null=True, db_index=True)  # Computed hash for dedup
    fingerprint_fields = ArrayField(CharField(), null=True)  # Which fields were used to compute fingerprint

    # Occurrence tracking (for deduplicated alerts)
    occurrence_count = IntegerField(default=1)
    first_fired_at = DateTimeField(null=True)
    last_fired_at = DateTimeField(null=True)

    # Resolution
    resolved_at = DateTimeField(null=True)
    resolved_by = CharField(null=True)  # 'auto', 'user:xxx', 'recovery:item_id'
    resolution_note = TextField(blank=True)

    # Acknowledgment
    acknowledged_at = DateTimeField(null=True)
    acknowledged_by = CharField(null=True)

    # Escalation tracking
    escalation_level = IntegerField(default=0)  # 0 = not escalated, 1+ = escalation stages
    last_escalated_at = DateTimeField(null=True)
    escalation_rule_id = ForeignKey('EscalationRule', null=True)

    # Correlation (for grouping related alerts)
    correlation_id = CharField(max_length=255, null=True, db_index=True)  # Group related alerts
    parent_alert_id = ForeignKey('self', null=True, related_name='child_alerts')  # For inhibition

    # === Common fields ===
    # Notifications
    reminder_sent = BooleanField(default=False)
    reminder_count = IntegerField(default=0)
    last_reminder_sent = DateTimeField(null=True)
    notification_preferences = JSONField(default=dict)

    # Silencing (applies to ALL modes)
    silenced_until = DateTimeField(null=True)  # Manual silence
    silenced_by = CharField(null=True)
    silence_reason = TextField(blank=True)
    silence_rule_id = ForeignKey('SilenceRule', null=True)  # If silenced by rule

    # Labels (for matching rules - inspired by Prometheus)
    labels = JSONField(default=dict)  # e.g., {"host": "web-01", "service": "nginx", "env": "prod"}

    # Metadata
    tags = ArrayField(CharField(), default=list)
    metadata = JSONField(default=dict)

    class Meta:
        indexes = [
            Index(fields=['mode', 'status']),
            Index(fields=['item_type']),
            Index(fields=['assignee_id']),
            Index(fields=['due_date']),
            Index(fields=['source', 'source_id']),
            Index(fields=['is_recurring']),
            Index(fields=['fingerprint']),
            Index(fields=['severity']),
            Index(fields=['correlation_id']),
            Index(fields=['silenced_until']),
        ]
        constraints = [
            # Only one active (firing/acknowledged) alert per fingerprint
            UniqueConstraint(
                fields=['fingerprint'],
                condition=Q(mode='alert', status__in=['firing', 'acknowledged']),
                name='unique_active_alert_fingerprint'
            )
        ]
```

### ApprovalRule
```python
class ApprovalRule(models.Model):
    id = UUIDField(primary_key=True)
    name = CharField(max_length=255)
    description = TextField(blank=True)

    # Matching
    item_types = ArrayField(CharField())  # e.g., ["action.hedging.*"]
    sources = ArrayField(CharField(), null=True)
    conditions = JSONField()  # JSON Logic format

    # Behavior
    auto_approve = BooleanField(default=False)
    auto_reject = BooleanField(default=False)
    priority = IntegerField(default=0)

    is_active = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)
```

### SilenceRule (all modes)
```python
class SilenceRule(models.Model):
    """
    Suppresses notifications for matching items.
    Items are still created and tracked, just not notified.
    """
    id = UUIDField(primary_key=True)
    name = CharField(max_length=255)
    description = TextField(blank=True)

    # Matching - flexible matchers
    modes = ArrayField(CharField(), null=True)  # ['task', 'action', 'alert'] or null for all
    item_types = ArrayField(CharField(), null=True)  # ["alert.warning", "task.reminder"]
    sources = ArrayField(CharField(), null=True)
    severities = ArrayField(CharField(), null=True)  # ['info', 'warning']
    label_matchers = JSONField(default=dict)  # {"host": "web-01", "env": "prod"}
    conditions = JSONField(null=True)  # JSON Logic for complex matching

    # Time bounds
    starts_at = DateTimeField(null=True)  # null = immediate
    ends_at = DateTimeField(null=True)  # null = indefinite (until manually disabled)

    # Recurrence (e.g., silence every weekend)
    is_recurring = BooleanField(default=False)
    recurrence_pattern = JSONField(null=True)  # cron-like or rrule

    # Metadata
    created_by = CharField(null=True)
    reason = TextField(blank=True)
    is_active = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)

    # Stats
    match_count = IntegerField(default=0)
    last_matched_at = DateTimeField(null=True)
```

### EscalationRule
```python
class EscalationRule(models.Model):
    """
    Defines escalation behavior when items aren't handled in time.
    """
    id = UUIDField(primary_key=True)
    name = CharField(max_length=255)
    description = TextField(blank=True)

    # Matching
    modes = ArrayField(CharField(), null=True)
    item_types = ArrayField(CharField(), null=True)
    sources = ArrayField(CharField(), null=True)
    min_severity = CharField(null=True)  # Only escalate if severity >= this
    label_matchers = JSONField(default=dict)
    conditions = JSONField(null=True)

    # Escalation stages (list of escalation steps)
    # Each stage defines what happens after a delay
    stages = JSONField()
    # Example:
    # [
    #   {
    #     "delay_minutes": 5,
    #     "actions": [
    #       {"type": "bump_severity", "to": "error"},
    #       {"type": "add_channels", "channels": ["telegram"]}
    #     ]
    #   },
    #   {
    #     "delay_minutes": 15,
    #     "actions": [
    #       {"type": "bump_severity", "to": "critical"},
    #       {"type": "add_channels", "channels": ["phone_call"]},
    #       {"type": "reassign", "to": "oncall"}
    #     ]
    #   },
    #   {
    #     "delay_minutes": 30,
    #     "actions": [
    #       {"type": "create_action", "action_type": "action.escalation", "title": "Unhandled critical alert"}
    #     ]
    #   }
    # ]

    is_active = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)
```

### InhibitionRule (alert mode)
```python
class InhibitionRule(models.Model):
    """
    Suppresses target alerts when source alerts are firing.
    Useful for reducing noise during cascading failures.
    """
    id = UUIDField(primary_key=True)
    name = CharField(max_length=255)
    description = TextField(blank=True)

    # Source alert matcher (the "parent" alert that inhibits others)
    source_item_types = ArrayField(CharField(), null=True)
    source_severities = ArrayField(CharField(), null=True)
    source_label_matchers = JSONField(default=dict)

    # Target alert matcher (alerts to suppress when source is firing)
    target_item_types = ArrayField(CharField(), null=True)
    target_severities = ArrayField(CharField(), null=True)
    target_label_matchers = JSONField(default=dict)

    # Labels that must match between source and target
    # e.g., ["host"] means source.labels.host must equal target.labels.host
    equal_labels = ArrayField(CharField(), default=list)

    is_active = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)

    # Example: If "Database down" alert is firing, suppress "API timeout" alerts
    # from the same host:
    # source_item_types = ["alert.critical"]
    # source_label_matchers = {"service": "postgresql"}
    # target_item_types = ["alert.error", "alert.warning"]
    # target_label_matchers = {"dependency": "postgresql"}
    # equal_labels = ["env"]  # Only inhibit in same environment
```

## Alert Deduplication

Deduplication is critical for alerting - we don't want to flood notifications when the same alert fires repeatedly.

### Fingerprinting Strategy

The fingerprint uniquely identifies an alert condition. It's computed from fields that define "the same alert":

```python
def compute_fingerprint(item_type: str, source: str, labels: dict, payload: dict) -> str:
    """
    Compute a fingerprint for deduplication.

    The fingerprint is built from:
    1. item_type (e.g., "alert.error")
    2. source (e.g., "arbiter-backend")
    3. Sorted subset of labels (configurable per source)
    4. Optional payload fields (configurable per source)
    """
    # Get fingerprint config for this source
    config = get_fingerprint_config(source)

    # Build fingerprint components
    components = [item_type, source]

    # Add configured label fields
    for key in sorted(config.get('label_fields', labels.keys())):
        if key in labels:
            components.append(f"{key}={labels[key]}")

    # Add configured payload fields
    for key in config.get('payload_fields', []):
        if key in payload:
            components.append(f"payload.{key}={payload[key]}")

    # Hash it
    fingerprint_string = "|".join(str(c) for c in components)
    return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:32]
```

### Fingerprint Configuration

Sources can register their fingerprint configuration:

```python
FINGERPRINT_CONFIGS = {
    "arbiter-backend": {
        "label_fields": ["asset", "exchange", "alert_type"],
        "payload_fields": ["threshold_type"]
    },
    "nginx-monitor": {
        "label_fields": ["host", "status_code", "path_pattern"],
        "payload_fields": []
    },
    # Default: use all labels, no payload fields
    "__default__": {
        "label_fields": None,  # None = use all labels
        "payload_fields": []
    }
}
```

### Deduplication Flow

```python
def submit_alert(data: dict) -> Item:
    """
    Submit an alert with automatic deduplication.
    """
    # Compute fingerprint
    fingerprint = compute_fingerprint(
        item_type=data['item_type'],
        source=data['source'],
        labels=data.get('labels', {}),
        payload=data.get('payload', {})
    )

    # Check for existing active alert with same fingerprint
    existing = Item.objects.filter(
        mode='alert',
        fingerprint=fingerprint,
        status__in=['firing', 'acknowledged']
    ).first()

    if existing:
        # Deduplicate: update existing alert
        existing.occurrence_count += 1
        existing.last_fired_at = timezone.now()

        # Optionally update severity if new one is higher
        if severity_rank(data.get('severity')) > severity_rank(existing.severity):
            existing.severity = data['severity']

        # Merge any new labels/payload
        existing.labels.update(data.get('labels', {}))
        existing.payload.update(data.get('payload', {}))

        existing.save()

        # Still publish event for tracking, but skip notification (it's a dupe)
        publish_event('alert.deduplicated', existing)
        return existing

    # New alert - create it
    item = Item.objects.create(
        mode='alert',
        item_type=data['item_type'],
        fingerprint=fingerprint,
        fingerprint_fields=get_fingerprint_config(data['source']).get('label_fields', []),
        status='firing',
        first_fired_at=timezone.now(),
        last_fired_at=timezone.now(),
        **data
    )

    # Check silence rules before notifying
    if not is_silenced(item) and not is_inhibited(item):
        notify_subscribers(item)
        schedule_escalation(item)

    return item
```

### Auto-Resolution

Alerts can be auto-resolved in several ways:

1. **Recovery alert**: A source sends a "recovery" alert that resolves the original
2. **Timeout**: Alert expires after `due_date`
3. **Manual**: User or Claude resolves it

```python
def handle_recovery_alert(data: dict) -> list[Item]:
    """
    Process a recovery alert and resolve matching firing alerts.
    """
    # Find alerts to resolve based on correlation
    resolved = []

    # Option 1: Explicit correlation_id
    if correlation_id := data.get('correlation_id'):
        alerts = Item.objects.filter(
            mode='alert',
            correlation_id=correlation_id,
            status__in=['firing', 'acknowledged']
        )
        for alert in alerts:
            alert.status = 'resolved'
            alert.resolved_at = timezone.now()
            alert.resolved_by = f"recovery:{data.get('source')}"
            alert.save()
            resolved.append(alert)

    # Option 2: Match by labels
    elif resolve_matchers := data.get('resolve_matchers'):
        # Find alerts matching the label criteria
        alerts = find_alerts_by_labels(resolve_matchers)
        # ... resolve them

    return resolved
```

### Subscription
```python
class Subscription(models.Model):
    id = UUIDField(primary_key=True)
    name = CharField(max_length=255)

    # What to subscribe to
    modes = ArrayField(CharField(), null=True)  # ['task', 'action'] or null for all
    item_types = ArrayField(CharField(), null=True)
    statuses = ArrayField(CharField(), null=True)

    # How to notify
    notify_type = CharField(choices=['webhook', 'redis', 'shizuha_notify'])

    # Webhook config
    webhook_url = URLField(null=True)
    webhook_headers = JSONField(default=dict)

    # Redis config
    redis_channel = CharField(null=True)

    # shizuha-notify config
    notify_channels = ArrayField(CharField(), null=True)  # ['telegram', 'email']
    notify_template = CharField(null=True)

    is_active = BooleanField(default=True)
```

## API Endpoints

### Items (unified)
```
# CRUD
POST   /api/items/                    # Create item (task or action)
GET    /api/items/                    # List items (filterable by mode, type, status)
GET    /api/items/{id}/               # Get item details
PATCH  /api/items/{id}/               # Update item
DELETE /api/items/{id}/               # Delete/cancel item

# Task-specific actions
POST   /api/items/{id}/complete/      # Mark task completed
POST   /api/items/{id}/snooze/        # Snooze reminder

# Action-specific actions
POST   /api/items/{id}/approve/       # Approve action
POST   /api/items/{id}/reject/        # Reject action
POST   /api/items/{id}/execute/       # Force execute
POST   /api/items/{id}/retry/         # Retry failed action

# Filtered views
GET    /api/items/tasks/              # All tasks
GET    /api/items/actions/            # All actions
GET    /api/items/pending/            # Pending approvals
GET    /api/items/today/              # Due today
GET    /api/items/overdue/            # Overdue items

# Stats
GET    /api/items/stats/              # Combined statistics
```

### Shortcut endpoints (convenience)
```
# Task shortcuts
POST   /api/tasks/                    # Create task (mode=task)
GET    /api/tasks/                    # List tasks only

# Action shortcuts
POST   /api/actions/                  # Create action (mode=action)
GET    /api/actions/                  # List actions only
POST   /api/actions/submit/           # Simple action submission for services

# Alert shortcuts
POST   /api/alerts/                   # Submit alert (with deduplication)
GET    /api/alerts/                   # List alerts (default: firing + acknowledged)
GET    /api/alerts/firing/            # Currently firing alerts
GET    /api/alerts/history/           # Resolved alerts (with pagination)
POST   /api/alerts/{id}/acknowledge/  # Acknowledge alert
POST   /api/alerts/{id}/resolve/      # Resolve alert
POST   /api/alerts/{id}/silence/      # Silence specific alert
POST   /api/alerts/bulk-acknowledge/  # Acknowledge multiple alerts
POST   /api/alerts/bulk-resolve/      # Resolve multiple alerts
```

### Rules & Subscriptions
```
# Approval Rules
POST   /api/rules/approval/
GET    /api/rules/approval/
PATCH  /api/rules/approval/{id}/
DELETE /api/rules/approval/{id}/
POST   /api/rules/approval/{id}/test/

# Silence Rules
POST   /api/rules/silence/
GET    /api/rules/silence/
PATCH  /api/rules/silence/{id}/
DELETE /api/rules/silence/{id}/
POST   /api/rules/silence/{id}/test/
GET    /api/rules/silence/active/      # Currently active silences

# Escalation Rules
POST   /api/rules/escalation/
GET    /api/rules/escalation/
PATCH  /api/rules/escalation/{id}/
DELETE /api/rules/escalation/{id}/

# Inhibition Rules
POST   /api/rules/inhibition/
GET    /api/rules/inhibition/
PATCH  /api/rules/inhibition/{id}/
DELETE /api/rules/inhibition/{id}/
POST   /api/rules/inhibition/{id}/test/

# Subscriptions
POST   /api/subscriptions/
GET    /api/subscriptions/
PATCH  /api/subscriptions/{id}/
DELETE /api/subscriptions/{id}/

# Fingerprint Configuration
GET    /api/fingerprint-configs/
POST   /api/fingerprint-configs/
PATCH  /api/fingerprint-configs/{source}/
```

## MCP Server

The MCP server provides tools for Claude to manage both tasks and actions:

### Task Management Tools
```python
@mcp_tool
def create_task(
    title: str,
    description: str = None,
    task_type: str = "task.generic",
    due_date: str = None,
    priority: str = "normal",
    tags: list[str] = None
) -> Item:
    """Create a new personal task"""

@mcp_tool
def list_tasks(
    status: str = None,
    due_before: str = None,
    priority: str = None,
    limit: int = 20
) -> list[Item]:
    """List personal tasks"""

@mcp_tool
def complete_task(item_id: str) -> Item:
    """Mark a task as completed"""

@mcp_tool
def reschedule_task(
    item_id: str,
    new_due_date: str,
    new_reminder: str = None
) -> Item:
    """Reschedule a task"""
```

### Action Management Tools
```python
@mcp_tool
def list_pending_actions(
    item_type: str = None,
    source: str = None,
    priority: str = None,
    limit: int = 10
) -> list[Item]:
    """List actions waiting for approval"""

@mcp_tool
def approve_action(
    item_id: str,
    comment: str = None
) -> Item:
    """Approve a pending action"""

@mcp_tool
def reject_action(
    item_id: str,
    reason: str
) -> Item:
    """Reject an action"""

@mcp_tool
def bulk_approve_actions(
    item_ids: list[str] = None,
    item_type: str = None,
    source: str = None,
    max_amount_usd: float = None
) -> dict:
    """Approve multiple actions"""
```

### Alert Management Tools
```python
@mcp_tool
def list_firing_alerts(
    severity: str = None,
    source: str = None,
    labels: dict = None,
    limit: int = 20
) -> list[Item]:
    """List currently firing alerts"""

@mcp_tool
def acknowledge_alert(
    item_id: str,
    comment: str = None
) -> Item:
    """Acknowledge an alert (stops escalation but keeps it visible)"""

@mcp_tool
def resolve_alert(
    item_id: str,
    resolution_note: str = None
) -> Item:
    """Resolve/close an alert"""

@mcp_tool
def bulk_acknowledge_alerts(
    item_ids: list[str] = None,
    source: str = None,
    severity: str = None,
    label_matchers: dict = None
) -> dict:
    """Acknowledge multiple alerts at once"""

@mcp_tool
def silence_alerts(
    duration_minutes: int,
    item_ids: list[str] = None,
    label_matchers: dict = None,
    reason: str = None
) -> SilenceRule:
    """Create a temporary silence for matching alerts"""

@mcp_tool
def get_alert_history(
    hours: int = 24,
    source: str = None,
    severity: str = None,
    limit: int = 50
) -> list[Item]:
    """Get resolved alerts from recent history"""
```

### Unified Tools
```python
@mcp_tool
def get_item(item_id: str) -> Item:
    """Get any item (task, action, or alert) by ID"""

@mcp_tool
def search_items(
    query: str,
    mode: str = None,  # 'task', 'action', 'alert', or None for all
    limit: int = 20
) -> list[Item]:
    """Search across all items"""

@mcp_tool
def get_daily_summary() -> dict:
    """Get summary of today's tasks, pending actions, and active alerts"""

@mcp_tool
def silence_item(
    item_id: str,
    duration_minutes: int,
    reason: str = None
) -> Item:
    """Silence notifications for any item (task, action, or alert)"""

@mcp_tool
def create_silence_rule(
    name: str,
    duration_minutes: int = None,  # null = indefinite
    modes: list[str] = None,
    item_types: list[str] = None,
    label_matchers: dict = None,
    reason: str = None
) -> SilenceRule:
    """Create a silence rule for matching items"""

@mcp_tool
def create_approval_rule(
    name: str,
    item_types: list[str],
    conditions: dict,
    auto_approve: bool = True
) -> ApprovalRule:
    """Create an auto-approval rule"""
```

### MCP Resources
```python
@mcp_resource("hub://today")
def today_summary() -> dict:
    """Today's tasks, pending actions, and active alerts"""

@mcp_resource("hub://pending")
def pending_items() -> list[Item]:
    """All items needing attention (tasks due, actions pending, alerts firing)"""

@mcp_resource("hub://alerts")
def active_alerts() -> list[Item]:
    """Currently firing and acknowledged alerts"""

@mcp_resource("hub://silences")
def active_silences() -> list[SilenceRule]:
    """Currently active silence rules"""

@mcp_resource("hub://stats")
def statistics() -> dict:
    """Overall statistics including alert metrics"""
```

## React Dashboard

Mobile-friendly, responsive dashboard:

### Views

1. **Today** (default view)
   - Tasks due today
   - Pending actions requiring approval
   - Firing alerts (prominent, color-coded by severity)
   - Upcoming reminders
   - Quick action buttons

2. **Tasks**
   - Personal task list
   - Filter by status, priority, tags
   - Calendar view option
   - Create/edit tasks

3. **Actions**
   - Pending approvals queue
   - Action history
   - Execution results
   - Swipe to approve/reject

4. **Alerts**
   - Firing alerts (grouped by severity)
   - Acknowledged alerts
   - Alert history/timeline
   - Swipe to acknowledge/resolve
   - Quick silence button
   - Occurrence count badge (for deduplicated alerts)
   - Filter by source, severity, labels

5. **Calendar**
   - Monthly/weekly/daily views
   - Tasks and deadlines
   - Recurring items

6. **Rules**
   - Manage approval rules
   - Manage silence rules (with active/scheduled indicators)
   - Manage escalation rules
   - Manage inhibition rules
   - Test rules
   - Enable/disable

7. **Settings**
   - Notification preferences
   - Integrations
   - MCP connection status
   - Fingerprint configurations per source

### Mobile UX
- Pull-to-refresh
- Swipe gestures (approve/reject/complete)
- Push notifications
- Offline support (PWA)
- Touch-friendly buttons

## Integration Examples

### Arbiter Integration (hedging)
```python
# In arbiter-backend when imbalance detected
def submit_hedging_action(asset, action_type, quantity, amount_usd):
    requests.post(
        "http://shizuha-hub:8000/api/actions/submit/",
        json={
            "item_type": f"action.hedging.{action_type}",
            "title": f"{action_type.title()} {quantity} {asset}",
            "source": "arbiter-backend",
            "source_id": f"hedge-{asset}-{int(time.time())}",
            "payload": {
                "asset": asset,
                "quantity": quantity,
                "amount_usd": amount_usd,
                "exchange": "binance"
            },
            "callback_url": "http://arbiter-backend:8000/api/execute-hedge/",
            "priority": "high" if amount_usd > 500 else "normal",
            "due_date": (datetime.now() + timedelta(hours=1)).isoformat()
        }
    )
```

### Personal Task via Claude
```
User: "Remind me to call the dentist tomorrow at 10am"

Claude: [calls create_task tool]
{
    "title": "Call the dentist",
    "task_type": "task.reminder",
    "due_date": "2024-01-16T10:00:00",
    "reminder_time": "2024-01-16T09:45:00"
}
```

### Alert from Application (generic pattern)
```python
# In any service that wants to send alerts to shizuha-hub
import requests
from datetime import datetime, timedelta

def send_alert(
    title: str,
    severity: str,  # 'info', 'warning', 'error', 'critical'
    labels: dict,
    description: str = None,
    payload: dict = None,
    auto_resolve_hours: int = None
):
    requests.post(
        "http://shizuha-hub:8000/api/alerts/",
        json={
            "item_type": f"alert.{severity}",
            "title": title,
            "description": description or "",
            "source": "my-service-name",
            "severity": severity,
            "labels": labels,
            "payload": payload or {},
            "due_date": (
                (datetime.now() + timedelta(hours=auto_resolve_hours)).isoformat()
                if auto_resolve_hours else None
            )
        }
    )

# Example: API error rate alert
send_alert(
    title="High error rate on /api/users",
    severity="warning",
    labels={
        "host": "api-01",
        "endpoint": "/api/users",
        "service": "user-service"
    },
    description="Error rate exceeded 5% threshold (currently 8.2%)",
    payload={
        "error_rate": 8.2,
        "threshold": 5.0,
        "sample_errors": ["timeout", "connection_refused"]
    }
)

# Example: Recovery alert (resolves related alerts)
def send_recovery(correlation_id: str):
    requests.post(
        "http://shizuha-hub:8000/api/alerts/",
        json={
            "item_type": "alert.recovery",
            "title": "Error rate returned to normal",
            "source": "my-service-name",
            "severity": "info",
            "correlation_id": correlation_id,  # Resolves alerts with this correlation_id
            "labels": {"host": "api-01", "service": "user-service"}
        }
    )
```

### Alert from arbiter (trading specific)
```python
# In arbiter-backend when risk threshold exceeded
def submit_risk_alert(asset, risk_type, current_value, threshold):
    requests.post(
        "http://shizuha-hub:8000/api/alerts/",
        json={
            "item_type": "alert.error" if current_value > threshold * 1.5 else "alert.warning",
            "title": f"Risk alert: {risk_type} for {asset}",
            "source": "arbiter-backend",
            "severity": "error" if current_value > threshold * 1.5 else "warning",
            "labels": {
                "asset": asset,
                "risk_type": risk_type,
                "exchange": "binance"
            },
            "payload": {
                "current_value": current_value,
                "threshold": threshold,
                "excess_percent": ((current_value / threshold) - 1) * 100
            },
            "correlation_id": f"risk-{asset}-{risk_type}",  # For grouping
            "due_date": (datetime.now() + timedelta(hours=4)).isoformat()  # Auto-resolve after 4h
        }
    )
```

### Mixed Query via Claude
```
User: "What do I need to handle today?"

Claude: [calls get_daily_summary tool]

Response:
"Here's your summary for today:

**🔴 Active Alerts (2)**
- CRITICAL: Database connection pool exhausted on db-01 (firing for 12 min, 3 occurrences)
- WARNING: High memory usage on web-02 (acknowledged)

**Tasks (3)**
- Call dentist at 10:00 AM
- Review quarterly report (due 5:00 PM)
- Team standup at 2:00 PM

**Pending Actions (2)**
- Approve: Sell 0.5 ETH ($1,200) - from arbiter
- Approve: Short 50,000 PEPE ($45) - from arbiter

The database alert looks urgent. Would you like me to acknowledge it while you investigate?"
```

### Alert Silence via Claude
```
User: "Silence all warnings from web-02 for the next hour, we're doing maintenance"

Claude: [calls create_silence_rule tool]
{
    "name": "web-02 maintenance",
    "duration_minutes": 60,
    "modes": ["alert"],
    "label_matchers": {"host": "web-02"},
    "reason": "Scheduled maintenance"
}

Response:
"Done! I've silenced all alerts from web-02 for the next hour.
Silence will expire at 3:45 PM.

Note: 2 currently firing alerts from web-02 will continue to show but won't
generate new notifications. Would you like me to also acknowledge them?"
```

## Migration from shizuha-pulse

Since we're extending shizuha-pulse:

1. **Rename**: `shizuha-pulse` → `shizuha-hub`
2. **Migrate model**: Add new fields to Task model
3. **Add**: ApprovalRule, Subscription models
4. **Add**: MCP server
5. **Update**: React Native app → React web app (mobile-friendly)

### Migration Steps
```sql
-- Add mode field
ALTER TABLE tasks_task ADD COLUMN mode VARCHAR(10) DEFAULT 'task';

-- Add action-specific fields
ALTER TABLE tasks_task ADD COLUMN payload JSONB DEFAULT '{}';
ALTER TABLE tasks_task ADD COLUMN requires_approval BOOLEAN DEFAULT false;
ALTER TABLE tasks_task ADD COLUMN callback_url VARCHAR(500);
-- ... etc

-- Rename table
ALTER TABLE tasks_task RENAME TO hub_item;
```

## Directory Structure

```
shizuha-hub/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── manage.py
├── config/
│   ├── settings.py
│   ├── urls.py
│   └── celery.py
├── hub/
│   ├── models/
│   │   ├── __init__.py
│   │   ├── item.py            # Item model
│   │   ├── rules.py           # ApprovalRule, SilenceRule, EscalationRule, InhibitionRule
│   │   └── subscription.py    # Subscription model
│   ├── serializers/
│   │   ├── __init__.py
│   │   ├── item.py
│   │   ├── rules.py
│   │   └── alert.py
│   ├── views/
│   │   ├── __init__.py
│   │   ├── items.py
│   │   ├── tasks.py
│   │   ├── actions.py
│   │   ├── alerts.py
│   │   └── rules.py
│   ├── urls.py
│   ├── services/
│   │   ├── approval.py        # Approval rule evaluation
│   │   ├── execution.py       # Callback execution
│   │   ├── notification.py    # Notification dispatch
│   │   ├── recurrence.py      # Recurring task generation
│   │   ├── deduplication.py   # Alert fingerprinting & dedup
│   │   ├── silencing.py       # Silence rule evaluation
│   │   ├── escalation.py      # Escalation processing
│   │   └── inhibition.py      # Inhibition rule evaluation
│   └── tasks.py               # Celery tasks
├── mcp_server/
│   ├── __init__.py
│   ├── server.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── tasks.py
│   │   ├── actions.py
│   │   ├── alerts.py
│   │   └── unified.py
│   └── resources.py
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── App.jsx
│   │   ├── pages/
│   │   │   ├── Today.jsx
│   │   │   ├── Tasks.jsx
│   │   │   ├── Actions.jsx
│   │   │   ├── Alerts.jsx
│   │   │   ├── Calendar.jsx
│   │   │   ├── Rules.jsx
│   │   │   └── Settings.jsx
│   │   ├── components/
│   │   │   ├── ItemCard.jsx
│   │   │   ├── AlertBadge.jsx
│   │   │   ├── SeverityIndicator.jsx
│   │   │   ├── SilenceModal.jsx
│   │   │   └── ...
│   │   └── services/
│   │       ├── api.js
│   │       └── websocket.js   # Real-time alert updates
│   └── public/
└── scripts/
    ├── migrate_from_tasks.py
    └── seed_rules.py
```

## Tech Stack

- **Backend**: Django 4.x + DRF
- **Database**: PostgreSQL
- **Cache/Queue**: Redis
- **Async Tasks**: Celery
- **MCP**: Python mcp library
- **Frontend**: React + TailwindCSS (mobile-first)
- **Notifications**: shizuha-notify integration

## Deployment

```yaml
# docker-compose.yml
services:
  shizuha-hub:
    build: ./shizuha-hub
    network_mode: host
    environment:
      - DATABASE_URL=postgres://...
      - REDIS_URL=redis://localhost:6379/5
      - NOTIFY_SERVICE_URL=http://localhost:8080
    volumes:
      - ./shizuha-hub:/app

  shizuha-hub-worker:
    build: ./shizuha-hub
    command: celery -A config worker -l info
    network_mode: host
    environment:
      - DATABASE_URL=postgres://...
      - REDIS_URL=redis://localhost:6379/5

  shizuha-hub-beat:
    build: ./shizuha-hub
    command: celery -A config beat -l info
    network_mode: host
```

## Summary

**Key Design Decisions:**

1. **Unified Model**: Single `Item` model with `mode` field distinguishes tasks, actions, and alerts
2. **Extensible Types**: `item_type` field allows any type (task.reminder, action.hedging.short, alert.critical, etc.)
3. **Smart Deduplication**: Configurable fingerprinting per source prevents alert storms
4. **Universal Silencing**: Silence rules work across all modes - mute alerts, snooze tasks, delay actions
5. **Escalation**: Unhandled items can escalate severity, add channels, or create follow-up actions
6. **Inhibition**: Parent alerts suppress child alerts to reduce noise during cascading failures
7. **Labels**: Prometheus-inspired label system for flexible matching in rules
8. **Backward Compatible**: Existing shizuha-pulse data migrates cleanly
9. **Generic & Standalone**: Not tied to arbiter or any specific system - any service can publish alerts
10. **MCP-First**: Claude can manage tasks, approve actions, and handle alerts through unified tools
11. **Mobile-Friendly**: React dashboard works great on phones
12. **Notification Integrated**: Uses existing shizuha-notify infrastructure

**Alerting Features (vs Alertmanager/Alerta):**

| Feature | Shizuha Hub |
|---------|-------------|
| Deduplication | ✓ Configurable fingerprinting per source |
| Silencing | ✓ Time-based, pattern-based, recurring |
| Escalation | ✓ Multi-stage with severity bump, channel addition |
| Inhibition | ✓ Parent-child alert suppression |
| Grouping | ✓ Via correlation_id and labels |
| Notifications | ✓ Via shizuha-notify (telegram, email, etc.) |
| Recovery | ✓ Auto-resolve via recovery alerts or timeout |
| AI Integration | ✓ Claude can acknowledge, silence, investigate |
| Unified Inbox | ✓ Tasks + Actions + Alerts in one place |

**Next Steps:**

1. Review and refine this design
2. Migrate shizuha-pulse to shizuha-hub
3. Add action and alert-specific fields to model
4. Implement approval rule engine
5. Implement alert deduplication service
6. Implement silence, escalation, and inhibition rule engines
7. Build MCP server with all tool categories
8. Update frontend (React web, mobile-first) with Alerts view
9. Integrate with arbiter-backend
10. Add real-time WebSocket updates for alerts
11. Document alert submission API for other services
