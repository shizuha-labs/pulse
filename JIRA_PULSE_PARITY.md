# Jira vs Pulse — Feature Parity Analysis

This document tracks what Jira has that Pulse currently lacks, organized by priority.

---

## P0 — Critical Gaps

### 1. ~~Generic Automation Engine (Trigger → Condition → Action)~~ ✅ IMPLEMENTED

**Jira:** Visual rule builder with triggers (issue created, status changed, field changed, comment added, sprint started/ended, scheduled cron, version released, worklog added, sub-task completed, manual trigger, incoming webhook), conditions (JQL match, field match, user in group/role, sub-tasks done, field changed from/to), actions (transition, edit fields, assign, comment, email, create sub-task, link issues, webhook, log work, add to sprint, Slack/Teams message). Supports if/else branching, smart values (`{{issue.summary}}`), per-rule audit log.

**Pulse now:** Full Jira-compatible automation engine:
- **AutomationRule** model with 13 trigger types: issue_created, issue_updated, status_changed, field_changed, comment_added, sprint_started, sprint_completed, version_released, worklog_added, subtask_completed, scheduled (cron), manual, incoming_webhook
- **Conditions**: JQL match, field match, user_in_role, subtasks_done, field_changed_from_to
- **Actions**: transition, edit_field, assign, comment, send_email, create_subtask, link_issue, webhook, add_to_sprint, slack_message, log_work
- **If/else branching** in action chains
- **Smart values**: `{{issue.summary}}`, `{{issue.assignee}}`, `{{rule.name}}` template variables
- **Rate limiting**: max_executions_per_hour per rule
- **Audit log**: AutomationRuleLog with trigger_data, actions_executed, error tracking, duration_ms

**API Endpoints:**
- `GET/POST /pulse/api/automation-rules/` — Rule CRUD
- `POST /pulse/api/automation-rules/{id}/toggle/` — Enable/disable
- `GET /pulse/api/automation-rules/{id}/logs/` — Execution audit log
- `GET /pulse/api/automation-logs/` — Global log viewer

### 2. ~~Workflow Schemes (Different Workflows per Issue Type)~~ ✅ IMPLEMENTED

**Jira:** A project has a Workflow Scheme that maps each issue type to a workflow. Bugs follow Bug Workflow (Open → Triage → Confirmed → In Progress → In Review → Verified → Closed). Stories follow Story Workflow (To Do → In Progress → Done). Epics follow Epic Workflow. One workflow can be shared across projects.

**Pulse now:** Full Jira-compatible workflow system:
- **Global Statuses** (`Status` model): 15 statuses with categories (todo/in_progress/done), shared across workflows
- **Standalone Workflows** (`Workflow` model): 3 built-in workflows (Simple, Software Development, Bug), shareable across projects
- **Workflow Schemes** (`WorkflowScheme` + `WorkflowSchemeMapping`): Maps item_types to workflows per project
- **Hierarchical type matching**: `task.bug` matches Bug Workflow, unmapped types fall back to default
- **Backward compatible**: Projects can use legacy JSON-based workflows or new Jira mode

**API Endpoints:**
- `GET/POST /pulse/api/statuses/` — Global status CRUD
- `GET/POST /pulse/api/resolutions/` — Resolution type CRUD
- `GET/POST /pulse/api/jira-workflows/` — Workflow CRUD with nested statuses/transitions
- `GET/POST /pulse/api/workflow-schemes/` — Scheme CRUD with type-to-workflow mappings
- `POST /pulse/api/items/{id}/execute-transition/` — Execute transition with screen data

---

## P1 — High Priority

### 3. ~~Resolution Types~~ ✅ IMPLEMENTED

