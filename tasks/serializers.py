from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from rest_framework import serializers
from .secret_scanner import redact_secrets
from .workflow_articles import normalize_knowledge_articles
from .workflow_json_fields import normalize_json_list_field
from .models import (
    Item, Task, DeploymentEvent,  # Task is alias for Item
    VerificationRequirementManifest, DeploymentEvidence,
    Objective, KeyResult,
    ApprovalRule, SilenceRule, EscalationRule, InhibitionRule,
    Subscription, FingerprintConfig, UserNotificationChannel,
    Notification,
    Project, ProjectMembership, Sprint,
    Comment, WebhookKey,
    Attachment, ItemActivity, ItemLink, ItemWatcher, CommentReaction,
    # Phase 3 models
    Component, Version, WorkLog,
    ProjectWorkflow, WorkflowTransition,
    BoardColumn, SavedFilter,
    # Workflow models
    Status, Resolution, Workflow, WorkflowStatus,
    TaskDraftProposal,
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
    AiGenerationRun,
)
from django.utils import timezone
from django.core.cache import cache
from .services.recurring import validate_recurring_configuration
from .user_service import user_service


def _validate_source_authority_candidate(*, resource, candidate):
    """Fail the serializer before save unless the provider approves one snapshot."""
    from .source_authority_contracts import (
        SourceAuthorityContractError,
        validate_source_authority_contract,
    )

    try:
        validate_source_authority_contract(
            'pulse.workflow_knowledge',
            resource=resource,
            candidate=candidate,
            expected_consumer='pulse.workflow_knowledge',
        )
    except SourceAuthorityContractError as exc:
        raise serializers.ValidationError({
            'source_authority_contract': str(exc),
        }) from exc


def _dict_field(data, key, default=''):
    """Safely read ``key`` from a cross-service result expected to be dict-or-None.

    A *truthy non-dict* result (a malformed upstream payload in prod, or a
    ``MagicMock`` in tests when a service singleton like ``admin_service`` is
    patched but the accessed method is left unstubbed) must NEVER flow into the
    serialized response. DRF's ``JSONEncoder.default()`` falls back to
    ``obj.tolist()`` / ``iter(obj)`` on any non-JSON object, and a MagicMock
    answers every attribute/call with a fresh child mock — an unbounded
    generative recursion that appends to ``mock_calls`` until the process
    OOM-kills (PLS-252: the CI test-gate 12–24GB blowup that survived three
    ceiling bumps; PLS-231 fixed the sibling ``_resolve_user_ref`` leak site but
    not this one). Fail closed to ``default`` for anything that is not a dict.
    """
    return data.get(key, default) if isinstance(data, dict) else default


def _agent_short_name(email):
    email = (email or '').strip().lower()
    if email.endswith('@agents.shizuha.io'):
        return email[:-len('@agents.shizuha.io')]
    if email.endswith('@shizuha.com'):
        return email[:-len('@shizuha.com')]
    return email.split('@', 1)[0] if '@' in email else email


def _agent_health_for_email(email):
    """Best-effort assignee liveness for serializers; never blocks item rendering."""
    short = _agent_short_name(email)
    if not short:
        return None
    assignment_available = None
    try:
        from .services.workflow_service import WorkflowService
        assignment_available = WorkflowService._is_agent_available_for_assignment(email)
    except Exception:
        assignment_available = None
    try:
        from .views import _scrape_agent_health
        health = _scrape_agent_health().get(short)
    except Exception:
        health = None
    if not health:
        if assignment_available is False:
            return {
                'agent': short,
                'status': 'unavailable',
                'is_online': False,
                'last_activity_s': None,
                'auth_errors': 0,
            }
        return None
    status = health.get('status') or 'unknown'
    if status == 'active' and assignment_available is False:
        status = 'unavailable'
    return {
        'agent': short,
        'status': status,
        'is_online': status == 'active',
        'last_activity_s': health.get('last_activity_s'),
        'auth_errors': health.get('auth_errors', 0),
    }


_ENTITY_KEY_SETTING_NAMES = (
    'entity_key',
    'mini_org_key',
    'organization_key',
    'org_key',
    'company_key',
)

_KNOWN_PROJECT_ENTITY_KEYS = {
    'BKSG': 'SG',
    'BKST': 'ST',
}


def _clean_entity_key(value):
    if not value:
        return ''
    key = ''.join(ch for ch in str(value).strip().upper() if ch.isalnum())
    return key[:4]


def _project_entity_key(project):
    """Return a compact org/entity key for task cards, without requiring a migration."""
    if not project:
        return ''

    settings = project.settings if isinstance(project.settings, dict) else {}
    for setting_name in _ENTITY_KEY_SETTING_NAMES:
        explicit_key = _clean_entity_key(settings.get(setting_name))
        if explicit_key:
            return explicit_key

    project_key = _clean_entity_key(project.key)
    if project_key in _KNOWN_PROJECT_ENTITY_KEYS:
        return _KNOWN_PROJECT_ENTITY_KEYS[project_key]

    if project_key.startswith('BKS') and len(project_key) > 3:
        return project_key[3:7]

    return ''


# =============================================================================
# Item (Task) Serializers - Backward Compatible
# =============================================================================

class OwnerActionPolicyMixin:
    def get_owner_action_policy(self, obj):
        if hasattr(obj, '_queue_owner_awareness'):
            return 'awareness' if obj._queue_owner_awareness else 'actionable'
        if not hasattr(self, '_owner_awareness_catalog'):
            memberships = WorkflowStatus.objects.filter(
                owner_action_policy='awareness',
            ).values_list('workflow__slug', 'workflow__name', 'status__slug')
            self._owner_awareness_catalog = {
                (workflow.casefold(), status)
                for slug, name, status in memberships
                for workflow in (slug, name)
            }
        identity = ((obj.workflow_name or '').casefold(), obj.status)
        return 'awareness' if identity in self._owner_awareness_catalog else 'actionable'


