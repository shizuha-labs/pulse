from django.contrib import admin
from .models import (
    Item, Task, Objective, KeyResult,
    ApprovalRule, SilenceRule, EscalationRule, InhibitionRule,
    Subscription, FingerprintConfig, UserNotificationChannel,
    Status, Resolution, Workflow, WorkflowStatus, WorkflowTransition,
    WorkflowScheme, WorkflowSchemeMapping,
    # Issue types, custom fields, screens, permissions
    IssueType, IssueTypeScheme, IssueTypeSchemeMapping,
    CustomField, CustomFieldContext, CustomFieldValue,
    Screen, ScreenField, ScreenScheme,
    IssueTypeScreenScheme, IssueTypeScreenSchemeMapping,
    SecurityScheme, SecurityLevel,
    PermissionScheme, PermissionGrant,
    NotificationScheme, NotificationSchemeEvent,
    SLADefinition, BusinessCalendar, SLAInstance,
    Board, BoardQuickFilter,
    Dashboard, DashboardGadget,
    AutomationRule, AutomationRuleLog,
    ProjectConfiguration,
    AgentAvailability,
)


@admin.register(Objective)
class ObjectiveAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'org_id', 'owner_email', 'time_period', 'status', 'progress_percent', 'updated_at')
    list_filter = ('org_id', 'status', 'time_period')
    search_fields = ('title', 'description', 'owner_email')
    filter_horizontal = ('projects', 'items')


@admin.register(KeyResult)
class KeyResultAdmin(admin.ModelAdmin):
    list_display = ('id', 'objective', 'title', 'current_value', 'target_value', 'unit', 'progress_percent', 'status')
    list_filter = ('status', 'objective__org_id')
    search_fields = ('title', 'metric_name', 'objective__title')


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'mode', 'title', 'item_type', 'source',
        'assignee_email', 'due_date', 'priority', 'severity', 'status',
        'created_at'
    )
    list_display_links = ('id', 'title')
    list_filter = (
        'mode', 'status', 'priority', 'severity',
        'item_type', 'source', 'is_recurring',
        'requires_approval', 'auto_approved'
    )
    search_fields = (
        'title', 'description', 'assignee_email',
        'tags', 'source_id', 'fingerprint', 'correlation_id'
    )
    ordering = ('-created_at',)
    readonly_fields = (
        'id', 'created_at', 'updated_at', 'completed_at',
        'first_fired_at', 'last_fired_at', 'resolved_at',
        'acknowledged_at', 'approved_at', 'last_escalated_at',
        'last_reminder_sent', 'last_synced', 'fingerprint', 'occurrence_count'
    )

    fieldsets = (
        ('Identity', {
            'fields': ('id', 'mode', 'item_type', 'title', 'description')
        }),
        ('Source & Assignment', {
            'fields': (
                'source', 'source_id', 'source_url',
                'assignee_id', 'assignee_email',
                'created_by_id', 'created_by_email'
            )
        }),
        ('Status & Priority', {
            'fields': ('status', 'priority', 'severity')
        }),
        ('Timing', {
            'fields': (
                'due_date', 'start_date', 'reminder_time',
                'completed_at', 'created_at', 'updated_at'
            )
        }),
        ('Task: Recurrence', {
            'fields': (
                'is_recurring', 'frequency', 'recurrence_pattern',
                'recurrence_end_date', 'parent_item'
            ),
            'classes': ('collapse',)
        }),
        ('Task: Effort', {
            'fields': ('estimated_hours', 'actual_hours'),
            'classes': ('collapse',)
        }),
        ('Action: Payload & Approval', {
            'fields': (
                'payload', 'requires_approval', 'auto_approved',
                'approved_by', 'approved_at', 'approval_rule', 'rejection_reason'
            ),
            'classes': ('collapse',)
        }),
        ('Action: Execution', {
            'fields': (
                'callback_url', 'callback_method', 'callback_headers',
                'execution_result', 'error_message', 'retry_count', 'max_retries'
            ),
            'classes': ('collapse',)
        }),
        ('Alert: Deduplication', {
            'fields': (
                'fingerprint', 'fingerprint_fields',
                'occurrence_count', 'first_fired_at', 'last_fired_at'
            ),
            'classes': ('collapse',)
        }),
        ('Alert: Resolution', {
            'fields': (
                'resolved_at', 'resolved_by', 'resolution_note',
                'acknowledged_at', 'acknowledged_by'
            ),
            'classes': ('collapse',)
        }),
        ('Alert: Escalation', {
            'fields': (
                'escalation_level', 'last_escalated_at', 'escalation_rule'
            ),
            'classes': ('collapse',)
        }),
        ('Alert: Correlation', {
            'fields': ('correlation_id', 'parent_alert'),
            'classes': ('collapse',)
        }),
        ('Silencing', {
            'fields': (
                'silenced_until', 'silenced_by',
                'silence_reason', 'silence_rule'
            ),
            'classes': ('collapse',)
        }),
        ('Labels & Metadata', {
            'fields': ('labels', 'tags', 'metadata'),
            'classes': ('collapse',)
        }),
        ('Notifications', {
            'fields': (
                'reminder_sent', 'reminder_count',
                'last_reminder_sent', 'notification_preferences'
            ),
            'classes': ('collapse',)
        }),
        ('External Integration', {
            'fields': (
                'external_id', 'external_url',
                'sync_enabled', 'last_synced'
            ),
            'classes': ('collapse',)
        }),
    )