**Jira:** Separate Resolution field (Done, Won't Fix, Duplicate, Cannot Reproduce, Incomplete). Required when closing. Resolution date tracked separately. Custom resolutions configurable.

**Pulse now:**
- `Resolution` model with 8 built-in types: Fixed, Won't Fix, Duplicate, Cannot Reproduce, Incomplete, Done, Won't Do, Known Error
- `Item.resolution` FK + `Item.resolution_date` DateTimeField
- Post-functions auto-set/clear resolution on transitions (e.g., QA Passed → set Fixed; Reopen → clear)
- Custom resolutions can be created via API

### 4. ~~Transition Screens, Validators, and Post-Functions~~ ✅ IMPLEMENTED

**Jira:** Each transition can have:
- **Screen**: Pop-up form (e.g., "Enter resolution and comment" when closing)
- **Validators**: Must pass before transition (field required, regex, permission check, subtasks done)
- **Conditions**: Who can see/use the transition (assignee only, role-based, subtask status check)
- **Post-functions**: Run after transition (set field, fire event, update parent, re-index, assign to lead, send notification, trigger webhook)

**Pulse now:** Full Jira-compatible transition features:
- **Transition Screens** (`screen_fields`): Define popup forms with required/optional fields per transition
- **Validators** (`validators`): field_required, subtasks_done, field_matches, permission_check, regex_match
- **Conditions**: `allowed_roles`, `assignee_only`, `reporter_only`, plus JSON Logic for custom rules
- **Post-Functions** (`post_functions`): set_field, clear_field, set_resolution, clear_resolution, assign_to (project_lead/reporter/user/unassign), add_comment (with template variables), send_notification, fire_webhook, copy_field, update_parent
- **Global Transitions** (`is_global`): Available from any status (like Jira's "Cancel" from anywhere)
- **Button Styling** (`button_style`): default, primary, success, warning, danger

### 5. ~~Swimlanes and Board Quick Filters~~ ✅ IMPLEMENTED

**Jira:** Board swimlanes group rows by assignee, priority, epic, story, JQL, or none. Quick filters are one-click JQL toggles on the board (e.g., "Only my issues", "Blockers only"). Card layout and card colors are configurable.

**Pulse now:** Full Jira-compatible board system:
- **Board** model with multiple boards per project (kanban/scrum)
- **Swimlanes**: none, assignee, priority, epic, issue_type, JQL-based
- **BoardQuickFilter**: One-click JQL toggle filters per board
- **Card layout**: Configurable field display (`card_layout` JSON)
- **Card colors**: Rule-based coloring by field value or JQL (`card_colors` JSON)
- **Estimation field**: Configurable (story_points, time_tracking, etc.)

**API Endpoints:**
- `GET/POST /pulse/api/boards/` — Board CRUD (filterable by project)
- `GET/POST /pulse/api/board-quick-filters/` — Quick filter CRUD

---

## P2 — Medium Priority

### 6. ~~Custom Fields (Typed, with Contexts)~~ ✅ IMPLEMENTED

**Jira:** Typed custom fields (text, number, select, multi-select, cascading select, user picker, date picker, URL, etc.). Each field has a context defining which projects/issue types it appears on. Field configuration schemes control required/optional/hidden per issue type.

**Pulse now:** Full Jira-compatible custom fields:
- **CustomField** model with 20 field types: text, textarea, number, float, select, multiselect, cascading_select, checkbox, radio, date, datetime, user, multiuser, url, labels, version, multiversion, project, group, readonly
- **CustomFieldContext**: Per-project/issue-type visibility (visible/required/hidden), options override
- **CustomFieldValue**: Per-item value storage (JSON value + text for search)
- **Validation**: regex, min/max value, max length per field type
- **Default values**: JSON-encoded per field

**API Endpoints:**
- `GET/POST /pulse/api/custom-fields/` — Field CRUD
- `GET/POST /pulse/api/custom-field-contexts/` — Context CRUD
- `GET/POST /pulse/api/custom-field-values/` — Value CRUD (filterable by item/field)

### 7. ~~Notification Schemes~~ ✅ IMPLEMENTED

**Jira:** Per-event notification routing — for each event type (created, assigned, transitioned, commented, etc.) define who gets notified: current assignee, reporter, project lead, watchers, specific group, project role. Personal preference overrides. Email integration (create issues via email). @mentions in comments. Digest/batching mode.

**Pulse now:** Full Jira-compatible notification schemes:
- **NotificationScheme** model with per-project assignment
- **NotificationSchemeEvent**: 19 event types (issue_created, issue_updated, issue_assigned, issue_resolved, issue_closed, issue_reopened, issue_deleted, issue_moved, comment_created/edited/deleted, attachment_created, worklog_created/updated/deleted, sprint_started/completed, version_released, status_changed)
- **8 recipient types**: assignee, reporter, project_lead, watchers, role, group, user, component_lead

**API Endpoints:**
- `GET/POST /pulse/api/notification-schemes/` — Scheme CRUD with nested events

### 8. ~~Bulk Operations for Tasks~~ ✅ IMPLEMENTED

**Jira:** Bulk transition, assign, edit fields, move to sprint, delete. Select issues matching a JQL query and apply operations to all.

**Pulse now:** Full Jira-compatible bulk operations:
- **Bulk transition**: Change status for multiple items at once
- **Bulk assign**: Assign/unassign multiple items
- **Bulk edit**: Update priority, severity, labels, tags, due_date across items
- **Bulk move to sprint**: Move items to a sprint
- **Bulk delete**: Delete multiple items

**API Endpoint:**
- `POST /pulse/api/items/bulk/` — Unified bulk operation endpoint (operation: transition/assign/edit/move_sprint/delete)

### 9. ~~Dashboard Gadgets~~ ✅ IMPLEMENTED

**Jira:** Configurable gadget-based dashboards (per user or shared). Gadgets: filter results, pie chart, workload, sprint burndown, velocity chart, cumulative flow diagram, created vs resolved, two-dimensional filter, time since chart, control chart (cycle time). Wallboard mode for large screens.

**Pulse now:** Full Jira-compatible dashboards:
- **Dashboard** model with visibility (private/project/org/public), wallboard mode, layout config
- **DashboardGadget**: 21 gadget types (filter_results, pie_chart, two_dimensional, created_vs_resolved, sprint_burndown, sprint_health, velocity_chart, cumulative_flow, control_chart, workload, time_since, activity_stream, assigned_to_me, watched_issues, text, road_map, recently_created, resolution_time, average_age, time_tracking, sla_met_vs_breached)
- **Gadget config**: Per-gadget JSON configuration (filter_id, project, stat_type, board_id, etc.)
- **Auto-refresh**: Configurable refresh interval per gadget

**API Endpoints:**
- `GET/POST /pulse/api/dashboards/` — Dashboard CRUD
- `GET/POST /pulse/api/dashboard-gadgets/` — Gadget CRUD

### 10. ~~Issue Type Schemes~~ ✅ IMPLEMENTED

**Jira:** Issue Type Scheme defines which issue types are available per project. Sub-task types are separate. Issue type hierarchy: Epic > Story > Task > Sub-task (configurable depth). Issue type screen schemes: different form layouts per issue type per operation (create/edit/view).

**Pulse now:** Full Jira-compatible issue type system:
- **IssueType** model: 7 default types (Epic, Story, Bug, Task, Sub-task, Improvement, New Feature)
- **Hierarchy levels**: 0=Epic, 1=Standard (Story/Bug), 2=Task, 3=Sub-task
- **IssueTypeScheme** + **IssueTypeSchemeMapping**: Which issue types available per project
- **IssueTypeScreenScheme** + **IssueTypeScreenSchemeMapping**: Different field layouts per issue type per operation
- **ScreenScheme**: Maps create/edit/view operations to Screen definitions
- **Screen** + **ScreenField**: Tab-based field layout with position ordering

**API Endpoints:**
- `GET/POST /pulse/api/issue-types/` — Issue type CRUD
- `GET/POST /pulse/api/issue-type-schemes/` — Scheme CRUD with mappings
- `GET/POST /pulse/api/screens/` — Screen CRUD with fields
- `GET/POST /pulse/api/screen-schemes/` — Screen scheme CRUD
- `GET/POST /pulse/api/issue-type-screen-schemes/` — Issue type screen scheme CRUD

---

## P3 — Lower Priority

### 11. ~~Screen Schemes (Field Layout per Issue Type)~~ ✅ IMPLEMENTED

**Jira:** Different field layouts for create vs edit vs view screens. Mapped per issue type. Controls which fields appear and in what order.

**Pulse now:** Full Jira-compatible screen system (see #10 above for details).

### 12. ~~Per-Issue Security Levels~~ ✅ IMPLEMENTED

**Jira:** Security schemes with levels (Internal Only, Confidential, Public). Set per issue. Controls visibility beyond project access.

**Pulse now:** Full Jira-compatible security levels:
- **SecurityScheme** model with default level
- **SecurityLevel**: Named levels with member access control (role, user, group, project_lead, reporter, assignee)
- Per-project assignment via ProjectConfiguration

**API Endpoints:**
- `GET/POST /pulse/api/security-schemes/` — Scheme CRUD with nested levels

### 13. ~~SLA Management~~ ✅ IMPLEMENTED

**Jira (Service Management):** SLA definitions with target response/resolution times per priority. Business hour calendars with holidays. SLA metrics (time to first response, time to resolution). Breach warnings before/after breach.

**Pulse now:** Full Jira Service Management-compatible SLAs:
- **SLADefinition**: 5 metrics (time_to_first_response, time_to_resolution, time_to_assignee, time_in_status, time_to_close)
- **Targets per priority**: JSON mapping (e.g., `{"urgent": 60, "high": 240}` in minutes)
- **BusinessCalendar**: Working hours per weekday, holiday list, timezone support
- **SLAInstance**: Per-item tracking with elapsed_minutes, remaining_minutes, breach detection
- **Status-based conditions**: Start/pause/stop by status slug
- **JQL filter**: Which items the SLA applies to

**API Endpoints:**
- `GET/POST /pulse/api/sla-definitions/` — SLA CRUD (filterable by project)
- `GET/POST /pulse/api/business-calendars/` — Calendar CRUD
- `GET /pulse/api/sla-instances/` — Instance viewer (read-only, filterable)

### 14. ~~Permission Schemes (Granular)~~ ✅ IMPLEMENTED

**Jira:** 30+ granular permissions per project role (Create Issues, Edit Issues, Transition Issues, Assign Issues, Close Issues, Delete Issues, Manage Sprints, Move Issues, Schedule Issues, Set Security, etc.). Global permissions. Workflow permissions (per-transition).

**Pulse now:** Full Jira-compatible granular permissions:
- **PermissionScheme** model with per-project assignment
- **PermissionGrant**: 33 Jira-compatible permissions (browse_projects, create_issues, edit_issues, delete_issues, close_issues, transition_issues, assign_issues, assignable_user, move_issues, schedule_issues, set_security, modify_reporter, link_issues, bulk_change, add_comments, edit_comments, edit_own_comments, delete_comments, delete_own_comments, create_attachments, delete_attachments, delete_own_attachments, work_on_issues, edit_own_worklogs, edit_all_worklogs, delete_own_worklogs, delete_all_worklogs, manage_watchers, view_voters_and_watchers, manage_sprints, view_read_only_workflow, view_dev_tools, administer_projects)
- **8 holder types**: role, group, user, project_lead, reporter, assignee, anyone, logged_in

**API Endpoints:**
- `GET/POST /pulse/api/permission-schemes/` — Scheme CRUD with nested grants

### 15. ~~Multiple Boards per Project~~ ✅ IMPLEMENTED

**Jira:** A project can have multiple boards with different filters, column configurations, and swimlane settings.

**Pulse now:** Full support (see #5 above for details). Multiple boards per project with independent filter JQL, swimlanes, card layout, and quick filters.

---

## Project Configuration

All Jira-compatible schemes are tied together via `ProjectConfiguration` (OneToOne to Project):
- `issue_type_scheme` — Which issue types are available
- `issue_type_screen_scheme` — Field layouts per issue type
- `security_scheme` — Per-issue security levels
- `permission_scheme` — Granular permission mapping
- `notification_scheme` — Per-event notification routing

**API Endpoint:**
- `GET/POST /pulse/api/project-configurations/` — Central project configuration

---

## Current Pulse Strengths (Features Jira Lacks or Charges Extra For)

| Feature | Pulse | Jira |
|---------|-------|------|
| Alert management (firing/ack/resolved/silenced) | Built-in | Requires Opsgenie ($) |
| Alert deduplication + fingerprinting | Built-in | Requires Opsgenie ($) |
| Escalation rules with multi-stage | Built-in | Requires Opsgenie ($) |
| Inhibition rules (cascade noise reduction) | Built-in | Not available |
| Grafana/Prometheus webhook ingestion | Built-in | Requires integration app |
| Action approval workflows | Built-in (ApprovalRule) | Requires Automation + custom |
| Silence rules with recurrence | Built-in | Not available |
| AI agent integration (MCP tools) | Built-in | Requires third-party |
| JQL parser | Built-in | Built-in (parity) |
| Unified task/action/alert model | Innovative | Separate products |
