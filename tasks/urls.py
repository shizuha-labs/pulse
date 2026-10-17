from django.urls import path, include
from rest_framework.routers import DefaultRouter
from alertdelivery.views import PrometheusAlertmanagerDurableWebhookView
from .views import (
    ItemViewSet, TaskViewSet, DeploymentEventViewSet,
    VerificationRequirementManifestViewSet, DeploymentEvidenceViewSet,
    ObjectiveViewSet, KeyResultViewSet,
    ExternalMcpToolsView, ExternalMcpCallView,
    ApprovalRuleViewSet, SilenceRuleViewSet, EscalationRuleViewSet, InhibitionRuleViewSet,
    SubscriptionViewSet, FingerprintConfigViewSet,
    UserNotificationChannelViewSet, NotificationViewSet,
    ProjectViewSet, SprintViewSet,
    CommentViewSet, AttachmentViewSet, ItemLinkViewSet, WebhookKeyViewSet,
    StatuszView,
    LaneHealthView,
    AlertSubmitView, AlertBulkAcknowledgeView, AlertBulkResolveView, AlertBulkDeleteView,
    TaskBulkCompleteView, TaskBulkUpdateView, TaskBulkDeleteView,
    ProjectBulkDeleteView,
    ActionSubmitView,
    GrafanaWebhookView, PrometheusAlertmanagerWebhookView, GenericWebhookView,
    GitHubWebhookView,
    InternalUserLifecycleEventView, InternalUserUnassignView,
    InternalUserProjectMembershipsView,
    InternalQueueLaneListView,
    InternalQueueLaneProviderWaitClearView,
    InternalQueueLaneProviderWaitView,
    InternalAgentLifecycleView,
    AgentProjectMembershipListView, AgentOutputAttributionAuditView,
    AgentSelfAvailabilityView,
    TaskEventListView,  # PLS-171
    TaskDraftProposalView, TaskDraftConfirmView, TaskDraftRejectView,  # PLS-306
    TeamListView, ResolveAssigneeView, RoutingAuthorityCommandCreateView,
    RoutingAuthorityCommandApproveView, CompanyStateSnapshotView, OrgChartSnapshotView,
    TeamRosterMemberView,
    TeamRosterListCreateView, TeamRosterDetailView, InternalTeamRosterView,
    InternalTeamRosterMemberMutationView,
    InternalTeamCapabilitiesView,
    # Phase 3 ViewSets
    ComponentViewSet, VersionViewSet, WorkLogViewSet,
    ProjectWorkflowViewSet,
    BoardColumnViewSet, SavedFilterViewSet,
    # Workflow ViewSets
    StatusViewSet, ResolutionViewSet,
    WorkflowViewSet, WorkflowTransitionViewSet,
    WorkflowSchemeViewSet, TransitionExecuteView, ItemAvailableTransitionsView, ItemRunNowView, ItemSnoozeView, ItemChangeWorkflowView,
    ItemReturnToLaneView,
    # Issue types, screens, permissions, SLA, boards, dashboards, automation
    IssueTypeViewSet, IssueTypeSchemeViewSet,
    CustomFieldViewSet, CustomFieldContextViewSet, CustomFieldValueViewSet,
    ScreenViewSet, ScreenSchemeViewSet, IssueTypeScreenSchemeViewSet,
    SecuritySchemeViewSet, PermissionSchemeViewSet, NotificationSchemeViewSet,
    SLADefinitionViewSet, BusinessCalendarViewSet, SLAInstanceViewSet,
    BoardViewSet, BoardQuickFilterViewSet,
    DashboardViewSet, DashboardGadgetViewSet,
    AutomationRuleViewSet, AutomationRuleLogViewSet,
    ProjectConfigurationViewSet,
    ItemBulkOperationView,
    CustomerPortalRequestListCreateView, CustomerPortalRequestDetailView,
    CustomerPortalRequestVoteView, CustomerPortalRequestCommentListCreateView,
    CustomerPortalRequestApproveView,
    pulse_metrics,
    AiRecapListCreateView, AiRecapDetailView, AiRecapFeedbackView,
)
from .membership_v2 import ProjectMembershipCandidatesResolveV2View  # PLAT-5343 slice 1