# Keep TaskAdmin as alias for backward compatibility
TaskAdmin = ItemAdmin


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'auto_approve', 'auto_reject', 'priority', 'is_active', 'created_at')
    list_filter = ('is_active', 'auto_approve', 'auto_reject')
    search_fields = ('name', 'description')
    ordering = ('-priority', 'name')


@admin.register(SilenceRule)
class SilenceRuleAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'is_active', 'starts_at', 'ends_at',
        'match_count', 'last_matched_at', 'created_at'
    )
    list_filter = ('is_active', 'is_recurring')
    search_fields = ('name', 'description', 'reason')
    ordering = ('-created_at',)
    readonly_fields = ('match_count', 'last_matched_at', 'created_at', 'updated_at')


@admin.register(EscalationRule)
class EscalationRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'min_severity', 'is_active', 'created_at')
    list_filter = ('is_active', 'min_severity')
    search_fields = ('name', 'description')
    ordering = ('name',)


@admin.register(InhibitionRule)
class InhibitionRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    ordering = ('name',)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('name', 'notify_type', 'is_active', 'created_at')
    list_filter = ('is_active', 'notify_type')
    search_fields = ('name', 'description')
    ordering = ('name',)


@admin.register(FingerprintConfig)
class FingerprintConfigAdmin(admin.ModelAdmin):
    list_display = ('source', 'created_at', 'updated_at')
    search_fields = ('source', 'description')
    ordering = ('source',)


@admin.register(UserNotificationChannel)
class UserNotificationChannelAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'user_id', 'channel_type', 'is_active',
        'notify_on_reminder', 'notify_on_overdue', 'notify_on_alert',
        'error_count', 'last_notification_at', 'created_at'
    )
    list_filter = ('channel_type', 'is_active', 'min_alert_severity')
    search_fields = ('name', 'user_email', 'telegram_chat_id')
    ordering = ('user_id', 'name')
    readonly_fields = ('created_at', 'updated_at', 'last_notification_at', 'last_error', 'error_count')

    fieldsets = (
        ('User', {
            'fields': ('user_id', 'user_email', 'name', 'is_active')
        }),
        ('Channel Configuration', {
            'fields': ('channel_type', 'discord_webhook_url', 'telegram_bot_token', 'telegram_chat_id')
        }),
        ('Notification Preferences', {
            'fields': (
                'notify_on_reminder', 'notify_on_overdue',
                'notify_on_alert', 'notify_on_action', 'min_alert_severity'
            )
        }),
        ('Quiet Hours', {
            'fields': ('quiet_hours_start', 'quiet_hours_end'),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('last_notification_at', 'last_error', 'error_count', 'created_at', 'updated_at')
        }),
    )


# =============================================================================
# Workflow Admin
# =============================================================================

@admin.register(Status)
class StatusAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'category', 'color', 'position', 'is_system')
    list_filter = ('category', 'is_system')
    search_fields = ('name', 'slug')
    ordering = ('position',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Resolution)
class ResolutionAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'position', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    ordering = ('position',)
    prepopulated_fields = {'slug': ('name',)}


class WorkflowStatusInline(admin.TabularInline):
    model = WorkflowStatus
    extra = 1


class WorkflowTransitionInline(admin.TabularInline):
    model = WorkflowTransition
    fk_name = 'workflow'
    extra = 1
    fields = ('name', 'from_status', 'to_status', 'position', 'is_global', 'allowed_roles', 'assignee_only')


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'initial_status', 'is_active', 'is_system')
    list_filter = ('is_active', 'is_system')
    search_fields = ('name', 'slug')
    inlines = [WorkflowStatusInline, WorkflowTransitionInline]
    prepopulated_fields = {'slug': ('name',)}


class WorkflowSchemeMappingInline(admin.TabularInline):
    model = WorkflowSchemeMapping
    extra = 1


@admin.register(WorkflowScheme)
class WorkflowSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'default_workflow', 'is_active', 'is_system')
    list_filter = ('is_active', 'is_system')
    search_fields = ('name', 'slug')
    inlines = [WorkflowSchemeMappingInline]
    prepopulated_fields = {'slug': ('name',)}


# =============================================================================
# Issue Types, Screens, Permissions, SLA Admin
# =============================================================================

@admin.register(IssueType)
class IssueTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'hierarchy_level', 'is_subtask', 'item_type_prefix', 'position', 'is_system')
    list_filter = ('hierarchy_level', 'is_subtask', 'is_system')
    search_fields = ('name', 'slug')
    ordering = ('hierarchy_level', 'position')
    prepopulated_fields = {'slug': ('name',)}


class IssueTypeSchemeMappingInline(admin.TabularInline):
    model = IssueTypeSchemeMapping
    extra = 1


