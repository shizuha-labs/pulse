# Workflow owner action policy

`WorkflowStatus.owner_action_policy` answers whether the current workflow state
expects owner action. `actionable` is the backward-compatible default;
`awareness` retains the visible owned item without presenting it as runnable.
Other dependency, handoff, and timer predicates can still make an `actionable`
state wait. The field is not a claim that every outgoing transition is useful.

Migration 0177 sets only `simple / resolved_elsewhere` to `awareness`.
Migration 0172 deliberately made that state nonterminal, todo-category, and
block-releasing: an escalation ask answered elsewhere need not independently
terminate. This change does not alter those properties, assignee identity,
assignment exclusions, capacity counts, links, or historical task evidence.
Generic Defer/Backlog/Escalate escape transitions do not create a new obligation.

The queue projection, focus selection, heartbeat preflight, and MCP ready count
consume the same typed policy. Awareness items stay in the active listing and
are counted separately from ready, blocked, and terminal work. The list and
detail APIs expose read-only `owner_action_policy`; workflow catalog metadata
owns it. A policy change is visible on the next request, without cache clearing
or item mutation. Reference-data reloads preserve the migration's seed.

`is_active_item` is an older mode-specific status allowlist, not an owner-action
or terminality contract. It returns false for ordinary custom todo-category
statuses too; using it as a ready filter would silently hide valid work.

Regression coverage invokes the authenticated list and heartbeat APIs, consumes
the actual serialized list through `pulse_get_my_tasks`, verifies focus ordering,
and exercises policy rearming and reference-data reload. No live task transition
is part of this repair.