router = DefaultRouter()

# Main item endpoint (unified tasks, actions, alerts)
router.register(r'items', ItemViewSet, basename='item')

# Backward compatible task endpoint (alias for items with mode=task)
router.register(r'tasks', TaskViewSet, basename='task')

# CI/CD deployment ledger endpoint (PLS-183)
router.register(r'deployment-events', DeploymentEventViewSet, basename='deployment-event')
router.register(
    r'verification-requirements', VerificationRequirementManifestViewSet,
    basename='verification-requirement',
)
router.register(r'deployment-evidence', DeploymentEvidenceViewSet, basename='deployment-evidence')

# OKR / goal alignment (PLS-272)
router.register(r'objectives', ObjectiveViewSet, basename='objective')
router.register(r'key-results', KeyResultViewSet, basename='key-result')

# Rule management endpoints
router.register(r'rules/approval', ApprovalRuleViewSet, basename='approval-rule')
router.register(r'rules/silence', SilenceRuleViewSet, basename='silence-rule')
router.register(r'rules/escalation', EscalationRuleViewSet, basename='escalation-rule')
router.register(r'rules/inhibition', InhibitionRuleViewSet, basename='inhibition-rule')

# Subscription and config endpoints
router.register(r'subscriptions', SubscriptionViewSet, basename='subscription')
router.register(r'fingerprint-configs', FingerprintConfigViewSet, basename='fingerprint-config')

# User notification channels
router.register(r'notification-channels', UserNotificationChannelViewSet, basename='notification-channel')

# Project endpoint (Organizations are managed by shizuha-admin at /admin/api/organizations/)
router.register(r'projects', ProjectViewSet, basename='project')

# Sprint endpoint (Agile sprints)
router.register(r'sprints', SprintViewSet, basename='sprint')

# Comments endpoint
router.register(r'comments', CommentViewSet, basename='comment')

# Attachments endpoint
router.register(r'attachments', AttachmentViewSet, basename='attachment')

# Item links endpoint (for deleting links by ID)
router.register(r'links', ItemLinkViewSet, basename='link')

# Webhook keys endpoint (user-specific)
router.register(r'webhook-keys', WebhookKeyViewSet, basename='webhook-key')

# Phase 3: Components and Versions
router.register(r'components', ComponentViewSet, basename='component')
router.register(r'versions', VersionViewSet, basename='version')

# Phase 3: Work Logs (time tracking)
router.register(r'work-logs', WorkLogViewSet, basename='work-log')

# Legacy project workflows (deprecated — use /workflows/ for global workflows)
router.register(r'project-workflows', ProjectWorkflowViewSet, basename='project-workflow')

# Phase 3: Board Configuration
router.register(r'board-columns', BoardColumnViewSet, basename='board-column')

# Phase 3: Saved Filters (JQL)
router.register(r'saved-filters', SavedFilterViewSet, basename='saved-filter')

# In-app notification history
router.register(r'notifications', NotificationViewSet, basename='notification')

# Workflow System — statuses, workflows, transitions, schemes
router.register(r'statuses', StatusViewSet, basename='status')
router.register(r'resolutions', ResolutionViewSet, basename='resolution')
router.register(r'workflows', WorkflowViewSet, basename='workflow')
router.register(r'transitions', WorkflowTransitionViewSet, basename='transition')
router.register(r'workflow-schemes', WorkflowSchemeViewSet, basename='workflow-scheme')

# Issue Types & Schemes
router.register(r'issue-types', IssueTypeViewSet, basename='issue-type')
router.register(r'issue-type-schemes', IssueTypeSchemeViewSet, basename='issue-type-scheme')

# Custom Fields
router.register(r'custom-fields', CustomFieldViewSet, basename='custom-field')
router.register(r'custom-field-contexts', CustomFieldContextViewSet, basename='custom-field-context')
router.register(r'custom-field-values', CustomFieldValueViewSet, basename='custom-field-value')