@admin.register(IssueTypeScheme)
class IssueTypeSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'default_issue_type', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [IssueTypeSchemeMappingInline]
    prepopulated_fields = {'slug': ('name',)}


class CustomFieldContextInline(admin.TabularInline):
    model = CustomFieldContext
    extra = 0


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'field_type', 'is_searchable', 'position', 'is_system')
    list_filter = ('field_type', 'is_system', 'is_searchable')
    search_fields = ('name', 'slug')
    ordering = ('position', 'name')
    inlines = [CustomFieldContextInline]
    prepopulated_fields = {'slug': ('name',)}


@admin.register(CustomFieldValue)
class CustomFieldValueAdmin(admin.ModelAdmin):
    list_display = ('item', 'custom_field', 'value')
    search_fields = ('value_text',)
    raw_id_fields = ('item', 'custom_field')


class ScreenFieldInline(admin.TabularInline):
    model = ScreenField
    extra = 1


@admin.register(Screen)
class ScreenAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [ScreenFieldInline]
    prepopulated_fields = {'slug': ('name',)}


@admin.register(ScreenScheme)
class ScreenSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'create_screen', 'edit_screen', 'view_screen', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


class IssueTypeScreenSchemeMappingInline(admin.TabularInline):
    model = IssueTypeScreenSchemeMapping
    extra = 1


@admin.register(IssueTypeScreenScheme)
class IssueTypeScreenSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'default_screen_scheme', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [IssueTypeScreenSchemeMappingInline]
    prepopulated_fields = {'slug': ('name',)}


class SecurityLevelInline(admin.TabularInline):
    model = SecurityLevel
    extra = 1


@admin.register(SecurityScheme)
class SecuritySchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'default_level', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [SecurityLevelInline]
    prepopulated_fields = {'slug': ('name',)}


class PermissionGrantInline(admin.TabularInline):
    model = PermissionGrant
    extra = 1


@admin.register(PermissionScheme)
class PermissionSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [PermissionGrantInline]
    prepopulated_fields = {'slug': ('name',)}


class NotificationSchemeEventInline(admin.TabularInline):
    model = NotificationSchemeEvent
    extra = 1


@admin.register(NotificationScheme)
class NotificationSchemeAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_system')
    list_filter = ('is_system',)
    search_fields = ('name', 'slug')
    inlines = [NotificationSchemeEventInline]
    prepopulated_fields = {'slug': ('name',)}


@admin.register(SLADefinition)
class SLADefinitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'metric', 'is_active')
    list_filter = ('metric', 'is_active')
    search_fields = ('name',)
    raw_id_fields = ('project', 'calendar')


@admin.register(BusinessCalendar)
class BusinessCalendarAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'timezone')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(SLAInstance)
class SLAInstanceAdmin(admin.ModelAdmin):
    list_display = ('item', 'sla_definition', 'sla_status', 'is_breached', 'target_minutes', 'elapsed_minutes')
    list_filter = ('sla_status', 'is_breached')
    raw_id_fields = ('item', 'sla_definition')
    readonly_fields = ('created_at', 'updated_at')


class BoardQuickFilterInline(admin.TabularInline):
    model = BoardQuickFilter
    extra = 1


@admin.register(Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'board_type', 'swimlane_by', 'is_default', 'position')
    list_filter = ('board_type', 'swimlane_by', 'is_default')
    search_fields = ('name',)
    raw_id_fields = ('project',)
    inlines = [BoardQuickFilterInline]


class DashboardGadgetInline(admin.TabularInline):
    model = DashboardGadget
    extra = 1
    fields = ('gadget_type', 'title', 'column', 'position', 'is_minimized')


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner_id', 'owner_email', 'visibility', 'is_default', 'is_wallboard')
    list_filter = ('visibility', 'is_default', 'is_wallboard')
    search_fields = ('name', 'owner_email')
    inlines = [DashboardGadgetInline]


class AutomationRuleLogInline(admin.TabularInline):
    model = AutomationRuleLog
    extra = 0
    readonly_fields = ('status', 'item', 'trigger_data', 'actions_executed', 'error_message', 'duration_ms', 'created_at')
    max_num = 10
    ordering = ('-created_at',)


@admin.register(AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'trigger_type', 'is_enabled', 'execution_count', 'last_executed_at')
    list_filter = ('trigger_type', 'is_enabled')
    search_fields = ('name', 'description')
    raw_id_fields = ('project',)
    readonly_fields = ('execution_count', 'last_executed_at', 'last_error', 'created_at', 'updated_at')
    inlines = [AutomationRuleLogInline]


@admin.register(ProjectConfiguration)
class ProjectConfigurationAdmin(admin.ModelAdmin):
    list_display = ('project', 'issue_type_scheme', 'permission_scheme', 'notification_scheme')
    raw_id_fields = ('project',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AgentAvailability)
class AgentAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('email', 'active', 'status', 'updated_by', 'updated_at')
    list_filter = ('active', 'status')
    search_fields = ('email', 'reason')
    list_editable = ('active', 'status')
    readonly_fields = ('updated_at',)