class ItemSerializer(OwnerActionPolicyMixin, serializers.ModelSerializer):
    """
    Comprehensive serializer for Item model (unified tasks, actions, alerts).
    """
    # Computed fields
    is_overdue = serializers.SerializerMethodField()
    days_until_due = serializers.SerializerMethodField()
    is_silenced_now = serializers.SerializerMethodField()
    is_active_item = serializers.SerializerMethodField()
    tags_list = serializers.SerializerMethodField()
    # Status category from the Status table ('todo' / 'in_progress' / 'done').
    # Used by the frontend to apply done-styling (strike-through, dimmed)
    # without duplicating the list of terminal status slugs.
    status_category = serializers.SerializerMethodField()
    owner_action_policy = serializers.SerializerMethodField()
    # PLS-792: machine-readable recurring-cycle freshness payload — exposes the
    # current cycle_revision/generation/event_id so a client can Refresh->Run Now
    # without private implementation knowledge. Absent for non-recurring items.
    recurring_cycle = serializers.SerializerMethodField()

    # User name fields - fetched from shizuha-id (source of truth)
    assignee_name = serializers.SerializerMethodField()
    assignee_health = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    assignment_team = serializers.SerializerMethodField()
    # Reporter TEAM ("where the task came from") — team-based like assignment_group,
    # so the UI shows a team and back-to-reporter routing resolves a live member.
    reporter_group = serializers.SerializerMethodField()

    # Project fields (read-only computed)
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    organization_id = serializers.IntegerField(source='project.org_id', read_only=True)
    organization_name = serializers.SerializerMethodField()
    organization_slug = serializers.SerializerMethodField()
    entity_key = serializers.SerializerMethodField()
    # Immutable CI/source evidence. ``affects_versions`` remains the release
    # version relation; this field answers "which exact Origin run/commit?".
    origin_observations = serializers.SerializerMethodField()
    append_origin_observations = serializers.JSONField(write_only=True, required=False)
    containment_decision = serializers.SerializerMethodField()
    external_wait = serializers.SerializerMethodField()

    # Parent item fields (read-only computed)
    parent_item_key = serializers.CharField(source='parent_item.item_key', read_only=True, default=None)
    parent_item_title = serializers.CharField(source='parent_item.title', read_only=True, default=None)

    class Meta:
        model = Item
        fields = [
            # Identity
            'id', 'mode', 'item_type', 'item_key',

            # Project
            'project', 'project_key', 'project_name',
            'organization_id', 'organization_name', 'organization_slug', 'entity_key',

            # Basic info
            'title', 'description', 'owner_action_policy',

            # PLS-518: pinned current-state handoff summary (editable by assignee)
            'current_state', 'current_state_updated_at',

            # Source tracking
            'source', 'source_id', 'source_url',

            # Assignment
            'assignee_id', 'assignee_email', 'assignment_group', 'assignment_team',
            'workflow_name', 'workflow_status', 'assignee_name', 'assignee_health',
            'created_by_id', 'created_by_email', 'created_by_name', 'reporter_group',
            # PLS-900 / ADR-0052: task-level conflict authority — emails/identifiers
            # the resolver must never select as assignee (hard exclusion).
            'conflicted_parties',

            # Status
            'status', 'status_category', 'priority', 'severity',
            # PLS-675 / PLAT-1351 v4 typed acceptance mode (code|deployed|live)
            'acceptance_mode',

            # Timing
            'created_at', 'updated_at', 'due_date', 'start_date',
            'reminder_time', 'completed_at',

            # Task-specific: Sprint, Story Points & Progress
            'sprint', 'story_points', 'percent_complete',

            # Task-specific: Recurrence
            'is_recurring', 'frequency', 'recurrence_pattern', 'recurrence_end_date',
            'recurring_cycle',
            'parent_item', 'parent_item_key', 'parent_item_title',

            # Task-specific: Effort
            'estimated_hours', 'actual_hours',

            # Task-specific: Time tracking (Phase 3)
            'original_estimate_minutes', 'time_spent_minutes', 'remaining_estimate_minutes',

            # Task-specific: Component & Version (Phase 3)
            'component', 'fix_version', 'affects_versions',

            # Action-specific: Payload & Approval
            'payload', 'requires_approval', 'auto_approved', 'approved_by',
            'approved_at', 'approval_rule', 'rejection_reason',

            # Action-specific: Execution
            'callback_url', 'callback_method', 'callback_headers',
            'execution_result', 'error_message', 'retry_count', 'max_retries',

            # Alert-specific: Deduplication
            'fingerprint', 'fingerprint_fields', 'occurrence_count',
            'first_fired_at', 'last_fired_at',

            # Alert-specific: Resolution
            'resolved_at', 'resolved_by', 'resolution_note',
            'acknowledged_at', 'acknowledged_by',

            # Alert-specific: Escalation
            'escalation_level', 'last_escalated_at', 'escalation_rule',

            # Alert-specific: Correlation
            'correlation_id', 'parent_alert',

            # Common: Notifications
            'reminder_sent', 'reminder_count', 'last_reminder_sent',
            'notification_preferences',

            # Common: Silencing
            'silenced_until', 'silenced_by', 'silence_reason', 'silence_rule',

            # Common: Labels & Metadata
            'labels', 'tags', 'metadata', 'origin_observations',
            'containment_decision',
            'external_wait',
            'append_origin_observations',

            # Legacy compatibility
            'external_id', 'external_url', 'sync_enabled', 'last_synced',

            # Computed fields
            'is_overdue', 'days_until_due', 'is_silenced_now', 'is_active_item', 'tags_list',
            # PLS-518: the pin timestamp is server-managed (auto-set on write)
            'current_state_updated_at',
        ]
        read_only_fields = [
            'id', 'item_key', 'created_at', 'updated_at', 'completed_at',
            'approved_at', 'first_fired_at', 'last_fired_at',
            'resolved_at', 'acknowledged_at', 'last_escalated_at',
            'reminder_sent', 'reminder_count', 'last_reminder_sent', 'last_synced',
            'is_overdue', 'days_until_due', 'is_silenced_now', 'is_active_item', 'tags_list',
            'fingerprint', 'occurrence_count',
            'project_key', 'project_name',
            'organization_id', 'organization_name', 'organization_slug', 'entity_key',
            'parent_item_key', 'parent_item_title',
            'assignee_name', 'created_by_name',
            'origin_observations',
            'external_wait',
            'time_spent_minutes',  # Auto-calculated from work logs
            # PLS-675 / Rei P1: acceptance_mode is set at create (or by an
            # authorized reclassification path). Ordinary PATCH must not
            # downgrade live/deployed → code and bypass the deploy-verify gate.
            'acceptance_mode',
        ]

    def validate_conflicted_parties(self, value):
        """PLS-971 / ADR-0052: normalize conflicted_parties on write.

        Accepts a list or a comma-separated string; lowercases, strips, and
        drops blanks so the resolver's hard-exclusion always compares against
        the canonical lowercase email form (matching the MCP create/update
        normalization). A malformed non-list/non-string value is rejected.
        """
        if value is None:
            return value
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(',') if p.strip()]
        elif isinstance(value, (list, tuple)):
            parts = [str(p).strip() for p in value if p is not None and str(p).strip()]
        else:
            from rest_framework import serializers as _s
            raise _s.ValidationError('conflicted_parties must be a list of emails or a comma-separated string')
        # Lowercase and dedupe (order-preserving) so case/whitespace variants of
        # the same party collapse to one canonical entry.
        seen = set()
        out = []
        for p in parts:
            norm = p.lower()
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def get_external_wait(self, obj):
        """Return only org-local/opaque wait data; never foreign Item tuples."""
        from .models import ExternalDependencyBinding, ExternalWaitAuthority

        result = {}
        authority = None
        if obj.external_wait_generation:
            authority = obj.external_wait_authorities.filter(
                wait_generation=obj.external_wait_generation,
                state__in={
                    ExternalWaitAuthority.STATE_ENTER_ARMED,
                    ExternalWaitAuthority.STATE_WAITING,
                    ExternalWaitAuthority.STATE_EXIT_ARMED,
                },
            ).first()
        if authority is not None:
            current = {
                'branch': authority.branch,
                'receipt': str(authority.receipt_id),
                'wait_generation': authority.wait_generation,
                'entered_at': authority.entered_at,
                'revalidate_after': authority.revalidate_after,
            }
            if authority.branch == ExternalWaitAuthority.BRANCH_HUMAN:
                # Local workflow context lets an authorized caller select the
                # configured resolution transition without guessing its source
                # state. No foreign binding or historical owner is disclosed.
                current['resume_status'] = authority.pre_status
                current['resume_workflow_status'] = authority.pre_workflow_status
                evidence = (
                    authority.evidence
                    if isinstance(authority.evidence, dict)
                    else {}
                )
                current['dependency'] = {
                    key: evidence.get(key)
                    for key in (
                        'holder_ref',
                        'requested_subject',
                        'acceptance_check',
                    )
                    if evidence.get(key)
                }
            elif authority.branch == ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE:
                # Closed, redacted schema only. Source-evidence identifiers and
                # digests stay behind the capability/audit boundary.
                current['checkpoint_at'] = authority.checkpoint_at
                current['display_reference'] = authority.display_reference
            else:
                current['bindings'] = [
                    {
                        'receipt': str(binding.receipt_id),
                        'state': binding.state,
                        'release_policy': binding.release_policy,
                        'local_display_reference': binding.local_display_reference,
                        'release_recorded_at': binding.release_recorded_at,
                    }
                    for binding in authority.bindings.exclude(
                        state=ExternalDependencyBinding.STATE_PREPARED,
                    ).order_by('created_at')
                ]
            result['current'] = current

        outbound = list(
            obj.outbound_external_dependency_bindings.filter(
                state__in={
                    ExternalDependencyBinding.STATE_ACTIVE,
                    ExternalDependencyBinding.STATE_RELEASED,
                },
            ).order_by('created_at')
        )
        if outbound:
            result['outbound_bindings'] = [
                {
                    'receipt': str(binding.receipt_id),
                    'state': binding.state,
                    'release_policy': binding.release_policy,
                }
                for binding in outbound
            ]
        return result or None

    def get_is_overdue(self, obj):
        return obj.is_overdue()

    def get_days_until_due(self, obj):
        return obj.days_until_due()

    def get_is_silenced_now(self, obj):
        return obj.is_silenced()

    def get_is_active_item(self, obj):
        return obj.is_active()

    def get_tags_list(self, obj):
        return obj.get_tags_list()

    def get_origin_observations(self, obj):
        from .origin_provenance import origin_observations_with_inference

        return origin_observations_with_inference(
            obj.metadata,
            source_url=obj.source_url,
            description=obj.description,
            source=obj.source,
            payload=obj.payload if obj.source == 'origin-ci' else None,
            labels=obj.labels,
        )

    def get_containment_decision(self, obj):
        from .services.urgent_risk_containment import serialize_decision

        return serialize_decision(obj)

    def get_recurring_cycle(self, obj):
        """PLS-792: expose the current recurring-cycle freshness payload.

        Returns the machine-readable ``cycle_snapshot`` (cycle_revision,
        generation, event_id, due_boundary, completion_watermark) for a
        recurring item so a client can Refresh->Run Now; ``None`` for
        non-recurring items.
        """
        if not getattr(obj, 'is_recurring', False):
            return None
        from .services.recurring_cycle_state import cycle_snapshot
        return cycle_snapshot(obj)

    # Cache Status→category lookups per-request to avoid N+1 DB hits on list views.
    _status_category_cache = {}

    def get_status_category(self, obj):
        """Resolve the Status row for obj.status and return its category.
        Categories: 'todo' | 'in_progress' | 'done'. Returns None if the slug
        doesn't exist in the Status table (defensive — frontend falls back to
        its own styling in that case)."""
        slug = obj.status
        if not slug:
            return None
        if slug not in ItemSerializer._status_category_cache:
            from .models import Status
            s = Status.objects.filter(slug=slug).only('category').first()
            ItemSerializer._status_category_cache[slug] = s.category if s else None
        return ItemSerializer._status_category_cache[slug]

    def _project_organization(self, obj):
        """Return one cached organization projection for the task's project.

        A task's immutable tenancy identity is ``project.org_id``.  Name and slug
        are display enrichment from Admin, so fetch them once per organization per
        serializer instance instead of making separate calls for each field.
        """
        project = getattr(obj, 'project', None)
        if not project:
            return None
        cache_by_id = getattr(self, '_task_organization_cache', None)
        if cache_by_id is None:
            cache_by_id = {}
            self._task_organization_cache = cache_by_id
        org_id = int(project.org_id)
        if org_id not in cache_by_id:
            cache_by_id[org_id] = project.get_organization()
        return cache_by_id[org_id]

    def get_organization_name(self, obj):
        """Display name for the task's owning organization."""
        return _dict_field(self._project_organization(obj), 'name') or ''

    def get_organization_slug(self, obj):
        """Slug for the task's owning organization."""
        return _dict_field(self._project_organization(obj), 'slug') or ''

    def get_entity_key(self, obj):
        return _project_entity_key(obj.project)

    def _get_user_display_name(self, user_id):
        """
        Fetch user display name from shizuha-id (source of truth).
        Uses caching to avoid repeated API calls.
        """
        if not user_id:
            return None

        # Check cache first (5 minute TTL)
        cache_key = f"user_display_name:{user_id}"
        cached_name = cache.get(cache_key)
        if cached_name is not None:
            return cached_name if cached_name != '' else None

        # Fetch from shizuha-id
        user_info = user_service.get_user_by_id(user_id)
        if user_info:
            # Prefer full name, then first_name, then username
            first = user_info.get('first_name', '').strip()
            last = user_info.get('last_name', '').strip()
            full = f"{first} {last}".strip() if (first or last) else ''
            name = (
                full or
                user_info.get('username') or
                user_info.get('email', '').split('@')[0]
            )
            cache.set(cache_key, name, 300)  # Cache for 5 minutes
            return name

        # Cache empty result to avoid repeated lookups
        cache.set(cache_key, '', 60)  # Cache miss for 1 minute
        return None

    def _get_user_display_name_by_email(self, email):
        """Fetch display name from shizuha-id keyed by EMAIL (the stable cross-service key).

        Use this, NOT assignee_id, for display: the local assignee_id/created_by_id
        do not share shizuha-id's id space and have drifted (one local id maps to
        many real users), so resolving a name by id returns the WRONG person.
        """
        if not email:
            return None
        cache_key = f"user_display_name_email:{email.lower()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        info = user_service.get_user_by_email(email)
        if info:
            first = (info.get('first_name') or '').strip()
            last = (info.get('last_name') or '').strip()
            full = f"{first} {last}".strip() if (first or last) else ''
            name = full or info.get('username') or email.split('@')[0]
            cache.set(cache_key, name, 300)
            return name
        cache.set(cache_key, '', 60)
        return None

    def get_assignee_health(self, obj):
        return _agent_health_for_email(obj.assignee_email)

    def get_assignment_team(self, obj):
        """Organization-qualified product identity for the task's team.

        ``assignment_group`` remains the legacy storage/API compatibility name;
        clients should present this object as the Team. Team identity never
        falls back across organization boundaries.
        """
        from .models import TeamRoster
        from .services.workflow_service import WorkflowService

        team_name = WorkflowService._canonical_team_name(obj.assignment_group)
        if not team_name:
            return None
        project = getattr(obj, 'project', None)
        task_org_slug = self.get_organization_slug(obj) if project else ''
        roster = None
        if task_org_slug:
            roster = TeamRoster.objects.filter(
                organization_slug=task_org_slug,
                team_name=team_name,
                is_active=True,
            ).only('id', 'organization_slug', 'team_name').first()
        team_org_slug = task_org_slug

        same_as_task_org = bool(project and team_org_slug == task_org_slug)
        return {
            'id': roster.pk if roster else None,
            'canonical_key': f'{team_org_slug}:{team_name}' if team_org_slug else team_name,
            'canonical_name': team_name,
            'display_name': team_name.replace('-', ' ').title(),
            'organization_id': int(project.org_id) if same_as_task_org else None,
            'organization_slug': team_org_slug,
            'organization_name': (
                self.get_organization_name(obj) if same_as_task_org else ''
            ),
        }

    def get_assignee_name(self, obj):
        """Assignee display name. Resolve by EMAIL (stable key); never trust the
        drifted local assignee_id (was rendering e.g. 'Hritik Soni' on ichi's task)."""
        if obj.assignee_email:
            return self._get_user_display_name_by_email(obj.assignee_email) or obj.assignee_email.split('@')[0]
        if obj.assignee_id:
            return self._get_user_display_name(obj.assignee_id)
        return None

    def get_created_by_name(self, obj):
        """Creator display name. Resolve by EMAIL (stable key); see get_assignee_name."""
        if obj.created_by_email:
            return self._get_user_display_name_by_email(obj.created_by_email) or obj.created_by_email.split('@')[0]
        if obj.created_by_id:
            return self._get_user_display_name(obj.created_by_id)
        return None

    def get_reporter_group(self, obj):
        """The reporter's TEAM ('where the task came from'). Stored reporter_group if
        set, else derived from the reporter's roster membership, else the item's team.
        Team-based so the UI shows a team and back-to-reporter routing is resilient."""
        try:
            from .services.workflow_service import WorkflowService
            return WorkflowService._reporter_group(obj)
        except Exception:
            return getattr(obj, 'assignment_group', None)

    def validate(self, data):
        """Validate item data based on mode."""
        # PLS-1249 (secondary defect): acceptance_mode is read-only on update,
        # but DRF's default silently DROPS read-only fields from validated
        # data — a client PATCHing it got HTTP 200 with the field unchanged
        # and no way to distinguish "applied" from "dropped" without a
        # follow-up GET (this masked the SCLI-723 diagnosis for a full
        # round-trip). Reject the write loudly instead: on UPDATE, an
        # explicit acceptance_mode in the request is a 400; on CREATE the
        # create() path above handles it.
        #
        # NOTE: this guard was originally a separate validate() defined
        # earlier in this class body, silently SHADOWED by this method
        # (Python last-definition-wins) — the loud reject never executed and
        # this PR's own test caught it. It lives here now; the dead
        # duplicate is gone.
        if self.instance is not None and isinstance(getattr(self, 'initial_data', None), dict) \
                and 'acceptance_mode' in self.initial_data:
            raise serializers.ValidationError({
                'acceptance_mode': (
                    'acceptance_mode is read-only on update; it is set at '
                    'create or by an authorized reclassification path '
                    '(PLS-675 / Rei P1).'
                ),
            })

        mode = data.get('mode', self.instance.mode if self.instance else 'task')

        # A package is several units of work behind one status. Create refuses
        # it. An update may still edit an existing package so it can be split.
        if mode in (None, '', 'task'):
            from .services.package_guard import package_of_tasks_reason
            new_title = data.get('title', getattr(self.instance, 'title', '') or '')
            new_description = data.get(
                'description', getattr(self.instance, 'description', '') or '',
            )
            reason = package_of_tasks_reason(new_title, new_description)
            already = ''
            if self.instance is not None:
                already = package_of_tasks_reason(
                    self.instance.title or '', self.instance.description or '',
                )
            if reason and not already:
                raise serializers.ValidationError({'description': reason})

        # PLS-521: a workflow-governed security finding must never bypass the
        # deployed-evidence gate (or Verify Fixed) via a direct item PATCH.
        # Every lifecycle mutation goes through /transition/, where validators,
        # verdict/outbox atomicity and lane routing execute.
        current_workflow = ''
        if self.instance is not None:
            current_workflow = (
                (self.instance.workflow_name or '').strip().lower().replace('_', '-')
            )
            requested_workflow = (
                (data.get('workflow_name', self.instance.workflow_name) or '')
                .strip().lower().replace('_', '-')
            )
            if (
                'workflow_name' in data
                and data['workflow_name'] != self.instance.workflow_name
                and 'security-finding' in {current_workflow, requested_workflow}
            ):
                raise serializers.ValidationError({
                    'workflow_name': (
                        'Security-finding workflow identity is structural and transition-only; '
                        'it cannot be changed through the item endpoint.'
                    )
                })

        if self.instance is not None and current_workflow == 'security-finding':
            direct_changes = {
                field: data[field]
                for field in ('status', 'workflow_status')
                if field in data and data[field] != getattr(self.instance, field)
            }
            if direct_changes:
                raise serializers.ValidationError({
                    field: 'Security-finding lifecycle state is transition-only; use the transition endpoint.'
                    for field in direct_changes
                })

        if self.instance is not None and 'project' in data:
            old_project = self.instance.project
            new_project = data.get('project')
            old_org_id = old_project.org_id if old_project else None
            new_org_id = new_project.org_id if new_project else None
            if old_org_id != new_org_id:
                raise serializers.ValidationError({
                    'project': (
                        'A task cannot be moved to another organization. Create '
                        'a new task in that organization instead.'
                    )
                })

        if 'metadata' in data:
            from .origin_provenance import normalize_origin_observations

            metadata = data.get('metadata')
            if metadata is not None and not isinstance(metadata, dict):
                raise serializers.ValidationError({'metadata': 'Must be a JSON object.'})
            metadata = dict(metadata or {})
            # PLAT-9504 trigger-hunt: labels are deliberately dual-shape
            # (PLS-712 — flat string arrays for tasks, key/value objects for
            # alerts), but ANY other shape poisoned focused-QA candidates and
            # crashed the reconcile's unpacks. Validate at the write boundary:
            # dict or list only, never anything else.
            labels = data.get('labels')
            if labels is not None and not isinstance(labels, (dict, list)):
                raise serializers.ValidationError(
                    {'labels': 'Must be a JSON object or a JSON array of strings.'})
            if 'origin_observations' in metadata:
                try:
                    metadata['origin_observations'] = normalize_origin_observations(
                        metadata.get('origin_observations')
                    )
                except ValueError as exc:
                    raise serializers.ValidationError({'origin_observations': str(exc)}) from exc
            # PLS-634 / rei P1: accepted-plan envelope is server-owned. Generic
            # metadata PATCH must not invent or rewrite Accept authority.
            _SERVER_OWNED_META = (
                'accepted_plan',
                'accept_capacity_held',
            )
            if self.instance is not None:
                existing_meta = dict(getattr(self.instance, 'metadata', None) or {})
                for key in _SERVER_OWNED_META:
                    if key in metadata:
                        # Preserve server value; drop client rewrite.
                        if key in existing_meta:
                            metadata[key] = existing_meta[key]
                        else:
                            metadata.pop(key, None)
            else:
                for key in _SERVER_OWNED_META:
                    metadata.pop(key, None)
            data['metadata'] = metadata

        if 'append_origin_observations' in data:
            from .origin_provenance import normalize_origin_observations

            try:
                data['append_origin_observations'] = normalize_origin_observations(
                    data.get('append_origin_observations')
                )
            except ValueError as exc:
                raise serializers.ValidationError({'append_origin_observations': str(exc)}) from exc

        # Redact secrets from free-text fields before persist (PLS-39)
        for field in ('title', 'description'):
            raw = data.get(field)
            if raw:
                cleaned, found = redact_secrets(raw)
                if found:
                    data[field] = cleaned

        # Validate due_date vs start_date
        start_date = data.get('start_date')
        due_date = data.get('due_date')
        if start_date and due_date and start_date > due_date:
            raise serializers.ValidationError("Start date cannot be after due date")

        # Validate recurrence settings. Use instance values on PATCH so
        # partial updates cannot accidentally bypass or create invalid config.
        frequency = data.get('frequency', getattr(self.instance, 'frequency', 'one_time'))
        is_recurring = data.get('is_recurring', getattr(self.instance, 'is_recurring', False))
        recurrence_pattern = data.get(
            'recurrence_pattern',
            getattr(self.instance, 'recurrence_pattern', None),
        )
        if data.get('is_recurring') is False and 'frequency' not in data:
            data['frequency'] = 'one_time'
            frequency = 'one_time'
        if frequency != 'one_time' and not is_recurring:
            data['is_recurring'] = True
            is_recurring = True
        try:
            validate_recurring_configuration(is_recurring, frequency, recurrence_pattern)
        except ValueError as exc:
            raise serializers.ValidationError({'frequency': str(exc)})

        # Mode-specific validation
        if mode == 'action':
            if data.get('requires_approval') and not data.get('callback_url'):
                # Callback URL is optional but recommended for actions
                pass

        if mode == 'alert':
            if not data.get('severity'):
                data['severity'] = 'warning'  # Default severity for alerts

        # security-ci source_id is a rolling identity (ledger or finding). A
        # second active row with the same (source, source_id) is a remint — refuse
        # at create so weekly scans cannot clutter Pulse (live 2026-08-29).
        if self.instance is None and mode == 'task':
            create_source = (data.get('source') or '').strip()
            create_source_id = (data.get('source_id') or '').strip()
            if create_source == 'security-ci' and create_source_id:
                from .models import Item as _Item
                _non_active = (
                    'done', 'completed', 'closed', 'cancelled', 'canceled',
                    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                    'failed', 'deferred', 'expired',
                )
                hit = (
                    _Item.objects.filter(source=create_source, source_id=create_source_id)
                    .exclude(status__in=_non_active)
                    .only('item_key')
                    .first()
                )
                if hit:
                    raise serializers.ValidationError({
                        'source_id': (
                            f'Active item {hit.item_key} already owns source_id '
                            f'{create_source_id}; update that item in place.'
                        )
                    })

        # PLS-256: Definition-of-Ready intake gate (create-only). A new task must
        # carry an actionable outcome — an empty / one-line description below the
        # word threshold is flagged `needs-detail` (default, non-blocking) or
        # rejected. Complements the MCP-layer ≥60%-overlap dedup. Lenient: only
        # mode='task', non-recurring, and human/agent-authored (gated) sources;
        # alerts/actions and structured auto-filer payloads pass untouched.
        if self.instance is None and mode == 'task':
            from django.conf import settings as _settings
            dor_mode = getattr(_settings, 'PULSE_DOR_INTAKE_MODE', 'flag')
            gated_sources = getattr(_settings, 'PULSE_DOR_GATED_SOURCES', {'manual'})
            source = (data.get('source') or 'manual').strip().lower()
            if dor_mode != 'off' and not data.get('is_recurring', False) and source in gated_sources:
                min_words = int(getattr(_settings, 'PULSE_DOR_MIN_DESCRIPTION_WORDS', 4))
                desc = (data.get('description') or '').strip()
                if len(desc.split()) < min_words:
                    if dor_mode == 'reject':
                        raise serializers.ValidationError({'description': (
                            f'Definition-of-Ready: a task needs an actionable description '
                            f'(at least {min_words} words describing the outcome / acceptance). '
                            f'Add detail and resubmit.')})
                    # default 'flag' mode — non-blocking: tag needs-detail, create anyway.
                    # PLAT-9504: merge shape-preservingly — labels may be a flat
                    # array (PLS-712) and dict-splatting a list raises
                    # TypeError: 'list' object is not a mapping.
                    existing_labels = data.get('labels')
                    if isinstance(existing_labels, dict):
                        data['labels'] = {**existing_labels, 'needs-detail': 'true'}
                    elif isinstance(existing_labels, list):
                        data['labels'] = [*(l for l in existing_labels if l != 'needs-detail'), 'needs-detail']
                    else:
                        data['labels'] = {'needs-detail': 'true'}

        return data

    def _warn_retro_acceptance_mode_live(self, instance, raw):
        """PLAT-9427: loud, non-blocking filing-mode warning when a
        retro-finding (record-class) row is filed ``acceptance_mode=live``.
        Best-effort — a comment failure must never fail the create.
        """
        try:
            from .services.workflow_service import WorkflowService
            tokens = WorkflowService._item_label_and_tag_tokens(instance)
            if (raw != WorkflowService._ACCEPTANCE_MODE_LIVE
                    or WorkflowService._RECORD_CLASS_TAG not in tokens):
                return
            request = self.context.get('request')
            actor = getattr(request, 'user', None)
            from .services.mechanical_comments import MechanicalCommentFactory
            MechanicalCommentFactory.create(
                item=instance,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='warning.retro_acceptance_mode_live',
                template_version=2,
                structured_payload={
                    'item_key': getattr(instance, 'item_key', '') or str(instance.pk),
                    'result': 'warning',
                    'filed_acceptance_mode': raw,
                    'record_class_tag': WorkflowService._RECORD_CLASS_TAG,
                    'detail': (
                        'retro-finding row filed acceptance_mode=live; if the '
                        'deliverable is a record, correct it via '
                        'POST /api/items/{key}/reclassify-acceptance/ '
                        '(PLAT-9427 seat-accessible reclassification)'
                    ),
                },
                # Deterministic per-item event id — one warning per create,
                # replay-safe under the factory's idempotency contract.
                origin_event_id=(
                    'warning.retro_acceptance_mode_live:'
                    + (getattr(instance, 'item_key', '') or str(instance.pk))
                ),
                causal_actor=actor if getattr(actor, 'is_authenticated', False) else None,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'PLAT-9427 retro acceptance-mode warning failed (non-blocking)')

    def create(self, validated_data):
        """Create item with proper defaults based on mode.

        NOTE (PLAT-9427): this is the ONLY surviving ``create`` on this class
        body — an earlier definition lived above and was silently SHADOWED
        (Python last-definition-wins; the same trap PLS-1249 documented for
        ``validate``). Its acceptance_mode create-time backfill and the
        PLAT-9100 group backstop call never executed. Both live here now.
        """
        # PLAT-9100: create-time assignment_group backstop (PLAT-9096
        # prevention) — require or auto-resolve the owning team on EVERY
        # API/MCP create, before the row exists. Loud-fails only for
        # teamless ownerless task creates; ledger otherwise. See
        # tasks/services/assignment_group_backstop.py. (Carried here from
        # the shadowed definition — review rei, pulse#620.)
        from .services.assignment_group_backstop import apply_create_time_backstop
        apply_create_time_backstop(validated_data)

        # PLAT-9100 × PLS-539 composition (pulse#620): the backstop's
        # AUTO-RESOLVED group is provenance-derived, not caller intent. On a
        # direct handoff (view-flagged ``_pls539_direct_create`` — the
        # canonical ``normalize_direct_handoff.is_direct`` computed AFTER the
        # team-route hint drop) an auto-resolved group is exactly the
        # "leftover stale group" the PATCH path clears (revi #316/#47 P1): a
        # direct override must not persist a team the direct owner's routing
        # did not choose — the item is person-owned and pin-governed, exempt
        # from team drain. A group CO-SUPPLIED in the request canonicalizes
        # via backstop branch 1 (no 'auto-resolved' provenance flag) and is
        # RETAINED, matching PATCH.
        if getattr(self, '_pls539_direct_create', False):
            _prov = ((validated_data.get('metadata') or {}).get(
                'assignment_group_backstop') or '')
            if _prov.startswith('auto-resolved') and validated_data.get('assignment_group'):
                validated_data['assignment_group'] = None
                _meta = dict(validated_data.get('metadata') or {})
                _meta['assignment_group_backstop'] = _prov + ':dropped-direct-handoff'
                validated_data['metadata'] = _meta

        # PLS-675 / Rei P1: acceptance_mode is read_only on update but must
        # remain settable at create. Pull from initial_data when the client
        # supplies a valid mode.
        from .services.workflow_service import WorkflowService
        raw = ''
        if isinstance(getattr(self, 'initial_data', None), dict):
            raw = str(self.initial_data.get('acceptance_mode') or '').strip().lower()
        if raw in WorkflowService._ACCEPTANCE_MODES_ALL:
            validated_data['acceptance_mode'] = raw

        from .origin_provenance import infer_origin_observation, merge_origin_observations

        additions = validated_data.pop('append_origin_observations', [])
        inferred = infer_origin_observation(
            source_url=validated_data.get('source_url') or '',
            description=validated_data.get('description') or '',
            source=validated_data.get('source') or '',
        )
        if inferred:
            additions = [*additions, inferred]
        if additions:
            metadata = dict(validated_data.get('metadata') or {})
            metadata['origin_observations'] = merge_origin_observations(
                metadata.get('origin_observations'), additions
            )
            validated_data['metadata'] = metadata
        mode = validated_data.get('mode', 'task')

        # Set defaults based on mode
        if mode == 'task':
            validated_data.setdefault('status', 'pending')
            validated_data.setdefault('item_type', 'task.generic')
        elif mode == 'action':
            validated_data.setdefault('status', 'pending')
            validated_data.setdefault('requires_approval', True)
            if not validated_data.get('item_type', '').startswith('action.'):
                validated_data['item_type'] = f"action.{validated_data.get('item_type', 'generic')}"
        elif mode == 'alert':
            validated_data.setdefault('status', 'firing')
            validated_data.setdefault('severity', 'warning')
            validated_data['first_fired_at'] = timezone.now()
            validated_data['last_fired_at'] = timezone.now()
            if not validated_data.get('item_type', '').startswith('alert.'):
                validated_data['item_type'] = f"alert.{validated_data.get('item_type', 'error')}"

        wip_token = self.context.pop('_wip_reservation_token', None)
        if wip_token:
            # The create view resolves against an unsaved permission-probe Item.
            # Transfer its PLS-509 reservation to the real row before the first
            # save so the capacity claim is verified and consumed atomically.
            instance = self.Meta.model(**validated_data)
            instance._wip_reservation_token = wip_token
            instance.save()
            self._warn_retro_acceptance_mode_live(instance, raw)
            return instance
        instance = super().create(validated_data)
        self._warn_retro_acceptance_mode_live(instance, raw)
        return instance

    def update(self, instance, validated_data):
        """Append CI evidence server-side without replacing unrelated metadata."""
        from .origin_provenance import infer_origin_observation, merge_origin_observations

        additions = validated_data.pop('append_origin_observations', [])
        inferred = infer_origin_observation(
            source_url=validated_data.get('source_url', instance.source_url) or '',
            description=validated_data.get('description', instance.description) or '',
            source=validated_data.get('source', instance.source) or '',
        )
        if inferred:
            additions = [*additions, inferred]
        if additions:
            if 'metadata' in validated_data:
                metadata = dict(validated_data.get('metadata') or {})
            else:
                metadata = dict(instance.metadata or {})
            existing = (instance.metadata or {}).get('origin_observations')
            metadata['origin_observations'] = merge_origin_observations(existing, additions)
            validated_data['metadata'] = metadata

        # PLS-792 acceptance #4: turning recurrence OFF on a task parked in the
        # timer-owned `scheduled` gap leaves it stranded in a status no human can
        # act on (no Cancel/Discard, no Next). Restore the workflow's initial
        # actionable status so the scheduler-side parked state is cleared.
        if (
            validated_data.get('is_recurring') is False
            and str(instance.status or '').strip().lower() == 'scheduled'
            and getattr(instance, 'is_recurring', False) is True
        ):
            from .services.recurring_cycle_state import _initial_status, _sync_workflow_status
            target = _initial_status(instance, default='todo')
            validated_data['status'] = target
            validated_data['due_date'] = None  # clear the parked next-due boundary
            _sync_workflow_status(instance, target)

        return super().update(instance, validated_data)


