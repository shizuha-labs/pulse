# ROADMAP.md - Shizuha Pulse

Planned features and technical debt for the Pulse service.

## Current Focus

<!-- Update as priorities change -->

- Unified Item model stabilization
- Alert deduplication tuning
- Webhook integration testing

## Planned Features

### High Priority

- [ ] Calendar view for tasks
- [ ] Recurring tasks support
- [ ] Bulk operations UI

### Medium Priority

- [ ] Kanban board view
- [ ] Sprint management
- [ ] Time tracking
- [ ] Custom fields per project

### Future Considerations

- [ ] Gantt chart view
- [ ] Dependencies between items
- [ ] Workflow automation (IFTTT-style)

## Technical Debt

<!-- Track items needing refactoring -->

- [ ] Consolidate task/alert/action serializers
- [ ] Add pagination to all list endpoints
- [ ] Improve webhook retry logic

## Recently Completed

<!-- Move items here when done, with date -->

- [x] Unified Item model (tasks, actions, alerts)
- [x] Grafana/Alertmanager webhook support
- [x] Escalation rules engine
- [x] Inhibition rules for alert suppression

## Notes

<!-- Context about priorities or decisions -->

Item model unification was chosen over separate models to simplify cross-cutting concerns (permissions, subscriptions, comments).