# Screens & Screen Schemes
router.register(r'screens', ScreenViewSet, basename='screen')
router.register(r'screen-schemes', ScreenSchemeViewSet, basename='screen-scheme')
router.register(r'issue-type-screen-schemes', IssueTypeScreenSchemeViewSet, basename='issue-type-screen-scheme')

# Security, Permissions, Notifications
router.register(r'security-schemes', SecuritySchemeViewSet, basename='security-scheme')
router.register(r'permission-schemes', PermissionSchemeViewSet, basename='permission-scheme')
router.register(r'notification-schemes', NotificationSchemeViewSet, basename='notification-scheme')

# SLA Management
router.register(r'sla-definitions', SLADefinitionViewSet, basename='sla-definition')
router.register(r'business-calendars', BusinessCalendarViewSet, basename='business-calendar')
router.register(r'sla-instances', SLAInstanceViewSet, basename='sla-instance')

# Boards (multiple per project, swimlanes, quick filters)
router.register(r'boards', BoardViewSet, basename='board')
router.register(r'board-quick-filters', BoardQuickFilterViewSet, basename='board-quick-filter')

# Dashboards & Gadgets
router.register(r'dashboards', DashboardViewSet, basename='dashboard')
router.register(r'dashboard-gadgets', DashboardGadgetViewSet, basename='dashboard-gadget')

# Automation Engine
router.register(r'automation-rules', AutomationRuleViewSet, basename='automation-rule')
router.register(r'automation-logs', AutomationRuleLogViewSet, basename='automation-log')

# Project Configuration
router.register(r'project-configurations', ProjectConfigurationViewSet, basename='project-configuration')