# Backward compatibility alias
TaskSerializer = ItemSerializer


class ItemListSerializer(OwnerActionPolicyMixin, serializers.ModelSerializer):
    """
    Lightweight serializer for item lists.
    """
    is_overdue = serializers.SerializerMethodField()
    days_until_due = serializers.SerializerMethodField()
    tags_list = serializers.SerializerMethodField()
    pull_requests = serializers.SerializerMethodField()
    origin_observations = serializers.SerializerMethodField()
    status_category = serializers.SerializerMethodField()
    owner_action_policy = serializers.SerializerMethodField()
    last_comment_at = serializers.SerializerMethodField()
    comment_count = serializers.SerializerMethodField()
    assignee_health = serializers.SerializerMethodField()
    containment_decision = serializers.SerializerMethodField()
    # PLS-792: recurring-cycle freshness on the lightweight list read surface.
    recurring_cycle = serializers.SerializerMethodField()

    # Project fields (read-only computed)
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    organization_id = serializers.IntegerField(source='project.org_id', read_only=True)

    # Parent item fields (read-only computed, needed by roadmap/list views)
    parent_item_key = serializers.CharField(source='parent_item.item_key', read_only=True, default=None)
    parent_item_title = serializers.CharField(source='parent_item.title', read_only=True, default=None)

    class Meta:
        model = Item
        fields = [
            'id', 'mode', 'item_type', 'item_key', 'title', 'source',
            'project', 'project_key', 'project_name', 'organization_id',
            'assignee_id', 'assignee_email', 'assignment_group', 'workflow_name', 'workflow_status', 'assignee_health',
            'due_date', 'start_date', 'priority', 'severity', 'status', 'status_category', 'owner_action_policy',
            'sprint', 'story_points', 'percent_complete', 'parent_item', 'parent_item_key', 'parent_item_title',
            'is_recurring', 'recurring_cycle', 'tags', 'labels',
            'containment_decision',
            'created_at', 'updated_at',
            'is_overdue', 'days_until_due', 'tags_list',
            'pull_requests',
            'origin_observations',
            # Recent-activity signal (PLS-88): lets an agent's heartbeat loop
            # cheaply detect new reviewer feedback on held tasks without a
            # per-task pulse_list_comments fetch (the SCLI-76 churn pattern).
            'last_comment_at', 'comment_count',
            # Alert-specific summary fields
            'occurrence_count', 'fingerprint',
        ]

    def get_assignee_health(self, obj):
        return _agent_health_for_email(obj.assignee_email)

    def get_recurring_cycle(self, obj):
        """PLS-792: recurring-cycle freshness payload on the list surface."""
        if not getattr(obj, 'is_recurring', False):
            return None
        from .services.recurring_cycle_state import cycle_snapshot
        return cycle_snapshot(obj)

    def get_containment_decision(self, obj):
        from .services.urgent_risk_containment import serialize_decision

        return serialize_decision(obj)

    def get_is_overdue(self, obj):
        return obj.is_overdue()

    def get_days_until_due(self, obj):
        return obj.days_until_due()

    def get_tags_list(self, obj):
        return obj.get_tags_list()

    def get_pull_requests(self, obj):
        """
        Surface linked PRs (number + status + url) in the list payload so
        MCP consumers can cite them directly without a second round trip
        and without hallucinating from session memory. Only exposes the
        minimal fields — no other metadata leaks.
        """
        md = obj.metadata or {}
        if not isinstance(md, dict):
            return []
        prs = md.get('pull_requests') or []
        if not isinstance(prs, list):
            return []
        return [
            {
                'number': pr.get('number'),
                'status': pr.get('status') or 'open',
                'url': pr.get('url'),
                'repo': pr.get('repo'),
            }
            for pr in prs
            if isinstance(pr, dict)
        ]

    def get_origin_observations(self, obj):
        from .origin_provenance import origin_observations_with_inference

        return origin_observations_with_inference(
            obj.metadata,
            source_url=obj.source_url,
            description=obj.description,
            source=obj.source,
            payload=(getattr(obj, '_origin_ci_payload', None)
                     if 'payload' in obj.get_deferred_fields() else obj.payload)
                    if obj.source == 'origin-ci' else None,
            labels=obj.labels,
        )

    def get_status_category(self, obj):
        """Resolve status category from Status table. Reuses ItemSerializer's cache."""
        slug = obj.status
        if not slug:
            return None
        if slug not in ItemSerializer._status_category_cache:
            from .models import Status
            s = Status.objects.filter(slug=slug).only('category').first()
            ItemSerializer._status_category_cache[slug] = s.category if s else None
        return ItemSerializer._status_category_cache[slug]

    def get_comment_count(self, obj):
        """
        PLS-88: total comments on this task. Prefers the `_comment_count`
        annotation set by the my_tasks endpoint (single-query, no N+1); falls
        back to a direct count if the serializer is used without it.
        PLS-1294: attention count — system lifecycle/audit comments are
        excluded via the shared substantive classing, matching the
        annotations and the trail digest (leg 1).
        """
        val = getattr(obj, '_comment_count', None)
        if val is None:
            return obj.comments.filter(
                origin_kind__in=Comment.SUBSTANTIVE_ORIGIN_KINDS
            ).count()
        return val

    def get_last_comment_at(self, obj):
        """
        PLS-88: ISO timestamp of the most recent comment, or null if none.
        Prefers the `_last_comment_at` annotation; falls back to a query.
        """
        if hasattr(obj, '_last_comment_at'):
            ts = obj._last_comment_at
        else:
            ts = obj.comments.filter(
                origin_kind__in=Comment.SUBSTANTIVE_ORIGIN_KINDS
            ).order_by('-created_at').values_list(
                'created_at', flat=True
            ).first()
        return ts.isoformat() if ts else None


TaskListSerializer = ItemListSerializer


class ItemCreateSerializer(ItemSerializer):
    """
    Serializer for creating items with required field validation.
    """
    class Meta(ItemSerializer.Meta):
        pass

    def validate(self, data):
        data = super().validate(data)
        # PLAT-9125: filing-discipline guard for the structurally-human
        # human-ops lane (PLAT-9076 backstop pattern — fail closed at the
        # filing boundary with a loud, actionable 400). Runs FIRST: the
        # misroute class is independent of project validity, and rejecting
        # the misroute before the project check gives the executing pod the
        # actionable refile instruction, not a generic validation error.
        # Team 18 has zero agent members BY DESIGN (lead = the human
        # operator; physical-hands escalations only); agent-executable work
        # filed there starves (PLAT-6474 sat 681h past a 96h SLA). A
        # human-ops filing must carry physical-hardware / role-gated-operator
        # tagging to prove human ownership.
        group = (data.get('assignment_group') or '').strip().lower()
        if group in {'human-ops', 'human_ops'}:
            from .services.workflow_service import WorkflowService
            if not WorkflowService._filing_claims_physical_hands(data):
                raise serializers.ValidationError({
                    'assignment_group': (
                        'human-ops is the structurally-human lane (zero agent '
                        'members by design — physical-hands escalations only). '
                        'This filing carries no physical-hardware / '
                        'role-gated-operator tagging, so it is agent-executable '
                        'work that would starve in the human queue. Refile to '
                        'the owning lane: host-credential / cluster-plane / '
                        'runtime → devops (DevOps holds host ssh + passwordless '
                        'sudo — self-serve, NOT an escalation); code / '
                        'implementation → engineering; QA / verification → qa. '
                        'Tag the item physical-hardware or role-gated-operator '
                        'ONLY if it genuinely needs the human operator\'s hands.'
                    ),
                })
        if not data.get('title'):
            raise serializers.ValidationError("Title is required")
        mode = data.get('mode', 'task')
        if mode == 'task' and data.get('project') is None:
            raise serializers.ValidationError({
                'project': 'A task must belong to an organization project.'
            })
        return data


TaskCreateSerializer = ItemCreateSerializer


class ItemUpdateSerializer(ItemSerializer):
    """
    Serializer for updating items with all fields optional.
    """
    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance

        # Operator directive 2026-08-08: a task that blocks active work MUST
        # carry the highest priority (urgent). Reject an explicit downgrade
        # below urgent while the item still has active blocked descendants.
        if (
            'priority' in attrs
            and instance is not None
            and str(attrs.get('priority') or '').strip().lower() != 'urgent'
        ):
            from .services.workflow_service import WorkflowService
            try:
                is_blocker = WorkflowService._has_active_blocked_descendants(
                    instance,
                )
            except Exception:
                is_blocker = False
            if is_blocker:
                raise serializers.ValidationError({
                    'priority': (
                        'This task blocks active work — its priority cannot be '
                        'lowered below **urgent**. Blocker tasks must carry the '
                        'highest priority so the rebalancer assigns them before '
                        'any lower-priority work; lower it only after the '
                        'dependent is resolved.'
                    ),
                })

        # PLS-559: typed workflow-change revisions are authoritative. A generic
        # PATCH may edit surrounding prose or unrelated metadata, but it must
        # never replace the executor payload or make the derived JSON projection
        # disagree with it.  Typed operation revisions go exclusively through
        # pulse_revise_workflow_change, which validates and writes both fields
        # atomically while the task is proposed.
        if instance and getattr(instance, 'workflow_name', '') == 'workflow-change':
            existing_metadata = instance.metadata if isinstance(instance.metadata, dict) else {}
            existing_envelope = existing_metadata.get('typed_change_spec')
            if isinstance(existing_envelope, dict):
                if 'metadata' in attrs:
                    proposed_metadata = attrs.get('metadata')
                    proposed_envelope = (
                        proposed_metadata.get('typed_change_spec')
                        if isinstance(proposed_metadata, dict)
                        else None
                    )
                    if proposed_envelope != existing_envelope:
                        raise serializers.ValidationError({
                            'metadata': (
                                'Authoritative typed_change_spec cannot be changed by generic task '
                                'update. Use pulse_revise_workflow_change while the request is proposed.'
                            )
                        })

                if 'description' in attrs:
                    from .typed_change_integrity import extract_typed_envelope

                    projected_envelope = extract_typed_envelope(attrs.get('description') or '')
                    if projected_envelope != existing_envelope:
                        raise serializers.ValidationError({
                            'description': (
                                'The typed Change Specification is derived from authoritative metadata '
                                'and cannot be edited independently. Use pulse_revise_workflow_change.'
                            )
                        })
        return attrs

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name not in self.Meta.read_only_fields:
                field.required = False


TaskUpdateSerializer = ItemUpdateSerializer


class DeploymentEventSerializer(serializers.ModelSerializer):
    """Deployment ledger row exposed to CI/QA consumers."""
    project_key = serializers.CharField(source='project.key', read_only=True, default=None)
    failure_task_key = serializers.CharField(source='failure_task.item_key', read_only=True, default=None)
    qa_task_key = serializers.CharField(source='qa_task.item_key', read_only=True, default=None)
    is_failure = serializers.BooleanField(read_only=True)

    class Meta:
        model = DeploymentEvent
        fields = [
            'id', 'idempotency_key', 'project', 'project_key',
            'repo', 'branch', 'commit_sha', 'service', 'component', 'environment',
            'cluster', 'namespace', 'deployment_target', 'image', 'image_tag',
            'image_digest', 'manifest_ref', 'pipeline_system', 'pipeline_run_id',
            'pipeline_url', 'logs_url', 'state', 'smoke_result', 'started_at',
            'rollout_started_at', 'rolled_out_at', 'smoke_started_at',
            'smoke_finished_at', 'pr_numbers', 'source_task_keys', 'links',
            'metadata', 'rollback_of', 'superseded_by', 'failure_task',
            'failure_task_key', 'qa_task', 'qa_task_key', 'is_failure',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'failure_task', 'failure_task_key', 'created_at', 'updated_at']


class DeploymentEventIngestSerializer(serializers.Serializer):
    """CI-facing idempotent upsert payload for DeploymentEvent."""
    idempotency_key = serializers.CharField(max_length=512, required=False, allow_blank=True)
    project_id = serializers.IntegerField(required=False, allow_null=True)
    project_key = serializers.CharField(max_length=10, required=False, allow_blank=True)
    repo = serializers.CharField(max_length=255)
    branch = serializers.CharField(max_length=255, required=False, allow_blank=True)
    commit_sha = serializers.CharField(max_length=64)
    service = serializers.CharField(max_length=160)
    component = serializers.CharField(max_length=160, required=False, allow_blank=True)
    environment = serializers.CharField(max_length=80, required=False, allow_blank=True, default='live')
    cluster = serializers.CharField(max_length=120, required=False, allow_blank=True, default='shizuha-k3s')
    namespace = serializers.CharField(max_length=160, required=False, allow_blank=True)
    deployment_target = serializers.CharField(max_length=255, required=False, allow_blank=True)
    image = serializers.CharField(max_length=512, required=False, allow_blank=True)
    image_tag = serializers.CharField(max_length=255, required=False, allow_blank=True)
    image_digest = serializers.CharField(max_length=255, required=False, allow_blank=True)
    manifest_ref = serializers.CharField(max_length=512, required=False, allow_blank=True)
    pipeline_system = serializers.CharField(max_length=120, required=False, allow_blank=True, default='origin-forgejo')
    pipeline_run_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    pipeline_url = serializers.URLField(max_length=2048, required=False, allow_blank=True)
    logs_url = serializers.URLField(max_length=2048, required=False, allow_blank=True)
    state = serializers.ChoiceField(choices=[c[0] for c in DeploymentEvent.STATE_CHOICES], required=False, default='created')
    smoke_result = serializers.ChoiceField(choices=[c[0] for c in DeploymentEvent.SMOKE_RESULT_CHOICES], required=False, default='unknown')
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    rollout_started_at = serializers.DateTimeField(required=False, allow_null=True)
    rolled_out_at = serializers.DateTimeField(required=False, allow_null=True)
    smoke_started_at = serializers.DateTimeField(required=False, allow_null=True)
    smoke_finished_at = serializers.DateTimeField(required=False, allow_null=True)
    pr_numbers = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    source_task_keys = serializers.ListField(child=serializers.CharField(max_length=50), required=False, default=list)
    links = serializers.JSONField(required=False, default=dict)
    metadata = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        attrs = dict(attrs)
        attrs['repo'] = attrs['repo'].strip()
        attrs['service'] = attrs['service'].strip()
        attrs['commit_sha'] = attrs['commit_sha'].strip()
        for field in ('repo', 'service', 'commit_sha'):
            if not attrs[field]:
                raise serializers.ValidationError({field: 'This field may not be blank.'})
        return attrs


class VerificationRequirementManifestSerializer(serializers.ModelSerializer):
    item_key = serializers.CharField(source='item.item_key', read_only=True)

    class Meta:
        model = VerificationRequirementManifest
        fields = [
            'id', 'item', 'item_key', 'requirement_generation', 'classification',
            'merge_commit', 'merge_attestation', 'pull_request_repo',
            'pull_request_number', 'environment', 'service', 'workload',
            'cluster', 'namespace',
            'affected_components', 'scrape_targets', 'metric_series',
            'prometheus_rules', 'manifest_digest', 'reviewed_by_id',
            'reviewed_by_email', 'reviewed_at', 'is_current', 'superseded_at',
            'created_at',
        ]


class VerificationRequirementManifestCreateSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    merge_commit = serializers.CharField(max_length=64)
    pull_request_repo = serializers.CharField(max_length=255)
    pull_request_number = serializers.IntegerField(min_value=1)
    environment = serializers.CharField(max_length=80, required=False, allow_blank=True, default='')
    service = serializers.CharField(max_length=160, required=False, allow_blank=True, default='')
    workload = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    # PLS-521 P1 #3: the service requires cluster+namespace for live_required.
    cluster = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    namespace = serializers.CharField(max_length=160, required=False, allow_blank=True, default='')
    affected_components = serializers.ListField(
        child=serializers.CharField(max_length=255), required=False, default=list,
    )
    scrape_targets = serializers.ListField(
        child=serializers.CharField(max_length=255), required=False, default=list,
    )
    metric_series = serializers.ListField(
        child=serializers.CharField(max_length=255), required=False, default=list,
    )
    prometheus_rules = serializers.DictField(
        child=serializers.CharField(max_length=71), required=False, default=dict,
    )

    # PLS-1375 (PLAT-7689/7324): legacy clients (akira's proven freeze recipe)
    # send `components` — DRF silently dropped the unknown key, leaving
    # affected_components empty, and the fail-closed derivation then surfaced as
    # a middleware 500. Accept the legacy alias explicitly: rename it to the
    # schema field before field validation so type errors land as normal 400
    # field errors on `affected_components`. If both keys are present the
    # payload is ambiguous — reject instead of guessing precedence.
    def to_internal_value(self, data):
        if isinstance(data, dict) and 'components' in data:
            if 'affected_components' in data:
                raise serializers.ValidationError({
                    'affected_components': (
                        "Pass either 'affected_components' or its legacy alias "
                        "'components', not both."
                    ),
                })
            # Copy, don't mutate the caller's request.data.
            data = {**data, 'affected_components': data['components']}
            data.pop('components', None)
        return super().to_internal_value(data)


class DeploymentEvidenceSerializer(serializers.ModelSerializer):
    item_key = serializers.CharField(source='item.item_key', read_only=True)

    class Meta:
        model = DeploymentEvidence
        fields = [
            'id', 'item', 'item_key', 'requirement_generation', 'evidence_generation',
            'merge_commit', 'artifact_digest', 'config_digest', 'build_provenance',
            'environment', 'service', 'cluster', 'namespace', 'workload',
            'workload_uid', 'rollout_revision', 'workload_generation',
            'observed_generation', 'ready', 'ready_at', 'target_health',
            'metric_series', 'prometheus_rules', 'producer_id', 'producer_identity',
            'evidence_digest', 'recorded_at', 'created_at',
        ]


class DeploymentEvidenceCreateSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    merge_commit = serializers.CharField(max_length=64)
    artifact_digest = serializers.CharField(max_length=255)
    config_digest = serializers.CharField(max_length=255)
    build_provenance = serializers.JSONField()
    environment = serializers.CharField(max_length=80)
    service = serializers.CharField(max_length=160)
    cluster = serializers.CharField(max_length=120)
    namespace = serializers.CharField(max_length=160)
    workload = serializers.CharField(max_length=255)
    workload_uid = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    rollout_revision = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    workload_generation = serializers.IntegerField(min_value=1)
    observed_generation = serializers.IntegerField(min_value=0)
    ready = serializers.BooleanField()
    ready_at = serializers.DateTimeField()
    target_health = serializers.JSONField(required=False, default=dict)
    metric_series = serializers.JSONField(required=False, default=dict)
    prometheus_rules = serializers.JSONField(required=False, default=dict)


class DeploymentEvidenceInvalidateSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    reason = serializers.ChoiceField(choices=[
        'rollback', 'redeploy', 'workload_replacement', 'rule_change',
        'target_disappearance', 'newer_merge',
    ])


# =============================================================================
# Alert-Specific Serializers
# =============================================================================

class AlertSerializer(ItemSerializer):
    """
    Specialized serializer for alerts with additional computed fields.
    """
    firing_duration = serializers.SerializerMethodField()
    is_inhibited = serializers.SerializerMethodField()

    class Meta(ItemSerializer.Meta):
        fields = ItemSerializer.Meta.fields + ['firing_duration', 'is_inhibited']

    def get_firing_duration(self, obj):
        """Get how long the alert has been firing."""
        if obj.first_fired_at and obj.status in ['firing', 'acknowledged']:
            delta = timezone.now() - obj.first_fired_at
            return int(delta.total_seconds())
        return None

    def get_is_inhibited(self, obj):
        """Check if alert is inhibited by another alert."""
        from tasks.services.inhibition import InhibitionService
        return InhibitionService.is_inhibited(obj)


class AlertSubmitSerializer(serializers.Serializer):
    """
    Serializer for submitting new alerts (handles deduplication internally).
    """
    item_type = serializers.CharField(default='alert.error')
    title = serializers.CharField(max_length=400)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    source = serializers.CharField(max_length=100)
    source_id = serializers.CharField(max_length=255, required=False, allow_null=True)
    source_url = serializers.URLField(max_length=2048, required=False, allow_null=True)
    severity = serializers.ChoiceField(
        choices=['info', 'warning', 'error', 'critical'],
        default='warning'
    )
    priority = serializers.ChoiceField(
        choices=['low', 'normal', 'high', 'urgent'],
        default='normal'
    )
    labels = serializers.JSONField(default=dict)
    payload = serializers.JSONField(default=dict)
    due_date = serializers.DateTimeField(required=False, allow_null=True)
    correlation_id = serializers.CharField(max_length=255, required=False, allow_null=True)
    project_id = serializers.IntegerField(required=False, allow_null=True)
    assignment_group = serializers.CharField(max_length=100, required=False, allow_null=True)
    assignee_id = serializers.IntegerField(required=False, allow_null=True)
    assignee_email = serializers.EmailField(required=False, allow_null=True)
    status = serializers.CharField(max_length=20, required=False, allow_null=True, default=None)

    def validate_assignment_group(self, value):
        from .services.workflow_service import WorkflowService
        return WorkflowService._canonical_team_name(value) if value else value

    def validate(self, data):
        """Bind alert routing to the selected project's exact organization."""
        team_name = data.get('assignment_group')
        if not team_name:
            return data
        project_id = data.get('project_id')
        if not project_id:
            raise serializers.ValidationError({
                'assignment_group': 'A project is required when routing an alert to a Team.',
            })
        project = Project.objects.filter(
            pk=project_id,
            is_active=True,
            is_archived=False,
        ).first()
        if project is None:
            raise serializers.ValidationError({'project_id': 'Project is unavailable.'})
        from .services.workflow_service import WorkflowService
        probe = Item(project=project, assignment_group=team_name)
        if not WorkflowService._team_registered_for_item(team_name, probe):
            raise serializers.ValidationError({
                'assignment_group': (
                    'Team is not registered for the selected project organization.'
                ),
            })
        assignee_email = data.get('assignee_email')
        if assignee_email and WorkflowService.assignment_org_mismatch(
            probe,
            assignee_email,
            team_name,
            user_id=data.get('assignee_id'),
        )[0]:
            raise serializers.ValidationError({
                'assignee_email': 'Assignee is not authorized for this project Team.',
            })
        return data


class AlertAcknowledgeSerializer(serializers.Serializer):
    """Serializer for acknowledging an alert."""
    comment = serializers.CharField(required=False, allow_blank=True, default='')


class AlertResolveSerializer(serializers.Serializer):
    """Serializer for resolving an alert."""
    resolution_note = serializers.CharField(required=False, allow_blank=True, default='')


class AlertSilenceSerializer(serializers.Serializer):
    """Serializer for silencing an alert."""
    duration_minutes = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


# =============================================================================
# Action-Specific Serializers
# =============================================================================

class ActionSerializer(ItemSerializer):
    """
    Specialized serializer for actions.
    """
    class Meta(ItemSerializer.Meta):
        pass


class ActionSubmitSerializer(serializers.Serializer):
    """
    Serializer for submitting new actions.
    """
    item_type = serializers.CharField(default='action.generic')
    title = serializers.CharField(max_length=400)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    source = serializers.CharField(max_length=100)
    source_id = serializers.CharField(max_length=255, required=False, allow_null=True)
    priority = serializers.ChoiceField(
        choices=['low', 'normal', 'high', 'urgent'],
        default='normal'
    )
    payload = serializers.JSONField(default=dict)
    labels = serializers.JSONField(default=dict)
    callback_url = serializers.URLField(required=False, allow_null=True)
    callback_method = serializers.ChoiceField(
        choices=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
        default='POST'
    )
    callback_headers = serializers.JSONField(default=dict)
    requires_approval = serializers.BooleanField(default=True)
    due_date = serializers.DateTimeField(required=False, allow_null=True)


class ActionApproveSerializer(serializers.Serializer):
    """Serializer for approving an action."""
    comment = serializers.CharField(required=False, allow_blank=True, default='')


class ActionRejectSerializer(serializers.Serializer):
    """Serializer for rejecting an action."""
    reason = serializers.CharField(required=True)


# =============================================================================
# Rule Serializers
# =============================================================================

class ApprovalRuleSerializer(serializers.ModelSerializer):
    """Serializer for ApprovalRule model."""
    class Meta:
        model = ApprovalRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class SilenceRuleSerializer(serializers.ModelSerializer):
    """Serializer for SilenceRule model."""
    is_currently_active = serializers.SerializerMethodField()

    class Meta:
        model = SilenceRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'match_count', 'last_matched_at']

    def get_is_currently_active(self, obj):
        return obj.is_currently_active()


class SilenceRuleCreateSerializer(serializers.Serializer):
    """Serializer for creating silence rules with duration support."""
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    duration_minutes = serializers.IntegerField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    modes = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    item_types = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    sources = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    severities = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    label_matchers = serializers.JSONField(required=False, default=dict)
    created_by = serializers.CharField(required=False, allow_blank=True, default='')
    reason = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, data):
        if not data.get('duration_minutes') and not data.get('ends_at'):
            # Allow indefinite silences
            pass
        return data


class EscalationRuleSerializer(serializers.ModelSerializer):
    """Serializer for EscalationRule model."""
    class Meta:
        model = EscalationRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class InhibitionRuleSerializer(serializers.ModelSerializer):
    """Serializer for InhibitionRule model."""
    class Meta:
        model = InhibitionRule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Subscription & Config Serializers
# =============================================================================

class SubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for Subscription model."""
    class Meta:
        model = Subscription
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class FingerprintConfigSerializer(serializers.ModelSerializer):
    """Serializer for FingerprintConfig model."""
    class Meta:
        model = FingerprintConfig
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Summary/Stats Serializers
# =============================================================================



class AiGenerationRunSerializer(serializers.ModelSerializer):
    project_key = serializers.CharField(source='project.key', read_only=True)
    sprint_name = serializers.CharField(source='sprint.name', read_only=True)

    class Meta:
        model = AiGenerationRun
        fields = [
            'id', 'org_id', 'project', 'project_key', 'sprint', 'sprint_name',
            'type', 'status', 'model', 'prompt_template_version',
            'source_window_start', 'source_window_end', 'source_snapshot_hash',
            'source_counts', 'output', 'citations', 'feedback', 'created_by_id',
            'created_by_email', 'client_id', 'error_message', 'created_at',
            'updated_at', 'completed_at',
        ]
        read_only_fields = [
            'id', 'org_id', 'project_key', 'sprint_name', 'status',
            'source_snapshot_hash', 'source_counts', 'output', 'citations',
            'feedback', 'created_by_id', 'created_by_email', 'error_message',
            'created_at', 'updated_at', 'completed_at',
        ]


class DailySummarySerializer(serializers.Serializer):
    """Serializer for daily summary response."""
    tasks_due_today = serializers.IntegerField()
    tasks_overdue = serializers.IntegerField()
    actions_pending = serializers.IntegerField()
    alerts_firing = serializers.IntegerField()
    alerts_acknowledged = serializers.IntegerField()
    tasks = ItemListSerializer(many=True)
    actions = ItemListSerializer(many=True)
    alerts = AlertSerializer(many=True)


class StatisticsSerializer(serializers.Serializer):
    """Serializer for statistics response."""
    total_items = serializers.IntegerField()
    by_mode = serializers.DictField()
    by_status = serializers.DictField()
    by_priority = serializers.DictField()
    by_severity = serializers.DictField()
    alerts_last_24h = serializers.IntegerField()
    actions_last_24h = serializers.IntegerField()
    # Org-wide headline velocity metric (PLAT-171); my-scoped value kept separately.
    tasks_completed_last_24h = serializers.IntegerField()
    my_tasks_completed_last_24h = serializers.IntegerField()
    # Org-wide task rollup that reconciles with by_mode.task (PLAT-171).
    global_tasks = serializers.DictField()


# =============================================================================
# Backward Compatibility - RecurringTaskSerializer
# =============================================================================

class RecurringTaskSerializer(serializers.ModelSerializer):
    """
    Specialized serializer for recurring tasks (backward compatible).
    """
    recurrence_pattern_dict = serializers.SerializerMethodField()
    child_tasks_count = serializers.SerializerMethodField()
    # PLS-792: recurring-cycle freshness on the recurring_tasks list surface.
    recurring_cycle = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            'id', 'title', 'description', 'item_type', 'frequency',
            'recurrence_pattern', 'recurrence_pattern_dict', 'recurrence_end_date',
            'is_recurring', 'recurring_cycle', 'child_tasks_count', 'assignee_id', 'assignee_email', 'assignment_group', 'workflow_name', 'workflow_status',
            'priority', 'status', 'created_at'
        ]

    def get_recurrence_pattern_dict(self, obj):
        return obj.get_recurrence_pattern_dict()

    def get_recurring_cycle(self, obj):
        """PLS-792: recurring-cycle freshness payload for the recurring list."""
        from .services.recurring_cycle_state import cycle_snapshot
        return cycle_snapshot(obj)

    def get_child_tasks_count(self, obj):
        """Count child tasks generated from this recurring task."""
        return Item.objects.filter(parent_item=obj).count()


# =============================================================================
# User Notification Channel Serializers
# =============================================================================

class UserNotificationChannelSerializer(serializers.ModelSerializer):
    """Serializer for UserNotificationChannel model."""
    is_within_quiet_hours = serializers.SerializerMethodField()

    class Meta:
        model = UserNotificationChannel
        fields = [
            'id', 'user_id', 'user_email', 'name', 'channel_type', 'is_active',
            'discord_webhook_url', 'telegram_bot_token', 'telegram_chat_id',
            'notify_on_reminder', 'notify_on_overdue', 'notify_on_alert', 'notify_on_action',
            'min_alert_severity', 'quiet_hours_start', 'quiet_hours_end',
            'created_at', 'updated_at', 'last_notification_at', 'last_error', 'error_count',
            'is_within_quiet_hours'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'last_notification_at',
            'last_error', 'error_count', 'is_within_quiet_hours'
        ]
        extra_kwargs = {
            'telegram_bot_token': {'write_only': True},  # Don't expose tokens in responses
        }

    def get_is_within_quiet_hours(self, obj):
        return obj.is_within_quiet_hours()

    def validate(self, data):
        channel_type = data.get('channel_type', getattr(self.instance, 'channel_type', None))

        if channel_type == 'discord':
            webhook_url = data.get('discord_webhook_url', getattr(self.instance, 'discord_webhook_url', None))
            if not webhook_url:
                raise serializers.ValidationError({
                    'discord_webhook_url': 'Discord webhook URL is required for Discord channels'
                })

        elif channel_type == 'telegram':
            bot_token = data.get('telegram_bot_token', getattr(self.instance, 'telegram_bot_token', None))
            chat_id = data.get('telegram_chat_id', getattr(self.instance, 'telegram_chat_id', None))
            if not bot_token:
                raise serializers.ValidationError({
                    'telegram_bot_token': 'Bot token is required for Telegram channels'
                })
            if not chat_id:
                raise serializers.ValidationError({
                    'telegram_chat_id': 'Chat ID is required for Telegram channels'
                })

        return data


class UserNotificationChannelCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating notification channels.
    user_id and user_email are set automatically from the authenticated user
    in the view's perform_create() method.
    """
    class Meta:
        model = UserNotificationChannel
        fields = [
            'user_id', 'user_email', 'name', 'channel_type', 'is_active',
            'discord_webhook_url', 'telegram_bot_token', 'telegram_chat_id',
            'notify_on_reminder', 'notify_on_overdue', 'notify_on_alert', 'notify_on_action',
            'min_alert_severity', 'quiet_hours_start', 'quiet_hours_end',
        ]
        # user_id and user_email are set automatically by the view from the authenticated user
        read_only_fields = ['user_id', 'user_email']

    def validate(self, data):
        channel_type = data.get('channel_type')

        if channel_type == 'discord':
            if not data.get('discord_webhook_url'):
                raise serializers.ValidationError({
                    'discord_webhook_url': 'Discord webhook URL is required for Discord channels'
                })

        elif channel_type == 'telegram':
            if not data.get('telegram_bot_token'):
                raise serializers.ValidationError({
                    'telegram_bot_token': 'Bot token is required for Telegram channels'
                })
            if not data.get('telegram_chat_id'):
                raise serializers.ValidationError({
                    'telegram_chat_id': 'Chat ID is required for Telegram channels'
                })

        return data