urlpatterns = [
    # Prometheus metrics (internal scrape endpoint)
    path('metrics/', pulse_metrics, name='pulse-metrics'),

    # PLS-307: citation-backed AI sprint/project recaps
    path('ai/recaps/', AiRecapListCreateView.as_view(), name='ai-recap-list-create'),
    path('ai/recaps/<uuid:run_id>/', AiRecapDetailView.as_view(), name='ai-recap-detail'),
    path('ai/recaps/<uuid:run_id>/feedback/', AiRecapFeedbackView.as_view(), name='ai-recap-feedback'),

    # Alert-specific endpoints (before router to avoid conflicts)
    path('statusz/', StatuszView.as_view(), name='mechanism-liveness-statusz'),
    path('lane-health/', LaneHealthView.as_view(), name='lane-health'),
    path('alerts/', AlertSubmitView.as_view(), name='alert-submit'),
    path('alerts/bulk-acknowledge/', AlertBulkAcknowledgeView.as_view(), name='alert-bulk-acknowledge'),
    path('alerts/bulk-resolve/', AlertBulkResolveView.as_view(), name='alert-bulk-resolve'),
    path('alerts/bulk-delete/', AlertBulkDeleteView.as_view(), name='alert-bulk-delete'),

    # Task bulk operations (before router to avoid conflicts)
    path('tasks/bulk-complete/', TaskBulkCompleteView.as_view(), name='task-bulk-complete'),
    path('tasks/bulk-update/', TaskBulkUpdateView.as_view(), name='task-bulk-update'),
    path('tasks/bulk-delete/', TaskBulkDeleteView.as_view(), name='task-bulk-delete'),

    # Project bulk operations
    path('projects/bulk-delete/', ProjectBulkDeleteView.as_view(), name='project-bulk-delete'),

    # Action-specific endpoints
    path('actions/submit/', ActionSubmitView.as_view(), name='action-submit'),

    # Execute transition on an item
    # Support both numeric ID and item_key (e.g., AT-37) in URL
    path('items/<str:item_id>/execute-transition/', TransitionExecuteView.as_view(), name='item-execute-transition'),
    path('items/<str:item_id>/available-transitions/', ItemAvailableTransitionsView.as_view(), name='item-available-transitions'),
    # PLAT-8222 suggestion 3: sanctioned cannot-execute → return-to-lane self-service
    path('items/<str:item_id>/return-to-lane/', ItemReturnToLaneView.as_view(), name='item-return-to-lane'),
    path('items/<str:item_id>/run-now/', ItemRunNowView.as_view(), name='item-run-now'),
    path('items/<str:item_id>/snooze/', ItemSnoozeView.as_view(), name='item-snooze'),
    path('items/<str:item_id>/change-workflow/', ItemChangeWorkflowView.as_view(), name='item-change-workflow'),

    # Bulk operations
    path('items/bulk/', ItemBulkOperationView.as_view(), name='item-bulk-operation'),

    # External webhook integrations (public, auth via key in URL)
    path('webhooks/grafana/<str:key>/', GrafanaWebhookView.as_view(), name='webhook-grafana'),
    path('webhooks/alertmanager/<str:key>/', PrometheusAlertmanagerWebhookView.as_view(), name='webhook-alertmanager'),
    path('webhooks/alertmanager-durable/<str:key>/', PrometheusAlertmanagerDurableWebhookView.as_view(), name='webhook-alertmanager-durable'),
    path('webhooks/generic/<str:key>/', GenericWebhookView.as_view(), name='webhook-generic'),
    # GitHub PR state sync — validates via X-Hub-Signature-256 / GITHUB_WEBHOOK_SECRET
    path('webhooks/github/', GitHubWebhookView.as_view(), name='webhook-github'),

    # Teams endpoint (for assignment dropdown)
    path('teams/', TeamListView.as_view(), name='team-list'),
    # Resolve a team/assignment_group -> current concrete agent (read-only; used by
    # the alert notifier to DM the on-call agent without hardcoding a roster).
    path('teams/resolve-assignee/', ResolveAssigneeView.as_view(), name='team-resolve-assignee'),
    path('routing-authority/commands/', RoutingAuthorityCommandCreateView.as_view(),
         name='routing-authority-command-create'),
    path('routing-authority/commands/<uuid:command_id>/approve/',
         RoutingAuthorityCommandApproveView.as_view(),
         name='routing-authority-command-approve'),

    # Org Chart 2.0 snapshot endpoint (PLAT-320)
    path('admin/org-chart/snapshot/', OrgChartSnapshotView.as_view(), name='org-chart-snapshot'),
    # Canonical team routing/display roster membership CRUD.
    path('admin/team-roster/members/', TeamRosterMemberView.as_view(), name='team-roster-members'),

    # HIVE-237: TeamRoster lifecycle CRUD (create/rename/delete/set-lead)
    path('admin/team-roster/', TeamRosterListCreateView.as_view(), name='team-roster-list'),
    path('admin/team-roster/<str:team_name>/', TeamRosterDetailView.as_view(), name='team-roster-detail'),

    # PLAT-4240: authenticated FRONT-DOOR route for the executive company-state
    # snapshot. The view enforces scope/staff/org-lead authorization itself
    # (PLAT-1197), so it does not rely on the /internal/ ingress guard — which
    # deliberately 404-masks ingress-borne /api/internal/* traffic and made the
    # PLAT-4139 "public" route unreachable by design. Canonical public URL:
    # /shizuha-pulse/api/company-state/snapshot/?schema_version=1&org_slug=...
    path('company-state/snapshot/', CompanyStateSnapshotView.as_view(), name='company-state-snapshot-frontdoor'),

    # Internal endpoints (service-to-service, secured by network)
    path('internal/company-state/snapshot/', CompanyStateSnapshotView.as_view(), name='company-state-snapshot'),
    path('internal/user-lifecycle-events/', InternalUserLifecycleEventView.as_view(), name='internal-user-lifecycle-events'),
    path('internal/agent-lifecycle/', InternalAgentLifecycleView.as_view(), name='internal-agent-lifecycle'),
    path('internal/users/<int:user_id>/unassign/', InternalUserUnassignView.as_view(), name='internal-user-unassign'),
    path('internal/users/<int:user_id>/project-memberships/', InternalUserProjectMembershipsView.as_view(), name='internal-user-project-memberships'),
    # PLAT-10083 canonical seat-scoped provider_wait surface (writer-anchored:
    # shizuha-beta src/gateway/provider-wait-writer.ts @ 000cccca, rei review 910927).
    path('internal/queue-lane/', InternalQueueLaneListView.as_view(), name='internal-queue-lane'),
    path('internal/queue-lane/provider-wait/', InternalQueueLaneProviderWaitView.as_view(), name='internal-queue-lane-provider-wait'),
    path('internal/queue-lane/provider-wait:clear/', InternalQueueLaneProviderWaitClearView.as_view(), name='internal-queue-lane-provider-wait-clear'),
    # PLAT-5343 slice 1: acyclic v2 snapshot (HLD §4.2). Fail-closed auth in-view;
    # zero outbound calls, ≤3 local queries. v1 above is retained until §4.4 step 5.
    path('internal/v2/project-membership-candidates:resolve', ProjectMembershipCandidatesResolveV2View.as_view(), name='internal-v2-project-membership-candidates-resolve'),
    path('internal/team-roster/', InternalTeamRosterView.as_view(), name='internal-team-roster'),
    path('internal/team-roster/members/', InternalTeamRosterMemberMutationView.as_view(), name='internal-team-roster-members'),
    path('internal/team-capabilities/', InternalTeamCapabilitiesView.as_view(), name='internal-team-capabilities'),

    # EVOL-44: least-privilege aggregate output attribution for daily fleet inspection.
    path('audit/agent-output-attribution/', AgentOutputAttributionAuditView.as_view(), name='agent-output-attribution-audit'),

    # EVOL-42: agent×project affinity membership feed for monitoring/exporters.
    path('agent-project-memberships/', AgentProjectMembershipListView.as_view(), name='agent-project-memberships'),

    # Agent self-availability endpoint (SCLI-92)
    path('agent-availability/self/', AgentSelfAvailabilityView.as_view(), name='agent-self-availability'),

    # PLS-171: live-update cursor-delta feed
    path('events/', TaskEventListView.as_view(), name='task-events'),

    # PLS-273: public customer request intake portal. Public read/create/vote/comment
    # endpoints are project-scoped; conversion to an internal Pulse item remains
    # authenticated and project-permission gated.
    path('customer-portal/<str:project_ref>/requests/', CustomerPortalRequestListCreateView.as_view(), name='customer-portal-requests'),
    path('customer-portal/<str:project_ref>/requests/<uuid:request_id>/', CustomerPortalRequestDetailView.as_view(), name='customer-portal-request-detail'),
    path('customer-portal/<str:project_ref>/requests/<uuid:request_id>/vote/', CustomerPortalRequestVoteView.as_view(), name='customer-portal-request-vote'),
    path('customer-portal/<str:project_ref>/requests/<uuid:request_id>/comments/', CustomerPortalRequestCommentListCreateView.as_view(), name='customer-portal-request-comments'),
    path('customer-portal/requests/<uuid:request_id>/approve/', CustomerPortalRequestApproveView.as_view(), name='customer-portal-request-approve'),

    # PLS-306: AI Work Intake — natural-language task draft/confirm pipeline
    path('ai-work-intake/drafts/', TaskDraftProposalView.as_view(), name='ai-work-intake-drafts'),
    path('ai-work-intake/drafts/<uuid:pk>/', TaskDraftProposalView.as_view(), name='ai-work-intake-draft-detail'),
    path('ai-work-intake/drafts/<uuid:pk>/confirm/', TaskDraftConfirmView.as_view(), name='ai-work-intake-draft-confirm'),
    path('ai-work-intake/drafts/<uuid:pk>/reject/', TaskDraftRejectView.as_view(), name='ai-work-intake-draft-reject'),

    # PLS-304/PLS-308: productized tenant-scoped external MCP gateway.
    path('external-mcp/tools/', ExternalMcpToolsView.as_view(), name='external-mcp-tools'),
    path('external-mcp/call/', ExternalMcpCallView.as_view(), name='external-mcp-call'),

    # Include router URLs (after specific paths)
    path('', include(router.urls)),
]