class TestNotificationSerializer(serializers.Serializer):
    """Serializer for testing a notification channel."""
    message = serializers.CharField(
        required=False,
        default="This is a test notification from Shizuha Hub!",
        help_text="Optional custom test message"
    )




def _request_user_org_ids(serializer):
    request = serializer.context.get('request') if hasattr(serializer, 'context') else None
    user = getattr(request, 'user', None)
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return None
    memberships = getattr(user, 'organization_memberships', {}) or {}
    try:
        return {int(k) for k in memberships.keys()}
    except (TypeError, ValueError):
        return set()


def _scoped_project_queryset(serializer):
    org_ids = _request_user_org_ids(serializer)
    qs = Project.objects.all()
    return qs if org_ids is None else qs.filter(org_id__in=org_ids)


def _scoped_item_queryset(serializer):
    org_ids = _request_user_org_ids(serializer)
    qs = Item.objects.select_related('project')
    return qs if org_ids is None else qs.filter(project__org_id__in=org_ids)


def _scoped_objective_queryset(serializer):
    org_ids = _request_user_org_ids(serializer)
    qs = Objective.objects.all()
    return qs if org_ids is None else qs.filter(org_id__in=org_ids)


class KeyResultSerializer(serializers.ModelSerializer):
    progress_percent = serializers.IntegerField(read_only=True)

    class Meta:
        model = KeyResult
        fields = [
            'id', 'objective', 'title', 'metric_name', 'current_value', 'target_value',
            'unit', 'status', 'confidence', 'progress_percent', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'progress_percent', 'created_at', 'updated_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['objective'].queryset = _scoped_objective_queryset(self)

    def validate_confidence(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError('Confidence must be between 0 and 100.')
        return value


class ObjectiveSerializer(serializers.ModelSerializer):
    progress_percent = serializers.IntegerField(read_only=True)
    key_results = KeyResultSerializer(many=True, read_only=True)
    project_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Project.objects.all(), source='projects', required=False
    )
    item_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Item.objects.all(), source='items', required=False
    )
    key_result_count = serializers.SerializerMethodField()
    linked_item_count = serializers.SerializerMethodField()
    linked_project_count = serializers.SerializerMethodField()

    class Meta:
        model = Objective
        fields = [
            'id', 'org_id', 'project', 'title', 'description', 'owner_id', 'owner_email',
            'time_period', 'start_date', 'target_date', 'status', 'project_ids', 'item_ids',
            'key_results', 'progress_percent', 'key_result_count', 'linked_item_count',
            'linked_project_count', 'created_by_id', 'created_by_email', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'progress_percent', 'key_results', 'key_result_count', 'linked_item_count',
            'linked_project_count', 'created_by_id', 'created_by_email', 'created_at', 'updated_at'
        ]

    def get_key_result_count(self, obj):
        annotated = getattr(obj, '_key_result_count', None)
        return annotated if annotated is not None else obj.key_results.count()

    def get_linked_item_count(self, obj):
        annotated = getattr(obj, '_linked_item_count', None)
        return annotated if annotated is not None else obj.items.count()

    def get_linked_project_count(self, obj):
        annotated = getattr(obj, '_linked_project_count', None)
        return annotated if annotated is not None else obj.projects.count()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        scoped_projects = _scoped_project_queryset(self)
        self.fields['project'].queryset = scoped_projects
        self.fields['project_ids'].queryset = scoped_projects
        self.fields['item_ids'].queryset = _scoped_item_queryset(self)

    def validate(self, attrs):
        org_id = attrs.get('org_id') if 'org_id' in attrs else getattr(self.instance, 'org_id', None)
        user_org_ids = _request_user_org_ids(self)
        if user_org_ids is not None and org_id not in user_org_ids:
            raise serializers.ValidationError({'org_id': 'You are not a member of this organization.'})
        if self.instance is not None and 'org_id' in attrs and attrs['org_id'] != self.instance.org_id:
            raise serializers.ValidationError({'org_id': 'Objective org_id cannot be changed.'})

        project = attrs.get('project') if 'project' in attrs else getattr(self.instance, 'project', None)
        if project and org_id and project.org_id != org_id:
            raise serializers.ValidationError({'project': 'Primary project must belong to the objective org_id.'})
        for project in attrs.get('projects', []):
            if org_id and project.org_id != org_id:
                raise serializers.ValidationError({'project_ids': 'All linked projects must belong to the objective org_id.'})
        for item in attrs.get('items', []):
            if item.project_id and org_id and item.project.org_id != org_id:
                raise serializers.ValidationError({'item_ids': 'All linked project items must belong to the objective org_id.'})
        return attrs

# =============================================================================
# Project Serializers
# Note: Organization management has moved to shizuha-admin.
# Use /admin/api/organizations/ for org CRUD operations.
# =============================================================================

class ProjectSerializer(serializers.ModelSerializer):
    """Serializer for Project model."""
    organization = serializers.SerializerMethodField()
    organization_name = serializers.SerializerMethodField()
    organization_slug = serializers.SerializerMethodField()
    entity_key = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    can_create = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id', 'org_id', 'organization', 'organization_name', 'organization_slug',
            'entity_key', 'name', 'key', 'description',
            'project_type', 'access_level',
            'lead_id', 'lead_email',
            'default_assignee_id', 'default_assignee_email',
            'next_item_number', 'settings',
            'icon', 'color',
            'is_archived', 'archived_at', 'is_active',
            'created_at', 'updated_at', 'created_by_id',
            'member_count', 'item_count', 'can_create'
        ]
        read_only_fields = [
            'id', 'next_item_number', 'is_archived', 'archived_at',
            'created_at', 'updated_at', 'member_count', 'item_count',
            'organization', 'organization_name', 'organization_slug', 'entity_key',
            'can_create',
        ]

    def get_organization(self, obj):
        """Return org_id for backward compatibility."""
        return obj.org_id

    def get_organization_name(self, obj):
        """Fetch organization name from shizuha-admin."""
        org_data = obj.get_organization()
        return _dict_field(org_data, 'name')

    def get_organization_slug(self, obj):
        """Fetch organization slug from shizuha-admin."""
        org_data = obj.get_organization()
        return _dict_field(org_data, 'slug')

    def get_entity_key(self, obj):
        return _project_entity_key(obj)

    def validate_org_id(self, value):
        if self.instance is not None and int(value) != int(self.instance.org_id):
            raise serializers.ValidationError(
                'A project cannot be moved to another organization.'
            )
        return value

    def get_member_count(self, obj):
        annotated = getattr(obj, '_member_count', None)
        if annotated is not None:
            return annotated
        return obj.memberships.filter(is_active=True).count()

    def get_item_count(self, obj):
        annotated = getattr(obj, '_item_count', None)
        if annotated is not None:
            return annotated
        return obj.items.count()

    def get_can_create(self, obj):
        """Expose the caller's effective item-create permission for this project.

        Project-list visibility is intentionally broader than writability.  The
        MCP workflow-change proposer uses this positive capability bit as its
        fail-closed authorization source, rather than treating every visible
        project as writable (PLS-574 review finding).

        PLS-708: this MUST share one source of truth with the create-item
        authorization (`PermissionService.can_access_project(permission='create')`
        — the exact check the ItemViewSet.create path runs). The previous
        implementation returned True for every `access_level == 'open'` project
        without verifying org membership, and for membership-based projects
        ignored the org-membership gate that `can_access_project` enforces first.
        A principal who could read a project therefore saw `can_create=true`
        while the create attempt was rejected with a 400 — a contradictory
        capability contract (focused-QA ANDON on PLAT-4838).
        """
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
            return True

        # PLS-708: an archived/inactive project cannot accept new items — the
        # create path rejects it before the permission check (ItemViewSet.create:
        # 'This project is archived and no longer accepts new items.'). The
        # capability bit must agree, or a cached archived project advertises
        # writability it cannot satisfy.
        if getattr(obj, 'is_archived', False) or not getattr(obj, 'is_active', True):
            return False

        from .services.permissions import PermissionService
        return PermissionService.can_access_project(
            user.id, obj, permission='create',
            user_email=getattr(user, 'email', None),
            user=user,
        )


class ProjectCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating projects."""
    # Accept 'organization' as alias for 'org_id' for backward compatibility
    organization = serializers.IntegerField(write_only=True, required=False)
    # Make org_id not required at field level - we validate in validate()
    org_id = serializers.IntegerField(required=False)

    class Meta:
        model = Project
        # Report the scoped key collision on ``key`` in validate() below. DRF's
        # generated UniqueTogetherValidator runs before validate() and exposes
        # this as a vague non_field_errors response.
        validators = []
        fields = [
            'org_id', 'organization', 'name', 'key', 'description',
            'project_type', 'access_level',
            'lead_id', 'lead_email',
            'default_assignee_id', 'default_assignee_email',
            'icon', 'color'
        ]

    def validate_key(self, value):
        value = value.upper()
        if len(value) < 2 or len(value) > 10:
            raise serializers.ValidationError("Key must be 2-10 characters")
        if not value.replace('_', '').replace('-', '').isalnum():
            raise serializers.ValidationError("Key must contain only letters, numbers, underscores, and hyphens")
        return value

    def validate(self, data):
        # Handle 'organization' alias for 'org_id'
        if 'organization' in data:
            if 'org_id' not in data or data.get('org_id') is None:
                data['org_id'] = data.pop('organization')
            else:
                data.pop('organization')

        org_id = data.get('org_id')
        key = data.get('key', '').upper()

        if not org_id:
            raise serializers.ValidationError({
                'org_id': 'Organization ID is required (you can also use "organization")'
            })

        if Project.objects.filter(org_id=org_id, key=key).exists():
            raise serializers.ValidationError({
                'key': f"Project with key '{key}' already exists in this organization"
            })

        return data


class ProjectMembershipSerializer(serializers.ModelSerializer):
    """Serializer for ProjectMembership."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMembership
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'user_id', 'user_email', 'user_name', 'role',
            'can_browse', 'can_create', 'can_edit', 'can_delete',
            'can_manage_project', 'can_manage_members',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'project_key', 'project_name']

    def get_user_name(self, obj):
        """Get display name for the user - prefer username over email prefix."""
        from .user_service import user_service
        if obj.user_id:
            user_info = user_service.get_user_by_id(obj.user_id)
            if user_info:
                # Prefer username, then first_name + last_name, then email prefix
                if user_info.get('username'):
                    return user_info['username']
                if user_info.get('first_name'):
                    name = user_info['first_name']
                    if user_info.get('last_name'):
                        name += ' ' + user_info['last_name']
                    return name
                if user_info.get('email'):
                    return user_info['email'].split('@')[0]
        # Fallback to email prefix
        if obj.user_email:
            return obj.user_email.split('@')[0]
        return 'Unknown'


# =============================================================================
# Sprint Serializers
# =============================================================================

class SprintSerializer(serializers.ModelSerializer):
    """Serializer for Sprint model."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    item_count = serializers.SerializerMethodField()
    completed_item_count = serializers.SerializerMethodField()
    total_points = serializers.SerializerMethodField()
    velocity = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()

    class Meta:
        model = Sprint
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'name', 'goal',
            'start_date', 'end_date',
            'status', 'started_at', 'completed_at',
            'committed_points', 'completed_points',
            'created_by_id', 'created_by_email',
            'created_at', 'updated_at',
            # Computed fields
            'item_count', 'completed_item_count', 'total_points',
            'velocity', 'progress_percent'
        ]
        read_only_fields = [
            'id', 'project_key', 'project_name',
            'started_at', 'completed_at',
            'committed_points', 'completed_points',
            'created_at', 'updated_at',
            'item_count', 'completed_item_count', 'total_points',
            'velocity', 'progress_percent'
        ]

    def get_item_count(self, obj):
        return obj.items.filter(mode='task').count()

    def get_completed_item_count(self, obj):
        return obj.items.filter(mode='task', status='completed').count()

    def get_total_points(self, obj):
        from django.db.models import Sum
        return obj.items.filter(mode='task').aggregate(
            total=Sum('story_points')
        )['total'] or 0

    def get_velocity(self, obj):
        return obj.get_velocity()

    def get_progress_percent(self, obj):
        total = self.get_item_count(obj)
        if total == 0:
            return 0
        completed = self.get_completed_item_count(obj)
        return round((completed / total) * 100)


class SprintCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating sprints."""

    class Meta:
        model = Sprint
        fields = [
            'project', 'name', 'goal',
            'start_date', 'end_date'
        ]

    def validate(self, data):
        # Validate dates
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        if start_date and end_date and start_date > end_date:
            raise serializers.ValidationError("Start date cannot be after end date")
        return data


class SprintListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for sprint list views."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    item_count = serializers.SerializerMethodField()
    completed_item_count = serializers.SerializerMethodField()

    class Meta:
        model = Sprint
        fields = [
            'id', 'project', 'project_key',
            'name', 'goal',
            'start_date', 'end_date',
            'status', 'committed_points', 'completed_points',
            'item_count', 'completed_item_count'
        ]

    def get_item_count(self, obj):
        return obj.items.filter(mode='task').count()

    def get_completed_item_count(self, obj):
        return obj.items.filter(mode='task', status='completed').count()


class ProjectMemberAddSerializer(serializers.Serializer):
    """Serializer for adding members to a project."""
    user_id = serializers.IntegerField(required=False)
    user_email = serializers.EmailField(required=False)
    role = serializers.ChoiceField(
        choices=['admin', 'member', 'viewer'],
        default='member'
    )

    def validate(self, data):
        if not data.get('user_id') and not data.get('user_email'):
            raise serializers.ValidationError("Either user_id or user_email is required")
        return data


# =============================================================================
# Comment Serializers
# =============================================================================

class CommentSerializer(serializers.ModelSerializer):
    """Serializer for Comment model."""
    author_display_name = serializers.SerializerMethodField()
    reply_count = serializers.SerializerMethodField()
    # PLS-518: machine-readable comment classing — system/lifecycle comments
    # (failover, rebalance, moved-to-backlog, auto-block/unblock, routability
    # holds, validator/resolver audits) vs substantive human/agent content.
    # Derived from the existing origin_kind provenance so no schema change is
    # needed; LEGACY_UNTYPED rows predate typing and are treated as substantive
    # (never hide potentially meaningful old content by default).
    is_system = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'item', 'author_id', 'author_email', 'author_name',
            'author_display_name', 'content', 'parent',
            'origin_kind', 'origin_event', 'template_version', 'origin_event_id',
            'origin_payload', 'caused_by_user_id', 'caused_by_email_snapshot',
            'created_at', 'updated_at', 'is_edited', 'reply_count',
            'is_system',
        ]
        read_only_fields = [
            'id', 'author_id', 'author_email', 'author_name', 'is_edited',
            'origin_kind', 'origin_event', 'template_version', 'origin_event_id',
            'origin_payload', 'caused_by_user_id', 'caused_by_email_snapshot',
            'created_at', 'updated_at', 'is_system',
        ]

    def get_author_display_name(self, obj):
        """Return author's display name or email username."""
        if obj.author_name:
            return obj.author_name
        if obj.author_email:
            return obj.author_email.split('@')[0]
        return 'Unknown'

    def get_reply_count(self, obj):
        """Return count of replies to this comment."""
        return obj.replies.count()

    def get_is_system(self, obj):
        """True for workflow-lifecycle/audit/focus-refused comments; False for
        user-authored and legacy-untyped (substantive) content."""
        return obj.origin_kind not in (
            Comment.ORIGIN_USER_AUTHORED,
            Comment.ORIGIN_LEGACY_UNTYPED,
        )


class ItemPKOrKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """PrimaryKey field that also accepts item_key strings like 'AT-83'."""

    def to_internal_value(self, data):
        if isinstance(data, str) and '-' in data and not data.isdigit():
            try:
                return self.get_queryset().get(item_key=data)
            except Item.DoesNotExist:
                self.fail('does_not_exist', pk_value=data)
        return super().to_internal_value(data)


class CommentCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating comments.

    Create responses must expose the stable comment ``id`` (PLAT-5162): the
    approved live tenant-isolation harness (PLAT-4196) and MCP clients address
    the created comment by id for cross-tenant detail/reaction probes, and a
    201 without ``id`` left them unable to reference the new row.
    """
    item = ItemPKOrKeyRelatedField(queryset=Item.objects.all())

    class Meta:
        model = Comment
        fields = ['id', 'item', 'content', 'parent']
        read_only_fields = ['id']

    def validate_content(self, value):
        """Reject empty comments, then redact secrets before persist (PLS-39).

        An empty/whitespace-only body is never a meaningful comment; accepting it
        silently let a looping agent spam hundreds of blank rows on one item
        (sora on item 1293, 2026-07-07) and clutter the activity feed. Reject it
        so the caller gets a clear 400 instead of a silent empty write.
        """
        if value is None or not str(value).strip():
            raise serializers.ValidationError('Comment content must not be empty.')
        cleaned, _ = redact_secrets(value)
        return cleaned

    def validate_parent(self, value):
        """Validate parent comment belongs to the same item."""
        if value:
            item_data = self.initial_data.get('item')
            item_pk = None
            if isinstance(item_data, str) and '-' in item_data and not item_data.isdigit():
                try:
                    item_pk = Item.objects.get(item_key=item_data).pk
                except Item.DoesNotExist:
                    return value
            else:
                try:
                    item_pk = int(item_data)
                except (TypeError, ValueError):
                    return value
            if value.item_id != item_pk:
                raise serializers.ValidationError("Parent comment must belong to the same item")
        return value


# =============================================================================
# Webhook Key Serializers
# =============================================================================

def _validate_webhook_routing_scope(serializer, data):
    """Validate create and update against one exact project/Team boundary."""
    instance = getattr(serializer, 'instance', None)
    project = data.get(
        'default_project',
        getattr(instance, 'default_project', None),
    )
    team_name = data.get(
        'default_assignment_group',
        getattr(instance, 'default_assignment_group', None),
    )
    if team_name and project is None:
        raise serializers.ValidationError({
            'default_assignment_group': 'Select a project before selecting a Team.',
        })

    if project is not None:
        from .services.permissions import PermissionService
        request = serializer.context['request']
        is_visible = PermissionService.get_user_projects(
            user=request.user,
        ).filter(pk=project.pk, is_active=True, is_archived=False).exists()
        can_create = is_visible and PermissionService.can_access_project(
            request.user.id,
            project,
            permission='create',
            user_email=getattr(request.user, 'email', None),
            user=request.user,
        )
        if not can_create:
            raise serializers.ValidationError({
                'default_project': 'Project is unavailable.',
            })

    if team_name:
        from .services.workflow_service import WorkflowService
        canonical_name = WorkflowService._canonical_team_name(team_name)
        probe = Item(project=project, assignment_group=canonical_name)
        if not WorkflowService._team_registered_for_item(canonical_name, probe):
            raise serializers.ValidationError({
                'default_assignment_group': (
                    'Team is not registered for the selected project organization.'
                ),
            })
        data['default_assignment_group'] = canonical_name
    return data


class WebhookKeySerializer(serializers.ModelSerializer):
    """Serializer for WebhookKey model with webhook URL templates."""
    webhook_urls = serializers.SerializerMethodField()

    class Meta:
        model = WebhookKey
        fields = [
            'id', 'user_id', 'user_email', 'name', 'key',
            'integration_type', 'is_active',
            'default_severity', 'source_name', 'auto_labels',
            'default_project', 'default_assignment_group',
            'alerts_received', 'last_used_at',
            'created_at', 'updated_at',
            'webhook_urls'
        ]
        read_only_fields = [
            'id', 'user_id', 'user_email', 'key',
            'alerts_received', 'last_used_at',
            'created_at', 'updated_at'
        ]

    def get_webhook_urls(self, obj):
        """Generate webhook URLs for different integration types."""
        from django.conf import settings

        request = self.context.get('request')
        if request:
            # Build URL preserving the original host and port from the request
            scheme = request.scheme
            host = request.get_host()  # This includes port if non-standard
            base_url = f'{scheme}://{host}'
        else:
            base_url = ''

        # Allow override via settings for production environments
        webhook_base_url = getattr(settings, 'WEBHOOK_BASE_URL', None)
        if webhook_base_url:
            base_url = webhook_base_url.rstrip('/')

        return {
            'grafana': f'{base_url}/pulse/api/webhooks/grafana/{obj.key}/',
            'alertmanager': f'{base_url}/pulse/api/webhooks/alertmanager/{obj.key}/',
            'generic': f'{base_url}/pulse/api/webhooks/generic/{obj.key}/',
        }

    def validate(self, data):
        return _validate_webhook_routing_scope(self, data)


class WebhookKeyCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating webhook keys."""
    class Meta:
        model = WebhookKey
        fields = [
            'name', 'integration_type',
            'default_severity', 'source_name', 'auto_labels',
            'default_project', 'default_assignment_group',
        ]

    def validate_name(self, value):
        """Validate name is unique for this user."""
        user_id = self.context['request'].user.id
        if WebhookKey.objects.filter(user_id=user_id, name=value).exists():
            raise serializers.ValidationError("You already have a webhook key with this name")
        return value

    def validate(self, data):
        return _validate_webhook_routing_scope(self, data)


# =============================================================================
# Attachment Serializers
# =============================================================================

ATTACHMENT_SERVE_TOKEN_SALT = 'pulse.attachment.serve'


def create_attachment_serve_token(attachment_id, kind):
    """Create a short-lived bearer token for browser image/download URLs.

    Attachment metadata is only serialized after item authorization.  The token
    lets normal ``<img>``/download navigation fetch the bytes without exposing
    the file permanently to every unauthenticated caller.
    """
    return signing.dumps(
        {'attachment_id': str(attachment_id), 'kind': str(kind)},
        salt=ATTACHMENT_SERVE_TOKEN_SALT,
        compress=True,
    )


def validate_attachment_serve_token(token, attachment_id, kind):
    if not token:
        return False
    max_age = int(getattr(
        settings,
        'PULSE_ATTACHMENT_SERVE_TOKEN_MAX_AGE_SECONDS',
        300,
    ))
    try:
        payload = signing.loads(
            token,
            salt=ATTACHMENT_SERVE_TOKEN_SALT,
            max_age=max_age,
        )
    except signing.BadSignature:
        return False
    return payload == {
        'attachment_id': str(attachment_id),
        'kind': str(kind),
    }


class AttachmentSerializer(serializers.ModelSerializer):
    """Serializer for Attachment model with download URLs."""
    download_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            'id', 'item', 'storage_key', 'filename', 'file_type', 'file_size',
            'is_image', 'width', 'height', 'thumbnail_key',
            'uploaded_by', 'uploaded_by_id', 'uploaded_by_email', 'source',
            'created_at', 'download_url', 'thumbnail_url'
        ]
        read_only_fields = [
            'id', 'storage_key', 'file_size', 'is_image', 'width', 'height',
            'thumbnail_key', 'uploaded_by_id', 'uploaded_by_email', 'created_at'
        ]

    def get_download_url(self, obj):
        """Generate URL for the serve endpoint (proxied through backend)."""
        token = create_attachment_serve_token(obj.id, 'serve')
        return f'/pulse/api/attachments/{obj.id}/serve/?{urlencode({"token": token})}'

    def get_uploaded_by(self, obj):
        """PLS-1351: canary discriminator — the uploader User PK at create time.

        Alias of ``uploaded_by_id`` exposed under the name the attachment
        canary (PLS-786/PLS-1351) reads, so a create/destroy identity mismatch
        is observable live from detail responses instead of being inferred
        from the 403.
        """
        return obj.uploaded_by_id

    def get_thumbnail_url(self, obj):
        """Generate URL for the thumbnail endpoint."""
        if not obj.is_image or not obj.thumbnail_key:
            return None
        token = create_attachment_serve_token(obj.id, 'thumbnail')
        return f'/pulse/api/attachments/{obj.id}/thumbnail/?{urlencode({"token": token})}'


class AttachmentUploadSerializer(serializers.Serializer):
    """Serializer for file upload."""
    file = serializers.FileField()
    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    source = serializers.ChoiceField(
        choices=Attachment.SOURCE_CHOICES,
        default=Attachment.SOURCE_DIRECT,
        required=False,
    )


# =============================================================================
# Item Activity Serializers
# =============================================================================

class ItemActivitySerializer(serializers.ModelSerializer):
    """Serializer for ItemActivity model."""
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = ItemActivity
        fields = [
            'id', 'item', 'action', 'action_display',
            'field_name', 'old_value', 'new_value',
            'user_id', 'user_email', 'user_name', 'created_at'
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        if obj.user_id:
            info = user_service.get_user_by_id(obj.user_id)
            if info:
                first = info.get('first_name', '').strip()
                last = info.get('last_name', '').strip()
                full = f"{first} {last}".strip()
                if full:
                    return full
        if obj.user_email:
            return obj.user_email.split('@')[0]
        return None


# =============================================================================
# Item Link Serializers
# =============================================================================

class ItemLinkSerializer(serializers.ModelSerializer):
    """Serializer for ItemLink model."""
    link_type_display = serializers.CharField(source='get_link_type_display', read_only=True)
    source_item_key = serializers.CharField(source='source_item.item_key', read_only=True)
    source_item_title = serializers.CharField(source='source_item.title', read_only=True)
    source_item_status = serializers.CharField(source='source_item.status', read_only=True)
    target_item_key = serializers.CharField(source='target_item.item_key', read_only=True)
    target_item_title = serializers.CharField(source='target_item.title', read_only=True)
    target_item_status = serializers.CharField(source='target_item.status', read_only=True)

    class Meta:
        model = ItemLink
        fields = [
            'id', 'source_item', 'target_item', 'link_type', 'link_type_display',
            'source_item_key', 'source_item_title', 'source_item_status',
            'target_item_key', 'target_item_title', 'target_item_status',
            'created_by_id', 'created_by_email', 'created_at'
        ]
        read_only_fields = [
            'id', 'created_by_id', 'created_by_email', 'created_at',
            'source_item_key', 'source_item_title', 'source_item_status',
            'target_item_key', 'target_item_title', 'target_item_status'
        ]


class ItemLinkCreateSerializer(serializers.Serializer):
    """Serializer for creating item links."""
    target_item_id = serializers.IntegerField(required=False)
    target_item_key = serializers.CharField(required=False, max_length=50)
    link_type = serializers.ChoiceField(choices=ItemLink.LINK_TYPES)

    def validate(self, data):
        if not data.get('target_item_id') and not data.get('target_item_key'):
            raise serializers.ValidationError(
                "Either target_item_id or target_item_key is required"
            )
        return data

    def validate_target_item_key(self, value):
        if value:
            try:
                Item.objects.get(item_key__iexact=value)
            except Item.DoesNotExist:
                raise serializers.ValidationError(f"Item with key '{value}' not found")
        return value


# =============================================================================
# Item Watcher Serializers
# =============================================================================

class ItemWatcherSerializer(serializers.ModelSerializer):
    """Serializer for ItemWatcher model."""
    class Meta:
        model = ItemWatcher
        fields = [
            'id', 'item', 'user_id', 'user_email', 'user_name', 'created_at'
        ]
        read_only_fields = ['id', 'user_id', 'user_email', 'user_name', 'created_at']


# =============================================================================
# Comment Reaction Serializers
# =============================================================================

class CommentReactionSerializer(serializers.ModelSerializer):
    """Serializer for CommentReaction model."""
    reaction_display = serializers.CharField(source='get_reaction_display', read_only=True)

    class Meta:
        model = CommentReaction
        fields = [
            'id', 'comment', 'user_id', 'user_email', 'reaction',
            'reaction_display', 'created_at'
        ]
        read_only_fields = ['id', 'user_id', 'user_email', 'created_at']


class CommentReactionCreateSerializer(serializers.Serializer):
    """Serializer for adding reactions."""
    reaction = serializers.ChoiceField(choices=CommentReaction.REACTION_CHOICES)


class CommentWithReactionsSerializer(CommentSerializer):
    """Comment serializer that includes reaction counts."""
    reactions_summary = serializers.SerializerMethodField()

    class Meta(CommentSerializer.Meta):
        fields = CommentSerializer.Meta.fields + ['reactions_summary']

    def get_reactions_summary(self, obj):
        """Get reaction counts grouped by type."""
        from django.db.models import Count
        reactions = obj.reactions.values('reaction').annotate(count=Count('id'))
        return {r['reaction']: r['count'] for r in reactions}


# =============================================================================
# Component Serializers (Phase 3)
# =============================================================================

class ComponentSerializer(serializers.ModelSerializer):
    """Serializer for Component model."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Component
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'name', 'description',
            'lead_id', 'lead_email',
            'default_assignee_id', 'default_assignee_email',
            'created_at', 'updated_at',
            'item_count'
        ]
        read_only_fields = ['id', 'project_key', 'project_name', 'created_at', 'updated_at', 'item_count']

    def get_item_count(self, obj):
        return obj.items.count()


class ComponentCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating components."""

    class Meta:
        model = Component
        fields = [
            'project', 'name', 'description',
            'lead_id', 'lead_email',
            'default_assignee_id', 'default_assignee_email'
        ]

    def validate(self, data):
        project = data.get('project')
        name = data.get('name')
        if Component.objects.filter(project=project, name=name).exists():
            raise serializers.ValidationError({
                'name': f"Component '{name}' already exists in this project"
            })
        return data


# =============================================================================
# Version Serializers (Phase 3)
# =============================================================================

class VersionSerializer(serializers.ModelSerializer):
    """Serializer for Version model."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    item_count = serializers.SerializerMethodField()
    completed_item_count = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()

    class Meta:
        model = Version
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'name', 'description', 'status',
            'start_date', 'release_date', 'released_at',
            'sequence',
            'created_at', 'updated_at',
            'item_count', 'completed_item_count', 'progress_percent'
        ]
        read_only_fields = [
            'id', 'project_key', 'project_name', 'released_at',
            'created_at', 'updated_at',
            'item_count', 'completed_item_count', 'progress_percent'
        ]

    def get_item_count(self, obj):
        return obj.get_item_count()

    def get_completed_item_count(self, obj):
        return obj.get_completed_item_count()

    def get_progress_percent(self, obj):
        return obj.get_progress_percent()


class VersionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating versions."""

    class Meta:
        model = Version
        fields = [
            'project', 'name', 'description', 'status',
            'start_date', 'release_date', 'sequence'
        ]

    def validate(self, data):
        project = data.get('project')
        name = data.get('name')
        if Version.objects.filter(project=project, name=name).exists():
            raise serializers.ValidationError({
                'name': f"Version '{name}' already exists in this project"
            })
        return data


class VersionListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for version lists."""
    item_count = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()

    class Meta:
        model = Version
        fields = [
            'id', 'name', 'status', 'release_date',
            'item_count', 'progress_percent'
        ]

    def get_item_count(self, obj):
        return obj.get_item_count()

    def get_progress_percent(self, obj):
        return obj.get_progress_percent()


# =============================================================================
# WorkLog Serializers (Phase 3)
# =============================================================================

class WorkLogSerializer(serializers.ModelSerializer):
    """Serializer for WorkLog model."""
    item_key = serializers.CharField(source='item.item_key', read_only=True)
    item_title = serializers.CharField(source='item.title', read_only=True)
    duration_display = serializers.SerializerMethodField()

    class Meta:
        model = WorkLog
        fields = [
            'id', 'item', 'item_key', 'item_title',
            'user_id', 'user_email',
            'duration_minutes', 'duration_display',
            'worked_date', 'description',
            'remaining_estimate_minutes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'item_key', 'item_title', 'user_id', 'user_email', 'created_at', 'updated_at']

    def get_duration_display(self, obj):
        """Format duration as Xh Ym."""
        hours = obj.duration_minutes // 60
        minutes = obj.duration_minutes % 60
        if hours and minutes:
            return f"{hours}h {minutes}m"
        elif hours:
            return f"{hours}h"
        else:
            return f"{minutes}m"


class WorkLogCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating work logs."""
    item = ItemPKOrKeyRelatedField(queryset=Item.objects.all())
    # Allow specifying duration as hours (convenience)
    duration_hours = serializers.FloatField(required=False, write_only=True)

    class Meta:
        model = WorkLog
        fields = [
            'item', 'duration_minutes', 'duration_hours',
            'worked_date', 'description', 'remaining_estimate_minutes'
        ]

    def validate(self, data):
        # Reparenting a persisted log corrupts time accounting: WorkLog.save()
        # only adjusts the current item's counter and cannot safely transfer the
        # old duration between parents.  Keep the parent immutable; callers may
        # edit duration/date/description or delete + recreate on another item.
        if (self.instance is not None and 'item' in data
                and data['item'].pk != self.instance.item_id):
            raise serializers.ValidationError({
                'item': 'A work log cannot be moved to another item.'
            })
        # Convert duration_hours to duration_minutes if provided
        if 'duration_hours' in data and data['duration_hours']:
            data['duration_minutes'] = int(data['duration_hours'] * 60)
        if not data.get('duration_minutes'):
            raise serializers.ValidationError({
                'duration_minutes': 'Duration is required (either duration_minutes or duration_hours)'
            })
        data.pop('duration_hours', None)
        return data


# =============================================================================
# ProjectWorkflow Serializers (Phase 3)
# =============================================================================

class WorkflowTransitionSerializer(serializers.ModelSerializer):
    """Serializer for WorkflowTransition model."""

    class Meta:
        model = WorkflowTransition
        fields = [
            'id', 'workflow', 'name',
            'from_status', 'to_status',
            'conditions', 'required_fields', 'screen_fields',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ProjectWorkflowSerializer(serializers.ModelSerializer):
    """Serializer for ProjectWorkflow model."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)
    transitions = WorkflowTransitionSerializer(many=True, read_only=True)
    status_count = serializers.SerializerMethodField()

    class Meta:
        model = ProjectWorkflow
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'statuses', 'initial_status',
            'transitions', 'status_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project_key', 'project_name', 'transitions', 'status_count', 'created_at', 'updated_at']

    def get_status_count(self, obj):
        return len(obj.statuses)


class WorkflowStatusSerializer(serializers.Serializer):
    """Serializer for workflow status objects."""
    name = serializers.CharField(max_length=50)
    label = serializers.CharField(max_length=100)
    category = serializers.ChoiceField(choices=['todo', 'in_progress', 'done'])
    color = serializers.CharField(max_length=7, default='#6b7280')


# =============================================================================
# BoardColumn Serializers (Phase 3)
# =============================================================================

class BoardColumnSerializer(serializers.ModelSerializer):
    """Serializer for BoardColumn model."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    item_count = serializers.SerializerMethodField()
    is_over_wip_limit = serializers.SerializerMethodField()

    class Meta:
        model = BoardColumn
        fields = [
            'id', 'project', 'project_key',
            'name', 'status_mapping', 'position',
            'wip_limit', 'color', 'is_collapsed',
            'item_count', 'is_over_wip_limit',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'project_key', 'item_count', 'is_over_wip_limit', 'created_at', 'updated_at']

    def get_item_count(self, obj):
        return obj.get_item_count()

    def get_is_over_wip_limit(self, obj):
        return obj.is_over_wip_limit()


class BoardColumnCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating board columns."""

    class Meta:
        model = BoardColumn
        fields = [
            'project', 'name', 'status_mapping', 'position',
            'wip_limit', 'color', 'is_collapsed'
        ]


class BoardColumnReorderSerializer(serializers.Serializer):
    """Serializer for reordering board columns."""
    column_ids = serializers.ListField(
        child=serializers.UUIDField(),
        help_text="Ordered list of column IDs"
    )


# =============================================================================
# SavedFilter Serializers (Phase 3)
# =============================================================================

class SavedFilterSerializer(serializers.ModelSerializer):
    """Serializer for SavedFilter model."""
    project_key = serializers.CharField(source='project.key', read_only=True, allow_null=True)

    class Meta:
        model = SavedFilter
        fields = [
            'id', 'name', 'description', 'jql',
            'user_id', 'user_email',
            'project', 'project_key',
            'is_shared',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user_id', 'user_email', 'project_key', 'created_at', 'updated_at']


def _validate_saved_filter_jql(jql):
    """Validate that a saved-filter JQL string parses before persisting."""
    from .services.jql_parser import JQLParser, JQLSyntaxError
    if not jql or not jql.strip():
        raise serializers.ValidationError("JQL query cannot be empty.")
    try:
        parser = JQLParser()
        parser.parse(jql)
        # The parser is intentionally permissive for interactive filtering
        # (it stops at the first complete expression). Saved filters are
        # durable named queries, so require a *fully* consumed, balanced query:
        # - no dangling closing paren (`status = "x")` currently parses)
        # - no unterminated opening paren (`... AND (assignee = "x"` currently
        #   parses because the group is consumed as a complete expression)
        tokens = getattr(parser, "tokens", []) or []
        pos = getattr(parser, "pos", None)
        remaining = tokens[pos:] if pos is not None else []
        # Allow an ORDER BY suffix (parser consumes it, so remaining is empty;
        # this guard is for safety if the parser changes).
        paren_balance = 0
        for tok in tokens:
            if tok == '(':
                paren_balance += 1
            elif tok == ')':
                paren_balance -= 1
                if paren_balance < 0:
                    raise serializers.ValidationError(
                        "Malformed JQL: unmatched closing parenthesis."
                    )
        if paren_balance != 0:
            raise serializers.ValidationError(
                "Malformed JQL: unbalanced parentheses."
            )
        if remaining:
            raise serializers.ValidationError(
                f"Malformed JQL: unexpected trailing input near '{remaining[0]}'."
            )
    except JQLSyntaxError as e:
        raise serializers.ValidationError(f"Malformed JQL: {e}")
    except serializers.ValidationError:
        raise
    except Exception as e:
        raise serializers.ValidationError(f"Invalid JQL: {e}")


def _validate_saved_filter_name_unique(name, user_id, project_id, exclude_id=None):
    """Reject duplicate saved-filter names for the same owner + project scope."""
    qs = SavedFilter.objects.filter(
        name__iexact=name.strip(),
        user_id=user_id,
        project_id=project_id,
    )
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    if qs.exists():
        scope = "this project" if project_id else "global scope"
        raise serializers.ValidationError(
            f"A saved filter named '{name.strip()}' already exists in {scope}."
        )


class SavedFilterCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating saved filters."""

    class Meta:
        model = SavedFilter
        fields = ['name', 'description', 'jql', 'project', 'is_shared']

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Filter name cannot be empty.")
        return value.strip()

    def validate_jql(self, value):
        _validate_saved_filter_jql(value)
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "id", None) is not None:
            project = attrs.get("project")
            _validate_saved_filter_name_unique(
                attrs.get("name", ""),
                user.id,
                project.id if project else None,
            )
        return attrs


class SavedFilterUpdateSerializer(SavedFilterSerializer):
    """Serializer for updating a saved filter (partial edits)."""

    def validate_name(self, value):
        if value is not None and not value.strip():
            raise serializers.ValidationError("Filter name cannot be empty.")
        return value.strip() if value is not None else value

    def validate_jql(self, value):
        if value is not None:
            _validate_saved_filter_jql(value)
        return value

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        if instance is not None:
            name = attrs.get("name", instance.name)
            project = attrs.get("project", instance.project)
            _validate_saved_filter_name_unique(
                name or "",
                instance.user_id,
                project.id if project else instance.project_id,
                exclude_id=instance.pk,
            )
        return attrs


# =============================================================================
# Notification Serializers
# =============================================================================

class NotificationSerializer(serializers.ModelSerializer):
    """Serializer for in-app notification log entries."""
    item_title = serializers.CharField(source='item.title', read_only=True, default=None)
    item_key = serializers.CharField(source='item.item_key', read_only=True, default=None)

    class Meta:
        from .models import Notification
        model = Notification
        fields = [
            'id', 'user_id', 'item', 'item_title', 'item_key',
            'notification_type', 'title', 'message',
            'is_read', 'read_at', 'created_at',
        ]
        read_only_fields = [
            'id', 'user_id', 'item', 'notification_type',
            'title', 'message', 'created_at',
        ]


# =============================================================================
# Workflow Serializers
# =============================================================================

class StatusSerializer(serializers.ModelSerializer):
    """Global status definition."""

    class Meta:
        model = Status
        fields = [
            'id', 'name', 'slug', 'description',
            'category', 'color', 'icon', 'position',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ResolutionSerializer(serializers.ModelSerializer):
    """Resolution type."""

    class Meta:
        model = Resolution
        fields = [
            'id', 'name', 'slug', 'description',
            'position', 'is_system',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class WorkflowStatusInlineSerializer(serializers.ModelSerializer):
    """Inline serializer for statuses within a workflow."""
    status_name = serializers.CharField(source='status.name', read_only=True)
    status_slug = serializers.CharField(source='status.slug', read_only=True)
    status_category = serializers.CharField(source='status.category', read_only=True)
    status_color = serializers.CharField(source='status.color', read_only=True)

    class Meta:
        model = WorkflowStatus
        fields = [
            'id', 'status', 'status_name', 'status_slug',
            'status_category', 'status_color', 'position', 'owner_action_policy',
        ]
        read_only_fields = ['id']


class WorkflowTransitionDetailSerializer(serializers.ModelSerializer):
    """Full transition serializer with conditions, validators, and post-functions."""
    from_status_label = serializers.SerializerMethodField()
    to_status_label = serializers.SerializerMethodField()

    def validate_knowledge_articles(self, value):
        try:
            return normalize_knowledge_articles(value, reject_malformed=True)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def _validate_dict_list_field(self, field_name, value):
        try:
            return normalize_json_list_field(value, field_name, item_type="dict", reject_malformed=True)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def validate_validators(self, value):
        return self._validate_dict_list_field('validators', value)

    def validate_post_functions(self, value):
        return self._validate_dict_list_field('post_functions', value)

    def validate_screen_fields(self, value):
        return self._validate_dict_list_field('screen_fields', value)

    def validate_allowed_roles(self, value):
        try:
            return normalize_json_list_field(value, 'allowed_roles', item_type="str", reject_malformed=True)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['knowledge_articles'] = normalize_knowledge_articles(
            getattr(instance, 'knowledge_articles', None),
            reject_malformed=False,
        )
        data['validators'] = normalize_json_list_field(
            getattr(instance, 'validators', None), 'validators', item_type="dict", reject_malformed=False
        )
        data['post_functions'] = normalize_json_list_field(
            getattr(instance, 'post_functions', None), 'post_functions', item_type="dict", reject_malformed=False
        )
        data['screen_fields'] = normalize_json_list_field(
            getattr(instance, 'screen_fields', None), 'screen_fields', item_type="dict", reject_malformed=False
        )
        data['allowed_roles'] = normalize_json_list_field(
            getattr(instance, 'allowed_roles', None), 'allowed_roles', item_type="str", reject_malformed=False
        )
        return data

    class Meta:
        model = WorkflowTransition
        fields = [
            'id', 'workflow', 'name', 'description',
            'from_status', 'from_status_label',
            'to_status', 'to_status_label',
            'conditions', 'allowed_roles', 'assignee_only', 'reporter_only',
            'validators', 'required_fields',
            'post_functions', 'screen_fields', 'knowledge_articles',
            'position', 'button_style', 'is_global',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_from_status_label(self, obj):
        if obj.from_status == '*':
            return 'Any Status'
        try:
            return Status.objects.get(slug=obj.from_status).name
        except Status.DoesNotExist:
            return obj.from_status

    def get_to_status_label(self, obj):
        try:
            return Status.objects.get(slug=obj.to_status).name
        except Status.DoesNotExist:
            return obj.to_status

    # Reject display-name shapes like 'Resolved [done]' at ingest so the
    # 'Resolved [done]' data-corruption incident (AT-60, Channel Message
    # Review workflow) can't recur. Callers must send canonical slugs.
    def _validate_status_slug(self, value, allow_wildcard=False):
        if value is None:
            return value
        if allow_wildcard and value == '*':
            return value
        if not isinstance(value, str) or not value:
            raise serializers.ValidationError('must be a non-empty string slug')
        if ' ' in value or '[' in value or ']' in value or value != value.lower():
            raise serializers.ValidationError(
                f'{value!r} is not a canonical status slug — pass the slug '
                f'(e.g. "resolved"), not the display string '
                f'(e.g. "Resolved [done]").'
            )
        if not Status.objects.filter(slug=value).exists():
            raise serializers.ValidationError(
                f'unknown status slug {value!r}'
            )
        return value

    def validate_from_status(self, value):
        return self._validate_status_slug(value, allow_wildcard=True)

    def validate_to_status(self, value):
        return self._validate_status_slug(value, allow_wildcard=False)

    def validate(self, data):
        """Reject review self-loop transitions — PLAT-417/PLAT-419 structural guard.

        A "Submit for * Review" transition may never assign back to the same team
        that the workflow's bounce-back transitions (Request Changes / Return for
        Changes / Failed Verification) route to.  That team is the implementer team;
        routing the review to them creates the same-team self-loop removed in PLAT-417.
        """
        name = data.get('name', '')
        to_status = data.get('to_status', '')
        post_functions = data.get('post_functions') or []
        workflow = data.get('workflow')

        # Persist canonical Team keys in transition configuration, not merely
        # at execution time.  Legacy aliases otherwise survive indefinitely
        # and can disagree with database membership guards or future writers.
        if 'post_functions' in data:
            from .services.workflow_service import WorkflowService
            normalized_post_functions = []
            for post_function in post_functions:
                if not isinstance(post_function, dict):
                    normalized_post_functions.append(post_function)
                    continue
                post_function = dict(post_function)
                if post_function.get('type') == 'assign_team':
                    key = (
                        'value' if post_function.get('value') is not None
                        else 'team' if post_function.get('team') is not None
                        else None
                    )
                    if key:
                        post_function[key] = WorkflowService._canonical_team_name(
                            post_function[key]
                        )
                normalized_post_functions.append(post_function)
            post_functions = normalized_post_functions
            data['post_functions'] = normalized_post_functions

        effective_workflow = workflow or (
            getattr(self.instance, 'workflow', None) if self.instance else None
        )
        contract_fields = (
            'name', 'description', 'from_status', 'to_status', 'conditions',
            'allowed_roles', 'assignee_only', 'reporter_only', 'validators',
            'required_fields', 'post_functions', 'screen_fields',
            'knowledge_articles', 'button_style', 'is_global',
        )
        candidate = {
            field: (
                data[field] if field in data
                else getattr(self.instance, field, None) if self.instance
                else None
            )
            for field in contract_fields
        }
        _validate_source_authority_candidate(
            resource={
                'workflow_id': str(getattr(effective_workflow, 'id', '') or ''),
                'workflow_slug': getattr(effective_workflow, 'slug', '') or '',
                'transition_id': str(getattr(self.instance, 'id', '') or ''),
            },
            candidate=candidate,
        )

        is_review_submit = (
            (name.startswith('Submit for') and 'Review' in name)
            or (
                to_status == 'in_review'
                and any(pf.get('type') == 'stamp_review_started' for pf in post_functions)
            )
        )

        if not (is_review_submit and workflow):
            return data

        review_team = None
        for pf in post_functions:
            if pf.get('type') == 'assign_team':
                review_team = pf.get('value') or pf.get('team')
                break

        if not review_team:
            return data

        qs = WorkflowTransition.objects.filter(workflow=workflow)
        if self.instance:
            qs = qs.exclude(id=self.instance.id)

        bounce_keywords = ('request changes', 'return for changes', 'failed verification')
        for t in qs:
            if any(kw in t.name.lower() for kw in bounce_keywords):
                for pf in (t.post_functions or []):
                    if pf.get('type') == 'assign_team':
                        impl_team = pf.get('value') or pf.get('team')
                        if impl_team and impl_team == review_team:
                            raise serializers.ValidationError(
                                f'Review self-loop rejected: "{name}" assigns to '
                                f'"{review_team}", but "{t.name}" bounces back to '
                                f'the same team. A review step must leave the '
                                f'authoring team — assign it to a different team '
                                f'(e.g. "review", "architecture").'
                            )

        return data


class WorkflowDetailSerializer(serializers.ModelSerializer):
    """Full workflow with statuses and transitions."""
    statuses = WorkflowStatusInlineSerializer(source='workflow_statuses', many=True, read_only=True)
    transitions = WorkflowTransitionDetailSerializer(many=True, read_only=True)
    initial_status_name = serializers.CharField(source='initial_status.name', read_only=True)
    initial_status_slug = serializers.CharField(source='initial_status.slug', read_only=True)

    def validate_knowledge_base(self, value):
        try:
            return normalize_knowledge_articles(value, reject_malformed=True)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

    def validate(self, data):
        contract_fields = (
            'name', 'slug', 'description', 'is_active', 'is_system',
            'knowledge_base',
        )
        candidate = {
            field: (
                data[field] if field in data
                else getattr(self.instance, field, None) if self.instance
                else None
            )
            for field in contract_fields
        }
        _validate_source_authority_candidate(
            resource={
                'workflow_id': str(getattr(self.instance, 'id', '') or ''),
                'workflow_slug': (
                    data.get('slug')
                    or getattr(self.instance, 'slug', '')
                    or ''
                ),
            },
            candidate=candidate,
        )
        return data

    class Meta:
        model = Workflow
        fields = [
            'id', 'name', 'slug', 'description',
            'initial_status', 'initial_status_name', 'initial_status_slug',
            'is_active', 'is_system',
            'statuses', 'transitions', 'knowledge_base',
            'created_by_id', 'created_by_email',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class WorkflowListSerializer(serializers.ModelSerializer):
    """Compact workflow for list views."""
    initial_status_name = serializers.CharField(source='initial_status.name', read_only=True)
    status_count = serializers.SerializerMethodField()
    transition_count = serializers.SerializerMethodField()

    class Meta:
        model = Workflow
        fields = [
            'id', 'name', 'slug', 'description',
            'initial_status', 'initial_status_name',
            'is_active', 'is_system',
            'status_count', 'transition_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_status_count(self, obj):
        return obj.workflow_statuses.count()

    def get_transition_count(self, obj):
        return obj.transitions.count()


class WorkflowCreateSerializer(serializers.ModelSerializer):
    """Create a new workflow."""
    status_slugs = serializers.ListField(
        child=serializers.SlugField(),
        write_only=True,
        help_text="List of status slugs to include in this workflow"
    )

    class Meta:
        model = Workflow
        fields = [
            'name', 'slug', 'description',
            'initial_status', 'is_active',
            'status_slugs',
        ]

    def validate(self, data):
        _validate_source_authority_candidate(
            resource={
                'workflow_id': '',
                'workflow_slug': data.get('slug', ''),
            },
            candidate={
                'name': data.get('name'),
                'slug': data.get('slug'),
                'description': data.get('description'),
                'is_active': data.get('is_active'),
                'status_slugs': data.get('status_slugs', []),
                'knowledge_base': [],
            },
        )
        return data

    def create(self, validated_data):
        status_slugs = validated_data.pop('status_slugs', [])
        workflow = Workflow.objects.create(**validated_data)

        # PLS-714: a newly created workflow must inherit the escalation lanes so
        # it never repeats the seeding-order gap (feature-request-rfc missed the
        # 0044/0053/0069 sweeps because it was created after them).
        from tasks.services.workflow_service import WorkflowService
        WorkflowService.ensure_escalation_lanes(workflow)

        # Add statuses
        for i, slug in enumerate(status_slugs):
            try:
                status = Status.objects.get(slug=slug)
                WorkflowStatus.objects.create(
                    workflow=workflow,
                    status=status,
                    position=i,
                )
            except Status.DoesNotExist:
                pass

        return workflow


class WorkflowSchemeMappingSerializer(serializers.ModelSerializer):
    """Mapping within a workflow scheme."""
    workflow_name = serializers.CharField(source='workflow.name', read_only=True)
    workflow_slug = serializers.CharField(source='workflow.slug', read_only=True)

    class Meta:
        model = WorkflowSchemeMapping
        fields = [
            'id', 'scheme', 'item_type',
            'workflow', 'workflow_name', 'workflow_slug',
        ]
        read_only_fields = ['id']


class WorkflowSchemeDetailSerializer(serializers.ModelSerializer):
    """Full workflow scheme with mappings."""
    default_workflow_name = serializers.CharField(source='default_workflow.name', read_only=True)
    default_workflow_slug = serializers.CharField(source='default_workflow.slug', read_only=True)
    mappings = WorkflowSchemeMappingSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'default_workflow', 'default_workflow_name', 'default_workflow_slug',
            'mappings',
            'is_active', 'is_system',
            'created_by_id', 'created_by_email',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class WorkflowSchemeListSerializer(serializers.ModelSerializer):
    """Compact scheme for list views."""
    default_workflow_name = serializers.CharField(source='default_workflow.name', read_only=True)
    mapping_count = serializers.SerializerMethodField()
    project_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'default_workflow', 'default_workflow_name',
            'mapping_count', 'project_count',
            'is_active', 'is_system',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_mapping_count(self, obj):
        return obj.mappings.count()

    def get_project_count(self, obj):
        return obj.projects.count()


class TransitionExecuteSerializer(serializers.Serializer):
    """Serializer for executing a transition (via API)."""
    to_status = serializers.SlugField(
        required=False,
        allow_null=True,
        help_text="Target status slug. Optional when transition_id is given. "
                  "When multiple transitions from the current status share this "
                  "target, transition_id is required to disambiguate."
    )
    transition_id = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Exact WorkflowTransition id (from available-transitions). "
                  "Disambiguates when several transitions share a to_status."
    )
    resolution = serializers.SlugField(
        required=False,
        allow_null=True,
        help_text="Resolution slug (for transitions to done statuses)"
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional comment to add during transition"
    )
    screen_data = serializers.DictField(
        required=False,
        help_text="Field values from the transition screen"
    )

    def validate(self, attrs):
        if not attrs.get('to_status') and not attrs.get('transition_id'):
            raise serializers.ValidationError(
                "Provide either 'to_status' or 'transition_id'."
            )
        return attrs


# =============================================================================
# Issue Type, Screen, Permission, SLA Serializers
# =============================================================================

# --- Issue Type ---

class IssueTypeSerializer(serializers.ModelSerializer):
    """Global issue type definition."""

    class Meta:
        model = IssueType
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class IssueTypeSchemeMappingSerializer(serializers.ModelSerializer):
    """Through-table mapping: scheme ↔ issue type."""
    issue_type_detail = IssueTypeSerializer(source='issue_type', read_only=True)

    class Meta:
        model = IssueTypeSchemeMapping
        fields = ['id', 'scheme', 'issue_type', 'issue_type_detail', 'position']
        read_only_fields = ['id']


class IssueTypeSchemeSerializer(serializers.ModelSerializer):
    """Issue type scheme with nested mappings and issue types."""
    mappings = IssueTypeSchemeMappingSerializer(many=True, read_only=True)
    issue_types = IssueTypeSerializer(many=True, read_only=True)
    default_issue_type_name = serializers.CharField(
        source='default_issue_type.name', read_only=True, default=None
    )

    class Meta:
        model = IssueTypeScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'issue_types', 'default_issue_type', 'default_issue_type_name',
            'mappings',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Custom Fields ---

class CustomFieldSerializer(serializers.ModelSerializer):
    """Custom field definition."""

    class Meta:
        model = CustomField
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class CustomFieldContextSerializer(serializers.ModelSerializer):
    """Custom field context — scopes a field to projects/issue types."""
    projects = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Project.objects.all(), required=False
    )
    issue_types = serializers.PrimaryKeyRelatedField(
        many=True, queryset=IssueType.objects.all(), required=False
    )
    custom_field_name = serializers.CharField(
        source='custom_field.name', read_only=True
    )

    class Meta:
        model = CustomFieldContext
        fields = [
            'id', 'custom_field', 'custom_field_name', 'name',
            'projects', 'issue_types',
            'visibility', 'options_override',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CustomFieldValueSerializer(serializers.ModelSerializer):
    """Per-item custom field value."""
    custom_field_name = serializers.CharField(
        source='custom_field.name', read_only=True
    )
    custom_field_type = serializers.CharField(
        source='custom_field.field_type', read_only=True
    )

    class Meta:
        model = CustomFieldValue
        fields = [
            'id', 'item', 'custom_field',
            'custom_field_name', 'custom_field_type',
            'value', 'value_text',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Screens ---

class ScreenFieldSerializer(serializers.ModelSerializer):
    """A field on a screen."""

    class Meta:
        model = ScreenField
        fields = '__all__'
        read_only_fields = ['id']


class ScreenSerializer(serializers.ModelSerializer):
    """Screen with nested field list."""
    fields_list = ScreenFieldSerializer(source='fields', many=True, read_only=True)

    class Meta:
        model = Screen
        fields = [
            'id', 'name', 'slug', 'description', 'tabs',
            'fields_list',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ScreenSchemeSerializer(serializers.ModelSerializer):
    """Screen scheme — maps operations to screens."""
    create_screen_name = serializers.CharField(
        source='create_screen.name', read_only=True, default=None
    )
    edit_screen_name = serializers.CharField(
        source='edit_screen.name', read_only=True, default=None
    )
    view_screen_name = serializers.CharField(
        source='view_screen.name', read_only=True, default=None
    )

    class Meta:
        model = ScreenScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'create_screen', 'create_screen_name',
            'edit_screen', 'edit_screen_name',
            'view_screen', 'view_screen_name',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Issue Type Screen Scheme ---

class IssueTypeScreenSchemeMappingSerializer(serializers.ModelSerializer):
    """Maps an issue type to a screen scheme within an ITSS."""
    issue_type_name = serializers.CharField(
        source='issue_type.name', read_only=True
    )
    screen_scheme_name = serializers.CharField(
        source='screen_scheme.name', read_only=True
    )

    class Meta:
        model = IssueTypeScreenSchemeMapping
        fields = [
            'id', 'scheme', 'issue_type', 'issue_type_name',
            'screen_scheme', 'screen_scheme_name',
        ]
        read_only_fields = ['id']


class IssueTypeScreenSchemeSerializer(serializers.ModelSerializer):
    """Issue type screen scheme with nested mappings."""
    mappings = IssueTypeScreenSchemeMappingSerializer(many=True, read_only=True)
    default_screen_scheme_name = serializers.CharField(
        source='default_screen_scheme.name', read_only=True
    )

    class Meta:
        model = IssueTypeScreenScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'default_screen_scheme', 'default_screen_scheme_name',
            'mappings',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Security Scheme ---

class SecurityLevelSerializer(serializers.ModelSerializer):
    """Security level within a scheme."""

    class Meta:
        model = SecurityLevel
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class SecuritySchemeSerializer(serializers.ModelSerializer):
    """Security scheme with nested levels."""
    levels = SecurityLevelSerializer(many=True, read_only=True)
    default_level_name = serializers.CharField(
        source='default_level.name', read_only=True, default=None
    )

    class Meta:
        model = SecurityScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'default_level', 'default_level_name',
            'levels',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Permission Scheme ---

class PermissionGrantSerializer(serializers.ModelSerializer):
    """A single permission grant."""
    permission_display = serializers.CharField(
        source='get_permission_display', read_only=True
    )
    holder_type_display = serializers.CharField(
        source='get_holder_type_display', read_only=True
    )

    class Meta:
        model = PermissionGrant
        fields = '__all__'
        read_only_fields = ['id']


class PermissionSchemeSerializer(serializers.ModelSerializer):
    """Permission scheme with nested grants."""
    grants = PermissionGrantSerializer(many=True, read_only=True)

    class Meta:
        model = PermissionScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'grants',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Notification Scheme ---

class NotificationSchemeEventSerializer(serializers.ModelSerializer):
    """Per-event notification routing entry."""
    event_type_display = serializers.CharField(
        source='get_event_type_display', read_only=True
    )
    recipient_type_display = serializers.CharField(
        source='get_recipient_type_display', read_only=True
    )

    class Meta:
        model = NotificationSchemeEvent
        fields = '__all__'
        read_only_fields = ['id']


class NotificationSchemeSerializer(serializers.ModelSerializer):
    """Notification scheme with nested events."""
    events = NotificationSchemeEventSerializer(many=True, read_only=True)

    class Meta:
        model = NotificationScheme
        fields = [
            'id', 'name', 'slug', 'description',
            'events',
            'is_system', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- SLA ---

class SLADefinitionSerializer(serializers.ModelSerializer):
    """SLA definition with target times."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    calendar_name = serializers.CharField(
        source='calendar.name', read_only=True, default=None
    )

    class Meta:
        model = SLADefinition
        fields = [
            'id', 'project', 'project_key',
            'name', 'description', 'metric',
            'targets', 'calendar', 'calendar_name',
            'jql_filter', 'conditions',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class BusinessCalendarSerializer(serializers.ModelSerializer):
    """Business hours calendar."""

    class Meta:
        model = BusinessCalendar
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class SLAInstanceSerializer(serializers.ModelSerializer):
    """Per-item SLA tracking (read-only)."""
    item_key = serializers.CharField(source='item.item_key', read_only=True)
    sla_name = serializers.CharField(source='sla_definition.name', read_only=True)

    class Meta:
        model = SLAInstance
        fields = [
            'id', 'item', 'item_key',
            'sla_definition', 'sla_name',
            'target_minutes',
            'started_at', 'paused_at', 'completed_at',
            'elapsed_minutes', 'remaining_minutes',
            'sla_status', 'is_breached', 'breached_at',
            'warning_sent',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields


# --- Board ---

class BoardQuickFilterSerializer(serializers.ModelSerializer):
    """Quick filter for a board."""

    class Meta:
        model = BoardQuickFilter
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class BoardSerializer(serializers.ModelSerializer):
    """Board with nested quick filters."""
    quick_filters = BoardQuickFilterSerializer(many=True, read_only=True)
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = Board
        fields = [
            'id', 'project', 'project_key', 'project_name',
            'name', 'description', 'board_type',
            'filter_jql', 'swimlane_by', 'swimlane_jql',
            'card_layout', 'card_colors',
            'estimation_field', 'position', 'is_default',
            'quick_filters',
            'created_by_id', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'project_key', 'project_name', 'created_at', 'updated_at']


# --- Dashboard ---

class DashboardGadgetSerializer(serializers.ModelSerializer):
    """A gadget on a dashboard."""
    gadget_type_display = serializers.CharField(
        source='get_gadget_type_display', read_only=True
    )

    class Meta:
        model = DashboardGadget
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class DashboardSerializer(serializers.ModelSerializer):
    """Dashboard with nested gadgets."""
    gadgets = DashboardGadgetSerializer(many=True, read_only=True)

    class Meta:
        model = Dashboard
        fields = [
            'id', 'name', 'description',
            'owner_id', 'owner_email', 'visibility',
            'layout', 'is_default', 'is_wallboard', 'position',
            'gadgets',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# --- Automation ---

class AutomationRuleLogSerializer(serializers.ModelSerializer):
    """Automation execution log (read-only)."""
    rule_name = serializers.CharField(source='rule.name', read_only=True)
    item_key = serializers.CharField(source='item.item_key', read_only=True, default=None)

    class Meta:
        model = AutomationRuleLog
        fields = [
            'id', 'rule', 'rule_name',
            'item', 'item_key',
            'status', 'trigger_data', 'actions_executed',
            'error_message', 'duration_ms',
            'created_at',
        ]
        read_only_fields = fields


class AutomationRuleSerializer(serializers.ModelSerializer):
    """Automation rule with recent log count."""
    project_key = serializers.CharField(source='project.key', read_only=True, default=None)
    recent_log_count = serializers.SerializerMethodField()

    class Meta:
        model = AutomationRule
        fields = [
            'id', 'project', 'project_key',
            'name', 'description',
            'trigger_type', 'trigger_config',
            'conditions', 'actions',
            'is_enabled', 'execution_count',
            'last_executed_at', 'last_error',
            'max_executions_per_hour',
            'recent_log_count',
            'created_by_id', 'created_by_email',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'execution_count', 'last_executed_at', 'last_error',
            'recent_log_count', 'project_key',
            'created_at', 'updated_at',
        ]

    def get_recent_log_count(self, obj):
        """Count of log entries from the last 7 days."""
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=7)
        return obj.logs.filter(created_at__gte=cutoff).count()


# --- Project Configuration ---

class ProjectConfigurationSerializer(serializers.ModelSerializer):
    """Central configuration linking all schemes to a project."""
    project_key = serializers.CharField(source='project.key', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    # Nested for read
    issue_type_scheme_detail = IssueTypeSchemeSerializer(
        source='issue_type_scheme', read_only=True
    )
    issue_type_screen_scheme_detail = IssueTypeScreenSchemeSerializer(
        source='issue_type_screen_scheme', read_only=True
    )
    security_scheme_detail = SecuritySchemeSerializer(
        source='security_scheme', read_only=True
    )
    permission_scheme_detail = PermissionSchemeSerializer(
        source='permission_scheme', read_only=True
    )
    notification_scheme_detail = NotificationSchemeSerializer(
        source='notification_scheme', read_only=True
    )

    class Meta:
        model = ProjectConfiguration
        fields = [
            'project', 'project_key', 'project_name',
            # PK fields for write
            'issue_type_scheme', 'issue_type_screen_scheme',
            'security_scheme', 'permission_scheme', 'notification_scheme',
            # Nested detail for read
            'issue_type_scheme_detail', 'issue_type_screen_scheme_detail',
            'security_scheme_detail', 'permission_scheme_detail',
            'notification_scheme_detail',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'project_key', 'project_name',
            'issue_type_scheme_detail', 'issue_type_screen_scheme_detail',
            'security_scheme_detail', 'permission_scheme_detail',
            'notification_scheme_detail',
            'created_at', 'updated_at',
        ]


# ── AI Work Intake (PLS-306) ────────────────────────────────────────────────

class TaskDraftProposalSerializer(serializers.ModelSerializer):
    """Serializer for TaskDraftProposal model (AI work intake)."""

    class Meta:
        model = TaskDraftProposal
        fields = [
            'id', 'org_id', 'requester_id', 'requester_email', 'source',
            'raw_prompt',
            'proposed_project_id', 'proposed_workflow_slug', 'proposed_title',
            'proposed_description', 'proposed_priority', 'proposed_assignment_group',
            'proposed_labels', 'proposed_tags', 'proposed_parent_item_id',
            'duplicate_candidates',
            'validation_status', 'validation_errors',
            'prompt_hash', 'model_used', 'pipeline_version',
            'confirmed_item_id', 'confirmed_at', 'rejected_at', 'rejected_reason',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'duplicate_candidates',
            'validation_status', 'validation_errors',
            'prompt_hash', 'pipeline_version',
            'confirmed_item_id', 'confirmed_at', 'rejected_at', 'rejected_reason',
            'created_at', 'updated_at',
        ]


class TaskDraftCreateSerializer(serializers.Serializer):
    """Serializer for creating a new task draft proposal."""
    raw_prompt = serializers.CharField(help_text="Natural-language task description")
    proposed_project_id = serializers.IntegerField(required=True)
    proposed_workflow_slug = serializers.CharField(required=True)
    proposed_title = serializers.CharField(required=True, max_length=500)
    proposed_description = serializers.CharField(required=False, default='', allow_blank=True)
    proposed_priority = serializers.CharField(required=False, default='', allow_blank=True)
    proposed_assignment_group = serializers.CharField(required=False, default='', allow_blank=True)
    proposed_labels = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    proposed_tags = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    proposed_parent_item_id = serializers.CharField(required=False, default='', allow_blank=True)
    model_used = serializers.CharField(required=False, default='', allow_blank=True)
    source = serializers.CharField(required=False, default='api')


class TaskDraftConfirmSerializer(serializers.Serializer):
    """Serializer for confirming a task draft proposal."""
    pass


class TaskDraftRejectSerializer(serializers.Serializer):
    """Serializer for rejecting a task draft proposal."""
    reason = serializers.CharField(required=False, default='', allow_blank=True)
