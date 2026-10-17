from django.contrib.postgres.indexes import GinIndex
from django.db import connection, connections, models, router, transaction
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q
from django.utils import timezone
import json
import hashlib
import logging
import re
import uuid

logger = logging.getLogger(__name__)


class NaturalKeyManager(models.Manager):
    """Manager providing Django natural-key lookup for reference data.

    Django's default ``Manager`` does NOT implement ``get_by_natural_key``, so
    ``loaddata`` can only resolve natural-primary fixture entries when the
    model's default manager defines it (``build_instance`` /
    ``deserialize_fk_value`` in ``django.core.serializers.base`` gate on it).
    The reference-data models below carry stable unique ``slug`` fields, so a
    fixture dumped with ``--natural-primary --natural-foreign`` round-trips
    idempotently across databases (PLS-623): existing rows are matched and
    updated by slug instead of colliding on the UUID PK or the unique slug.
    """
    def get_by_natural_key(self, *natural_key):
        model = self.model
        fields = getattr(model, 'natural_key_fields', ())
        kwargs = {path: value for path, value in zip(fields, natural_key)}
        return self.get(**kwargs)


# =============================================================================
# Project Model (Organizations are managed by shizuha-admin)
# =============================================================================

class Project(models.Model):
    """
    Project within an organization. Items belong to projects.
    Project structure with project keys (e.g., PROJ-123).

    Note: Organizations are managed by shizuha-admin. This model only stores
    org_id as a reference. Use AdminService to fetch organization details.
    """
    PROJECT_TYPE_CHOICES = [
        ('software', 'Software Development'),
        ('business', 'Business Project'),
        ('service_management', 'Service Management'),
        ('operations', 'Operations'),
    ]

    ACCESS_LEVEL_CHOICES = [
        ('open', 'Open - All org members can access'),
        ('limited', 'Limited - Only project members'),
        ('private', 'Private - Invitation only'),
    ]

    # Organization reference (managed by shizuha-admin)
    org_id = models.IntegerField(
        db_index=True,
        help_text='Organization ID from shizuha-admin'
    )

    # Identity
    name = models.CharField(max_length=255)
    key = models.CharField(
        max_length=10,
        db_index=True,
        help_text="Project key for issue numbering (e.g., PROJ)"
    )
    description = models.TextField(blank=True)

    # Configuration
    project_type = models.CharField(
        max_length=30,
        choices=PROJECT_TYPE_CHOICES,
        default='software'
    )
    access_level = models.CharField(
        max_length=20,
        choices=ACCESS_LEVEL_CHOICES,
        default='limited'
    )

    # Leadership
    lead_id = models.IntegerField(null=True, blank=True, help_text="Project lead user ID")
    lead_email = models.EmailField(null=True, blank=True)
    default_assignee_id = models.IntegerField(null=True, blank=True)
    default_assignee_email = models.EmailField(null=True, blank=True)

    # Item numbering
    next_item_number = models.PositiveIntegerField(default=1)

    # Settings
    settings = models.JSONField(default=dict, blank=True, help_text="Project-specific settings")

    # Appearance
    icon = models.CharField(max_length=50, null=True, blank=True)
    color = models.CharField(max_length=7, default='#0052CC')  # Hex color

    # Status
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # HIVE-1223: the org's auto-provisioned canonical default project. A fresh
    # org with zero projects gets exactly one of these lazily on its first task
    # create, so every caller (Hive CTA, raw API, MCP agents) can place a first
    # task. Enforced one-per-org by the partial-unique constraint in Meta.
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by_id = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.key} - {self.name}"

    def get_next_item_key(self):
        """
        Atomically get and increment the next item number.
        Returns key like 'PROJ-123'.

        Collision-safe (PULSE-1): the minted number is max(counter, highest
        existing <KEY>-<n> + 1), so it can NEVER land at or below an existing
        key. `next_item_number` can desync below max(existing) when items are
        created off the normal serializer path (e.g. manage.py shell / bulk
        backfill) without advancing the counter; trusting the bare counter then
        minted a duplicate item_key → `tasks_task_item_key_key` IntegrityError
        (all new creates for that project 500'd). Deriving max-existing under the
        existing row lock self-heals that drift on the next create.
        """
        import re
        from django.db import transaction
        from django.db.models import Max, IntegerField
        from django.db.models.functions import Cast, Substr
        with transaction.atomic():
            project = Project.objects.select_for_update().get(pk=self.pk)
            prefix = f"{project.key}-"
            # Highest numeric suffix among this project's existing item_keys
            # (regex-anchored so only exact <KEY>-<digits> count).
            max_existing = (
                Item.objects
                .filter(item_key__regex=rf'^{re.escape(project.key)}-\d+$')
                .annotate(_num=Cast(Substr('item_key', len(prefix) + 1), IntegerField()))
                .aggregate(m=Max('_num'))['m']
            ) or 0
            next_n = max(project.next_item_number, max_existing + 1)
            item_key = f"{project.key}-{next_n}"
            project.next_item_number = next_n + 1
            project.save(update_fields=['next_item_number', 'updated_at'])
            return item_key

    def get_organization(self):
        """
        Get organization details from shizuha-admin.

        Returns:
            dict: Organization data or None if not found
        """
        from .services.admin_service import admin_service
        return admin_service.get_organization(self.org_id)

    class Meta:
        db_table = 'tasks_project'
        ordering = ['org_id', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['org_id', 'key'],
                name='uniq_project_org_key',
            ),
            # HIVE-1223: at most ONE default project per org. This is the
            # concurrency backstop for the lazy get_or_create in the task-create
            # path — racing first-task POSTs converge to a single default (the
            # loser's INSERT hits this constraint and re-reads the winner).
            models.UniqueConstraint(
                fields=['org_id'],
                condition=models.Q(is_default=True),
                name='uniq_default_project_per_org',
            ),
        ]
        indexes = [
            models.Index(fields=['org_id', 'is_active']),
            models.Index(fields=['key']),
            models.Index(fields=['lead_id']),
        ]


# =============================================================================
# Objectives / OKR alignment (PLS-272)
# =============================================================================

class Objective(models.Model):
    """Company/team objective that Pulse work can roll up to.

    Mirrors the common OKR/initiative shape: an objective is owned, time-bound,
    and can align both projects and individual items. Progress is computed from
    measurable key results when present, otherwise from linked work completion.
    """

    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('active', 'Active'),
        ('at_risk', 'At Risk'),
        ('done', 'Done'),
        ('archived', 'Archived'),
    ]

    org_id = models.IntegerField(db_index=True, help_text='Organization ID from shizuha-admin')
    project = models.ForeignKey(
        'Project', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='objectives', help_text='Optional primary project context for this objective.'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    owner_id = models.IntegerField(null=True, blank=True, help_text='Objective owner user ID from auth service')
    owner_email = models.EmailField(null=True, blank=True)
    time_period = models.CharField(max_length=100, blank=True, help_text='Human label, e.g. 2026-Q3')
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planned', db_index=True)
    projects = models.ManyToManyField('Project', blank=True, related_name='aligned_objectives')
    items = models.ManyToManyField('Item', blank=True, related_name='aligned_objectives')
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def progress_percent(self):
        key_results = list(self.key_results.all())
        if key_results:
            return round(sum(kr.progress_percent for kr in key_results) / len(key_results))
        linked_items = list(self.items.all())
        if linked_items:
            done = sum(1 for item in linked_items if item.completed_at or item.status in {'completed', 'done', 'resolved', 'closed'})
            return round((done / len(linked_items)) * 100)
        linked_projects = list(self.projects.all())
        if linked_projects:
            total = 0
            done = 0
            for project in linked_projects:
                qs = project.items.all()
                total += qs.count()
                done += qs.filter(Q(completed_at__isnull=False) | Q(status__in=['completed', 'done', 'resolved', 'closed'])).count()
            return round((done / total) * 100) if total else 0
        return 0

    def __str__(self):
        return self.title

    class Meta:
        db_table = 'tasks_objective'
        ordering = ['org_id', 'time_period', 'title']
        indexes = [
            models.Index(fields=['org_id', 'status']),
            models.Index(fields=['owner_email']),
            models.Index(fields=['time_period']),
        ]


class KeyResult(models.Model):
    """Measurable result for an Objective."""

    STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('on_track', 'On Track'),
        ('at_risk', 'At Risk'),
        ('done', 'Done'),
    ]

    objective = models.ForeignKey(Objective, on_delete=models.CASCADE, related_name='key_results')
    title = models.CharField(max_length=255)
    metric_name = models.CharField(max_length=100, blank=True)
    current_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_value = models.DecimalField(max_digits=14, decimal_places=2, default=1)
    unit = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_started', db_index=True)
    confidence = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='0-100 confidence score',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def progress_percent(self):
        try:
            target = float(self.target_value)
            if target <= 0:
                return 0
            progress = (float(self.current_value) / target) * 100
            return max(0, min(100, round(progress)))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0

    def __str__(self):
        return self.title

    class Meta:
        db_table = 'tasks_key_result'
        ordering = ['objective_id', 'id']
        indexes = [models.Index(fields=['objective', 'status'])]


class ProjectMembership(models.Model):
    """
    Membership linking users to projects with roles and permissions.
    """
    ROLE_CHOICES = [
        ('admin', 'Project Admin'),
        ('member', 'Member'),
        ('viewer', 'Viewer'),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    user_id = models.IntegerField(
        db_index=True,
        null=True,
        blank=True,
        help_text="User ID from auth service. NULL for email-only memberships (e.g., agents)."
    )
    user_email = models.EmailField(null=True, blank=True)

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')

    # Granular permissions (can override role defaults)
    can_browse = models.BooleanField(default=True)
    can_create = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=True)
    can_delete = models.BooleanField(default=False)
    can_manage_project = models.BooleanField(default=False)
    can_manage_members = models.BooleanField(default=False)

    # Status
    is_active = models.BooleanField(default=True)
    # ADR-0041: provenance makes assignment-created rows mechanically
    # distinguishable from independently reviewed project grants.  Legacy rows
    # remain unknown and therefore cannot authorize an armed routing lane.
    grant_source = models.CharField(max_length=32, default='legacy_unknown', db_index=True)
    grant_event_id = models.UUIDField(null=True, blank=True)
    grant_issuer_email = models.EmailField(blank=True, default='')
    authorization_version = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_email} in {self.project.key} ({self.role})"

    def save(self, *args, **kwargs):
        # Set default permissions based on role for new memberships
        if not self.pk:
            if self.role == 'admin':
                self.can_delete = True
                self.can_manage_project = True
                self.can_manage_members = True
            elif self.role == 'member':
                self.can_delete = False
                self.can_manage_project = False
                self.can_manage_members = False
            elif self.role == 'viewer':
                self.can_create = False
                self.can_edit = False
                self.can_delete = False
                self.can_manage_project = False
                self.can_manage_members = False
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'tasks_project_membership'
        unique_together = [['project', 'user_id']]
        indexes = [
            models.Index(fields=['user_id', 'is_active']),
            models.Index(fields=['project', 'role']),
        ]


# =============================================================================
# Agent × Project Affinity Membership (EVOL-20 Ph0a — HLD §2, page ab8fc292 v2)
# =============================================================================
class AgentProjectMembership(models.Model):
    """
    Agent×project affinity membership (EVOL-20 Ph0a, HLD §2).

    Records which projects an agent predominantly works, so the org moves
    MEMBERSHIPS (slow, deliberate) rather than TASKS (fast, thrashy). This model
    is INERT in Ph0 — nothing reads it; queue-ordering (Ph1) and resolver
    awareness (Ph2) consume it later. Every change is audit-logged via
    AgentProjectMembershipActivity — memberships are org decisions, never silent
    state.

    `agent_user` is the v1 (user_id, derived-email) convention, designed FK-ready
    for the PLAT-190 user-mirror: when that slice lands `agent_user_id` becomes a
    real FK with no schema fork. Deliberately mirrors ProjectMembership's
    (user_id, user_email) shape.
    """
    TIER_CHOICES = [
        ('primary', 'Primary'),
        ('secondary', 'Secondary'),
    ]

    # SIGNED cap (HLD §2/§11): <= 4 ACTIVE memberships per agent (cognitive-load
    # bound — the point of the design). Enforced in clean() + the service layer;
    # a DB constraint is intentionally left optional per the HLD.
    MAX_ACTIVE_PER_AGENT = 4

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='agent_memberships',
    )
    # v1 agent reference (FK-ready for the PLAT-190 mirror).
    agent_user_id = models.IntegerField(
        db_index=True,
        null=True,
        blank=True,
        help_text="Agent user ID from auth service. NULL for email-only (pre-mirror).",
    )
    agent_user_email = models.EmailField(
        help_text="Derived agent email (canonical-user convention; soft prereq PLAT-269).",
    )

    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default='primary')
    is_lead = models.BooleanField(
        default=False,
        help_text="Project stage-lead within their team (absorbs PLS-8 _PROJECT_STAGE_LEADS).",
    )
    active = models.BooleanField(default=True, db_index=True)

    # Provenance — memberships are org decisions, not silent state.
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    reason = models.TextField(blank=True, help_text="Why this membership was granted/changed.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional: secondaries may be time-boxed by the rebalancer.",
    )

    def __str__(self):
        suffix = '/lead' if self.is_lead else ''
        return f"{self.agent_user_email} @ {self.project.key} ({self.tier}{suffix})"

    def clean(self):
        from django.core.exceptions import ValidationError
        from django.db.models import Q
        # SIGNED cap: <= MAX_ACTIVE_PER_AGENT active memberships per agent (HLD §2).
        if self.active:
            # Count ALL of the agent's active rows, keyed by EITHER identifier: the
            # canonical agent email OR the numeric user_id (Codex P1). Counting only
            # one identifier let stage-lead rows (agent_user_id IS NULL, derived from
            # the PLS-8 map) bypass the cap when the agent already held MAX_ACTIVE
            # id-backed rows — the two disjoint counts never saw each other.
            q = Q(agent_user_email__iexact=self.agent_user_email)
            if self.agent_user_id is not None:
                q |= Q(agent_user_id=self.agent_user_id)
            siblings = AgentProjectMembership.objects.filter(active=True).filter(q)
            if self.pk:
                siblings = siblings.exclude(pk=self.pk)
            if siblings.count() >= self.MAX_ACTIVE_PER_AGENT:
                raise ValidationError(
                    f"Agent {self.agent_user_email} already holds "
                    f"{self.MAX_ACTIVE_PER_AGENT} active project memberships (cap)."
                )

    class Meta:
        db_table = 'tasks_agent_project_membership'
        unique_together = [['project', 'agent_user_id']]
        indexes = [
            models.Index(fields=['agent_user_id', 'active']),
            models.Index(fields=['agent_user_email', 'active']),
            models.Index(fields=['project', 'tier', 'active']),
        ]


class AgentProjectMembershipActivity(models.Model):
    """
    Audit log for AgentProjectMembership changes (EVOL-20 HLD §2), ItemActivity-style:
    who/when/why + old→new. One row per change; memberships are never silent state.
    """
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('activated', 'Activated'),
        ('deactivated', 'Deactivated'),
        ('expired', 'Expired'),
        ('tier_changed', 'Tier Changed'),
        ('lead_changed', 'Lead Flag Changed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        'AgentProjectMembership',
        on_delete=models.CASCADE,
        related_name='activities',
        help_text="The membership this audit row belongs to.",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    field_name = models.CharField(max_length=100, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)

    # Actor + rationale.
    actor_user_id = models.IntegerField(
        null=True, blank=True, db_index=True,
        help_text="Who made the change (NULL = system/rebalancer).",
    )
    actor_email = models.EmailField(blank=True)
    reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.action} on membership {self.membership_id} by {self.actor_email or 'system'}"

    class Meta:
        db_table = 'tasks_agent_project_membership_activity'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['membership', 'created_at']),
        ]


# =============================================================================
# Sprint Model (Agile Sprints)
# =============================================================================

class Sprint(models.Model):
    """
    Represents an Agile sprint within a project.
    Sprints are time-boxed iterations used to plan and track work.
    """
    STATUS_CHOICES = [
        ('planning', 'Planning'),
        ('active', 'Active'),
        ('completed', 'Completed'),
    ]

    # Terminal "done" slugs across all workflows (autonomous-dev uses 'done', others 'completed')
    _DONE_STATUSES = ['completed', 'done', 'resolved', 'merged', 'closed']

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='sprints'
    )
    name = models.CharField(max_length=255)
    goal = models.TextField(blank=True, help_text="Sprint goal/objective")

    # Timing
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planning')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Metrics (computed/cached)
    committed_points = models.IntegerField(default=0, help_text="Total story points at sprint start")
    completed_points = models.IntegerField(default=0, help_text="Story points completed")

    # Metadata
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key} - {self.name}"

    def start(self):
        """Start the sprint"""
        from django.utils import timezone
        if self.status != 'planning':
            raise ValueError("Can only start sprints in planning status")
        self.status = 'active'
        self.started_at = timezone.now()
        if not self.start_date:
            self.start_date = timezone.now().date()
        # Committed = all tasks in the sprint, regardless of current status.
        # Rolling sprints attach tasks already in awaiting_merge/done from prior
        # work, so restricting to pending/in_progress underestimates scope.
        self.committed_points = self.items.filter(
            mode='task'
        ).aggregate(
            total=models.Sum('story_points')
        )['total'] or 0
        if self.committed_points == 0:
            self.committed_points = self.items.filter(mode='task').count()
        self.save()

    def complete(self):
        """Complete the sprint"""
        from django.utils import timezone
        if self.status != 'active':
            raise ValueError("Can only complete active sprints")
        self.status = 'completed'
        self.completed_at = timezone.now()
        if not self.end_date:
            self.end_date = timezone.now().date()
        # Calculate completed points; count all "done" statuses; fall back to task count
        self.completed_points = self.items.filter(
            mode='task',
            status__in=self._DONE_STATUSES
        ).aggregate(
            total=models.Sum('story_points')
        )['total'] or 0
        if self.completed_points == 0:
            self.completed_points = self.items.filter(
                mode='task',
                status__in=self._DONE_STATUSES
            ).count()
        self.save()

    def get_velocity(self):
        """Get velocity (completed points) for this sprint, counting tasks when no story points."""
        if self.status == 'completed':
            if self.completed_points > 0:
                return self.completed_points
            return self.items.filter(mode='task', status__in=self._DONE_STATUSES).count()
        pts = self.items.filter(
            mode='task',
            status__in=self._DONE_STATUSES
        ).aggregate(
            total=models.Sum('story_points')
        )['total'] or 0
        if pts == 0:
            return self.items.filter(mode='task', status__in=self._DONE_STATUSES).count()
        return pts

    def get_burndown_data(self):
        """
        Get burndown chart data.
        Returns list of {date, ideal, actual} points.
        """
        if not self.start_date or not self.end_date:
            return []

        from datetime import timedelta
        from django.utils import timezone

        # Calculate ideal burndown; fall back to task count when story points absent
        total_points = self.committed_points or self.items.filter(
            mode='task'
        ).aggregate(total=models.Sum('story_points'))['total'] or 0

        use_count_based = False
        if total_points == 0:
            total_points = self.items.filter(mode='task').count()
            use_count_based = True

        if total_points == 0:
            return []

        # Get all completion events. autonomous-dev tasks enter 'done' without
        # stamping completed_at, so fall back to updated_at for those rows.
        if use_count_based:
            completed_items = [
                {
                    'done_at': item.completed_at or item.updated_at,
                    'story_points': 1,
                }
                for item in self.items.filter(
                    mode='task',
                    status__in=self._DONE_STATUSES,
                ).only('completed_at', 'updated_at')
            ]
        else:
            completed_items = [
                {
                    'done_at': item['completed_at'] or item['updated_at'],
                    'story_points': item['story_points'],
                }
                for item in self.items.filter(
                    mode='task',
                    status__in=self._DONE_STATUSES,
                ).values('completed_at', 'updated_at', 'story_points')
            ]

        # Build daily data
        data = []
        current_date = self.start_date
        end = self.end_date
        # Use the configured-timezone date (Asia/Kolkata), not the UTC date, so the
        # "actual" line reaches today's row. self.start_date/end_date are naive
        # dates in the project TZ; timezone.now().date() is UTC and lags by a day
        # during the UTC/local date-boundary window, truncating the actual series
        # one day early (today_row['actual']=None).
        today = timezone.localdate()

        total_days = (end - current_date).days
        if total_days <= 0:
            total_days = 1

        points_per_day = total_points / total_days

        while current_date <= end:
            ideal = total_points - (points_per_day * (current_date - self.start_date).days)
            ideal = max(0, ideal)

            # Calculate actual remaining for completed sprints or up to today
            if self.status == 'completed' or current_date <= today:
                completed_by_date = sum(
                    item['story_points'] or 0
                    for item in completed_items
                    if item['done_at'] and item['done_at'].date() <= current_date
                )
                actual = total_points - completed_by_date
            else:
                actual = None  # Future dates

            data.append({
                'date': current_date.isoformat(),
                'ideal': round(ideal, 1),
                'actual': round(actual, 1) if actual is not None else None
            })
            current_date += timedelta(days=1)

        return data

    class Meta:
        db_table = 'tasks_sprint'
        ordering = ['-start_date', '-created_at']
        indexes = [
            models.Index(fields=['project', 'status']),
            models.Index(fields=['start_date', 'end_date']),
        ]


# =============================================================================
# Item Model (Unified Task/Action/Alert)
# =============================================================================

class Item(models.Model):
    """
    Unified model for tasks, actions, and alerts.

    - Task mode: Personal items (reminders, calendar events, to-dos)
    - Action mode: System-generated items with approval workflows and callbacks
    - Alert mode: System alerts requiring acknowledgment, with deduplication and escalation
    """

    # === Choices ===
    MODE_CHOICES = [
        ('task', 'Task'),
        ('action', 'Action'),
        ('alert', 'Alert'),
    ]

    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    ]

    SEVERITY_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]

    # Task statuses
    TASK_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('blocked', 'Blocked'),
    ]

    # Action statuses
    ACTION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('executing', 'Executing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    ]

    # Alert statuses
    ALERT_STATUS_CHOICES = [
        ('firing', 'Firing'),
        ('acknowledged', 'Acknowledged'),
        ('resolved', 'Resolved'),
        ('silenced', 'Silenced'),
    ]

    # Combined status choices for the field
    STATUS_CHOICES = list(set(TASK_STATUS_CHOICES + ACTION_STATUS_CHOICES + ALERT_STATUS_CHOICES))

    FREQUENCY_CHOICES = [
        ('one_time', 'One Time'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
        ('custom', 'Custom'),
    ]

    # === Identity ===
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='task', db_index=True)
    item_type = models.CharField(
        max_length=100,
        default='task.generic',
        help_text="Hierarchical type e.g., task.reminder, action.hedging.short, alert.error"
    )

    # === Basic info ===
    title = models.CharField(max_length=400)
    description = models.TextField(blank=True)

    # === Source tracking ===
    source = models.CharField(max_length=100, default='manual', help_text="Origin: manual, google_calendar, arbiter-backend, etc.")
    source_id = models.CharField(max_length=255, null=True, blank=True, help_text="External reference ID")
    source_url = models.URLField(max_length=2048, null=True, blank=True, help_text="Link to source system")

    # === Project ===
    project = models.ForeignKey(
        'Project',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Project this item belongs to. Null = personal item."
    )
    item_key = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        db_index=True,
        unique=True,
        help_text="Project-scoped key like PROJ-123"
    )

    # === Assignment ===
    assignee_id = models.IntegerField(null=True, blank=True, help_text="User ID from auth service")
    assignee_email = models.EmailField(null=True, blank=True)
    assignment_group = models.CharField(max_length=100, null=True, blank=True, help_text="Team/group assigned to (ServiceNow-style)")
    workflow_name = models.CharField(max_length=100, null=True, blank=True, help_text="Workflow governing this task lifecycle")
    workflow_status = models.CharField(max_length=100, null=True, blank=True, help_text="Current status within the workflow")
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)

    # === Status ===
    status = models.CharField(max_length=50, default='pending')  # Workflow system validates statuses, not model choices
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, null=True, blank=True, help_text="For alerts")
    # PLS-467: row-local authority revision for state-bearing events.  It is
    # advanced once per committed assignment/status/priority/PR-binding change;
    # consumers compare exact equality so A→B→A replays cannot look current.
    state_generation = models.PositiveBigIntegerField(default=0)
    # PLS-733 / ADR-0064: generation scoped specifically to the external-wait
    # state machine.  It is independent from state_generation so a harmless
    # priority/comment-side state occurrence cannot authorize a stale provider
    # wake for a later wait cycle.
    external_wait_generation = models.PositiveBigIntegerField(default=0)

    # PLS-675 / PLAT-1351 v4: typed acceptance mode is the sole enforcement
    # authority for the deploy-verify close gate. Null means unresolved —
    # the gate derives a default (service-affecting/user-facing → live;
    # otherwise code). Free-form labels/comments are never authority.
    ACCEPTANCE_MODE_CODE = 'code'
    ACCEPTANCE_MODE_DEPLOYED = 'deployed'
    ACCEPTANCE_MODE_LIVE = 'live'
    ACCEPTANCE_MODE_CHOICES = [
        (ACCEPTANCE_MODE_CODE, 'Code'),
        (ACCEPTANCE_MODE_DEPLOYED, 'Deployed'),
        (ACCEPTANCE_MODE_LIVE, 'Live'),
    ]
    acceptance_mode = models.CharField(
        max_length=16,
        choices=ACCEPTANCE_MODE_CHOICES,
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "PLS-675/PLAT-1351 typed acceptance mode: code|deployed|live. "
            "Null = unresolved; gate derives default for service-affecting/"
            "user-facing work → live. Enforcement authority is this field, "
            "not labels or free-form comments."
        ),
    )

    # === Resolution ===
    resolution = models.ForeignKey(
        'Resolution',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='items',
        help_text="Resolution type (set when item moves to a 'done' status, cleared on reopen)"
    )
    resolution_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the resolution was set (separate from completed_at)"
    )

    # === Archival ===
    # Set by a periodic Celery sweep on Done-category items older than N days
    # (default 28, per Linear's convention). Archived items are excluded from
    # all default queries and only surfaced through the /archive view. See
    # tasks/tasks.py::sweep_archive_done_items.
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when the item has been auto-archived. Archived items are hidden from all default views."
    )
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the item was archived (set by the periodic sweep)."
    )

    # === Timing ===
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    due_date = models.DateTimeField(null=True, blank=True, help_text="Tasks: when due; Actions: expiration; Alerts: auto-resolve time")
    start_date = models.DateTimeField(null=True, blank=True)
    reminder_time = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # === Task-specific fields ===
    # Sprint assignment (Agile)
    sprint = models.ForeignKey(
        'Sprint',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='items',
        help_text="Sprint this item is assigned to"
    )
    story_points = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Story point estimate (Fibonacci: 1,2,3,5,8,13,21)"
    )
    percent_complete = models.PositiveIntegerField(
        default=0,
        help_text="Task completion percentage (0-100). Auto-set from workflow state, manually overridable."
    )

    # Recurrence
    is_recurring = models.BooleanField(default=False)
    frequency = models.CharField(max_length=15, choices=FREQUENCY_CHOICES, default='one_time')
    recurrence_pattern = models.TextField(null=True, blank=True, help_text="JSON recurrence rules")
    recurrence_end_date = models.DateTimeField(null=True, blank=True)
    parent_item = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='child_items')

    # === PLS-481 / PLS-634: accepted-capacity-held schema ===
    # Immutable lazy-plan identity for no-PR Architecture carriers.
    # Set atomically by the Accept — Capacity-Held transition; cleared on
    # terminal completion. plan_hash is the content-addressed plan identity;
    # plan_generation is a monotonic counter bumped on re-acceptance.
    plan_hash = models.CharField(
        max_length=64, null=True, blank=True,
        help_text="PLS-481: immutable content-addressed plan hash (set by Accept — Capacity-Held).",
    )
    plan_generation = models.PositiveIntegerField(
        default=0, null=True, blank=True,
        help_text="PLS-481: monotonic plan generation; bumped on re-acceptance.",
    )
    # Typed reason for capacity-held status (free-text, set by the accepting architect).
    capacity_reason = models.TextField(
        blank=True, default='', null=True,
        help_text="PLS-481: architect-supplied reason for holding capacity.",
    )
    # Reference to the immutable PLS-482 PublicationBinding that was current
    # at the time of acceptance. Null until Accept — Capacity-Held fires.
    publication_binding = models.ForeignKey(
        'PublicationBinding', null=True, blank=True, on_delete=models.PROTECT,
        related_name='accepted_items',
        help_text="PLS-481: the PublicationBinding at the time of acceptance.",
    )
    # Exact reviewed round/result from the PublicationAttempt that produced
    # the binding. Persisted alongside the binding reference for audit.
    publication_round = models.CharField(
        max_length=128, null=True, blank=True,
        help_text="PLS-481: review_round_id from the accepted PublicationAttempt.",
    )
    publication_result = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="PLS-481: publication_result from the accepted PublicationAttempt.",
    )

    # === PLS-533: epic close-out assessment materialization ===
    # `scope_generation` lives on the EPIC: it is bumped whenever the epic's
    # canonical decomposition scope changes (a new/reopened parent_of/child_of
    # or parent_item child), which legally supersedes any assessment minted for
    # an older generation. On a materialized assessment task it is unused (0).
    scope_generation = models.PositiveIntegerField(
        default=0, null=True, blank=True,
        help_text="PLS-533: the epic's current canonical-scope generation; bumped on scope change to supersede stale close-out assessments. Nullable so a historical-model insert (which omits this leaf column) does not trip a NOT NULL whose DB default Django strips post-backfill; code reads it as `scope_generation or 0`.",
    )
    # On a `task.epic_closeout_assessment` item, these record which epic + which
    # of its scope generations the assessment was minted for. The partial unique
    # constraint below makes (closeout_parent, closeout_generation) the dedup key
    # so retries and concurrent final-child events resolve to ONE assessment.
    closeout_parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.CASCADE,
        related_name='closeout_assessments',
        help_text="PLS-533: the epic this close-out assessment task assesses (assessment items only).",
    )
    closeout_generation = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="PLS-533: the epic scope_generation this assessment was minted for.",
    )

    # === PLS-518: pinned current-state summary (handoff aid) ===
    # Distinct from description: a short "where are we now / next executable
    # step / who-what-blocks" pin owned by the current assignee. Shown above
    # comments and in pulse_get_task output. It is a handoff aid, NOT
    # authoritative task state — the workflow/status is. Updates are
    # single-writer (current assignee or staff) and timestamped; a stale
    # assignee must not overwrite a newer pin (see perform_update gate).
    current_state = models.TextField(
        blank=True, default='', null=True,
        help_text="PLS-518: pinned 'current state / next step / blockers' handoff summary, editable by the assignee.",
    )
    current_state_updated_at = models.DateTimeField(
        null=True, blank=True,
        help_text="PLS-518: when the pinned current_state was last set by the assignee.",
    )

    # Effort tracking
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    actual_hours = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    # Time tracking (minutes for precision)
    original_estimate_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Original time estimate in minutes"
    )
    time_spent_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Total time spent in minutes (auto-calculated from work logs)"
    )
    remaining_estimate_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Remaining time estimate in minutes"
    )

    # Component assignment
    component = models.ForeignKey(
        'Component',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='items',
        help_text="Component this item belongs to"
    )

    # Version tracking
    fix_version = models.ForeignKey(
        'Version',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fix_items',
        help_text="Version where this item will be fixed"
    )
    affects_versions = models.ManyToManyField(
        'Version',
        blank=True,
        related_name='affected_items',
        help_text="Versions affected by this item"
    )

    # === Action-specific fields ===
    # Payload
    payload = models.JSONField(default=dict, blank=True, help_text="Action/alert-specific data")

    # Approval
    requires_approval = models.BooleanField(default=False)
    auto_approved = models.BooleanField(default=False)
    approved_by = models.CharField(max_length=255, null=True, blank=True, help_text="auto, user:xxx, mcp:claude")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_rule = models.ForeignKey('ApprovalRule', null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_items')
    rejection_reason = models.TextField(blank=True)

    # Execution (for actions with callbacks)
    callback_url = models.URLField(null=True, blank=True, help_text="Webhook to execute on approval")
    callback_method = models.CharField(max_length=10, default='POST')
    callback_headers = models.JSONField(default=dict, blank=True)
    execution_result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)

    # === Alert-specific fields ===
    # Deduplication
    fingerprint = models.CharField(max_length=255, null=True, blank=True, db_index=True, help_text="Computed hash for dedup")
    fingerprint_fields = models.JSONField(null=True, blank=True, help_text="Fields used to compute fingerprint")

    # Occurrence tracking (for deduplicated alerts)
    occurrence_count = models.IntegerField(default=1)
    first_fired_at = models.DateTimeField(null=True, blank=True)
    last_fired_at = models.DateTimeField(null=True, blank=True)

    # Resolution
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=255, null=True, blank=True, help_text="auto, user:xxx, recovery:item_id")
    resolution_note = models.TextField(blank=True)

    # Acknowledgment
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.CharField(max_length=255, null=True, blank=True)

    # Escalation tracking
    escalation_level = models.IntegerField(default=0, help_text="0 = not escalated, 1+ = escalation stages")
    last_escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_rule = models.ForeignKey('EscalationRule', null=True, blank=True, on_delete=models.SET_NULL, related_name='escalated_items')

    # Correlation (for grouping related alerts)
    correlation_id = models.CharField(max_length=255, null=True, blank=True, db_index=True, help_text="Group related alerts")
    parent_alert = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='child_alerts')

    # === Common fields ===
    # Notifications
    reminder_sent = models.BooleanField(default=False)
    overdue_notified = models.BooleanField(default=False, help_text="Set when overdue notification is sent; reset when due_date changes")
    reminder_count = models.IntegerField(default=0)
    last_reminder_sent = models.DateTimeField(null=True, blank=True)
    notification_preferences = models.JSONField(default=dict, blank=True)

    # Silencing (applies to ALL modes)
    silenced_until = models.DateTimeField(null=True, blank=True)
    silenced_by = models.CharField(max_length=255, null=True, blank=True)
    silence_reason = models.TextField(blank=True)
    silence_rule = models.ForeignKey('SilenceRule', null=True, blank=True, on_delete=models.SET_NULL, related_name='silenced_items')

    # Dual-shape labels (PLS-712): task producers commonly persist flat string
    # arrays while alert producers persist Prometheus-style key/value objects.
    # Consumers must use tasks.label_utils rather than assuming either shape.
    labels = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Dual shape: task labels are JSON string arrays such as '
            '["repo:shizuha-labs/drive"]; alert labels may be JSON objects '
            'such as {"host": "web-01", "service": "nginx"}.'
        ),
    )

    # Metadata
    tags = models.TextField(null=True, blank=True, help_text="Comma-separated tags")
    metadata = models.JSONField(default=dict, blank=True, help_text="Additional metadata")

    # === PLS-900 / ADR-0052: task-level conflict authority (Option C) ===
    # Exact-subject conflict exclusion: a list of email addresses (or user
    # identifiers) that must NEVER be resolved as this item's assignee, even if
    # they are the least-loaded available member. The resolver treats this as a
    # hard filter (like the originating-author exclusion), so a conflicted party
    # is never silently selected. An empty post-exclusion candidate set fails
    # loud as a visible routing HOLD (not a silent conflicted assignment) so the
    # item stays observable and recoverable instead of landing on a conflicted
    # owner. Callers (MCP create/update) normalize to lowercase email on write;
    # the resolver lowercases defensively at consume time. This is the schema the
    # PLS-611 criteria 3/6 validation exercises (ADR-0052 Option C).
    conflicted_parties = models.JSONField(
        default=list, blank=True, null=True,
        help_text='Emails/identifiers never selectable as this item\'s assignee '
                  '(ADR-0052 conflict authority; hard-excluded by the resolver)',
    )

    # External system integration (legacy compatibility)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    external_url = models.URLField(null=True, blank=True)
    sync_enabled = models.BooleanField(default=False)
    last_synced = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"[{self.mode}] {self.title} ({self.item_type})"

    # === Persistence ===
    @staticmethod
    def _project_organization_id(project_id, project=None):
        if project_id is None:
            return None
        if project is not None and getattr(project, 'pk', None) == project_id:
            return project.org_id
        return Project.objects.only('org_id').get(pk=project_id).org_id

    def _validate_parent_organization(self, field_name):
        parent_id = getattr(self, f'{field_name}_id')
        if not parent_id:
            return
        parent = self._state.fields_cache.get(field_name)
        parent_project_id = (
            parent.project_id
            if parent is not None
            else Item.objects.values_list('project_id', flat=True).get(pk=parent_id)
        )
        project = self._state.fields_cache.get('project')
        item_org_id = self._project_organization_id(self.project_id, project)
        parent_project = (
            parent._state.fields_cache.get('project')
            if parent is not None
            else None
        )
        parent_org_id = self._project_organization_id(
            parent_project_id,
            parent_project,
        )
        if item_org_id != parent_org_id:
            raise ValidationError({
                field_name: (
                    'Parent and child tasks must belong to the same organization.'
                )
            })

    def _validate_organization_ownership(self, update_fields=None):
        """Prevent an existing task and its history moving between tenants."""
        if not self.pk:
            return
        if update_fields is not None and not {
            'project', 'project_id',
        }.intersection(update_fields):
            return
        old_project_id = (
            Item.objects
            .filter(pk=self.pk)
            .values_list('project_id', flat=True)
            .first()
        )
        if old_project_id == self.project_id:
            return
        old_org_id = self._project_organization_id(old_project_id)
        project = self._state.fields_cache.get('project')
        new_org_id = self._project_organization_id(self.project_id, project)
        if old_org_id != new_org_id:
            raise ValidationError({
                'project': (
                    'A task cannot be moved to another organization. Create a '
                    'new task in that organization instead.'
                )
            })

    def save(self, *args, **kwargs):
        self._validate_organization_ownership(kwargs.get('update_fields'))
        # Organization isolation is structural. Project-less legacy items may
        # parent each other, and different projects in one organization may
        # share hierarchy, but an organization-owned task never shares either
        # task or alert ancestry with another org (or an unscoped item).
        self._validate_parent_organization('parent_item')
        self._validate_parent_organization('parent_alert')

        # Every task belongs to a workflow — the alternative (see AT-60)
        # strands the item with no transitions available because the API
        # has no workflow to look them up in. We resolve one at insert-time
        # using the project's scheme (Jira-style) and fall back to the
        # system 'simple' workflow. Alerts use a separate state machine
        # (firing / resolved / silenced) and are intentionally exempt.
        is_insert = self._state.adding
        # Persist only canonical team identities for newly-created work. Existing
        # rows are migrated separately so a routine status-only save cannot look
        # like an assignment-context change to the routing-authority fence.
        if is_insert and self.assignment_group:
            group = str(self.assignment_group).strip().lower().replace('_', '-').replace(' ', '-')
            if group in TeamRoster._CANONICAL_ALIASES:
                self.assignment_group = TeamRoster._CANONICAL_ALIASES[group]
        if is_insert and self.mode == 'task' and not self.workflow_name:
            self._auto_attach_workflow()
        # PLAT-1209: the focused-QA machinery (PLS-184 auto-created QA tasks,
        # the daily high-risk sweep) keys on risk labels that authors rarely
        # set — the 2026-07-02 audit matched 0/32 tasks on a day with auth,
        # agent-runtime, and Books changes. Derive them at insert so every
        # writer (API, MCP, internal services) produces labeled tasks.
        if is_insert and self.mode == 'task':
            self._auto_apply_risk_labels()
        # Snapshot acceptance_mode BEFORE any defaulting so update_fields
        # broadening can persist a create-time/backfill write (Rei P1).
        acceptance_mode_before = self.acceptance_mode
        # PLS-675 / PLAT-1351 v4 (Rei P2-5 / P1): persist the typed
        # acceptance_mode default at insert (and heal null existing rows on
        # save) so the field is the sole enforcement authority. Service-
        # affecting/user-facing/incident work defaults to live; else code.
        # Metadata mirror is create-time backfill only, never a competing
        # authority at resolve time.
        if self.mode == 'task' and not (self.acceptance_mode or '').strip():
            from .services.workflow_service import WorkflowService
            meta = dict(self.metadata or {})
            meta_mode = (meta.get('acceptance_mode') or '').strip().lower()
            if meta_mode in WorkflowService._ACCEPTANCE_MODES_ALL:
                self.acceptance_mode = meta_mode
            elif WorkflowService._package_publish_without_deploy_surface(self):
                # Package-publish artifacts (PLS-1047) deliver a published
                # package, not a k8s DeploymentEvent — never default them to
                # live just because they carry user-facing risk labels. The
                # explicit ``package-publish`` marker prevents over-exemption
                # of deploy-governed work.
                self.acceptance_mode = WorkflowService._ACCEPTANCE_MODE_CODE
            elif WorkflowService._focused_qa_leg_acceptance_exempt(self):
                # Focused-QA legs (PLAT-7506): a leg verifies and never
                # deploys — its origin is either non-deployable (PLAT-7600)
                # or already carries the deploy proof task-correlated to the
                # ORIGIN. Default the persisted field to code, not live.
                self.acceptance_mode = WorkflowService._ACCEPTANCE_MODE_CODE
            elif WorkflowService._is_service_affecting_or_user_facing(self):
                self.acceptance_mode = WorkflowService._ACCEPTANCE_MODE_LIVE
            else:
                self.acceptance_mode = WorkflowService._ACCEPTANCE_MODE_CODE
        metadata_before = dict(self.metadata or {})
        # PLS-301: recurring cadence duties need a stable owner-team marker
        # independent of the mutable assignment_group. Stamp it at creation/setup
        # from the intended current team and then preserve it across transitions
        # unless an explicit caller edits metadata.recurring_owner_team.
        if self.mode == 'task' and self.is_recurring and self.assignment_group:
            meta = dict(self.metadata or {})
            if not meta.get('recurring_owner_team'):
                meta['recurring_owner_team'] = (
                    str(self.assignment_group).strip().lower().replace('_', '-')
                )
                self.metadata = meta
        # Heal invalid status before sync — if the task's status isn't a
        # valid slug for its workflow (e.g. legacy 'pending' on a workflow
        # that only has 'open') the UI strands with "No transitions available
        # from Pending". This runs on every save so any writer that
        # introduces the drift (API, admin, shell, imports) gets auto-healed.
        status_before = self.status
        wfstatus_before = self.workflow_status
        self._coerce_invalid_status()
        # Keep status (slug) and workflow_status (display name) in sync.
        # These two fields encode the same piece of state and any drift
        # produces hard-to-debug bugs — SM-1 / AT-40 showed status='pending'
        # (todo) with workflow_status='done' because the recurring scheduler
        # was only writing one. Resolve the counterpart at save time so no
        # writer can leave them inconsistent.
        self._sync_status_fields()
        completed_at_before = self.completed_at
        self._sync_completed_at_for_status()
        # If healing/sync touched a field that WASN'T in the caller's
        # update_fields, broaden the write so the repair actually hits
        # the DB. Without this, a save(update_fields=['status']) would
        # heal status in memory but drop the recomputed workflow_status /
        # completed_at stamp.
        uf = kwargs.get('update_fields')
        if uf is not None:
            uf = set(uf)
            if dict(self.metadata or {}) != metadata_before:
                uf.add('metadata')
            if self.status != status_before:
                uf.add('status')
            if self.workflow_status != wfstatus_before:
                uf.add('workflow_status')
            if self.completed_at != completed_at_before:
                uf.add('completed_at')
            if self.acceptance_mode != acceptance_mode_before:
                uf.add('acceptance_mode')
            kwargs['update_fields'] = list(uf)
        with transaction.atomic():
            # ADR-0041 writer floor: detect the *persisted pair delta*, not the
            # caller's update_fields hint.  A plain ``item.save()`` otherwise
            # bypasses the fence because update_fields is None.  Locking the
            # previous row also makes the comparison part of the assignment
            # transaction rather than a stale in-memory assertion.
            previous_tuple = None
            state_previous = None
            if not is_insert and self.pk:
                previous_tuple = Item.objects.select_for_update().filter(
                    pk=self.pk,
                ).values_list(
                    'assignee_id', 'assignee_email', 'project_id',
                    'assignment_group', 'workflow_name',
                    'status',
                    # HIVE-1153 (review P1 #1): the resolver's requirement inputs,
                    # so a requirement-input-only delta on a retained owner is
                    # detected as an authorizing write (indices 6-10).
                    'labels', 'tags', 'metadata', 'title', 'description',
                    # PLS-467 extends the same locked read with its registered
                    # state vector, avoiding a second query on every Item.save.
                    'workflow_status', 'resolution_id', 'resolution_date',
                    'is_archived', 'archived_at', 'priority', 'state_generation',
                ).first()
                if previous_tuple is not None:
                    # Keep the PLS-467 view named rather than leaking the
                    # routing tuple's positional representation into its service.
                    state_previous = {
                        'assignee_id': previous_tuple[0],
                        'assignee_email': previous_tuple[1],
                        'assignment_group': previous_tuple[3],
                        'project_id': previous_tuple[2],
                        'workflow_name': previous_tuple[4],
                        'workflow_status': previous_tuple[11],
                        'status': previous_tuple[5],
                        'resolution_id': previous_tuple[12],
                        'resolution_date': previous_tuple[13],
                        'is_archived': previous_tuple[14],
                        'archived_at': previous_tuple[15],
                        'priority': previous_tuple[16],
                        'metadata': previous_tuple[8],
                        'state_generation': previous_tuple[17],
                    }
            if previous_tuple is not None and previous_tuple[5] != self.status:
                previous_status = (previous_tuple[5] or '').strip().lower()
                current_status = (self.status or '').strip().lower()
                if current_status == 'waiting_external':
                    # Operator 2026-09-26: the status is retired. An enter-armed
                    # authority is not permission to park a chain root.
                    raise ValidationError({
                        'status': 'waiting_external_retired',
                    })
                elif previous_status == 'waiting_external':
                    exit_armed = ExternalWaitAuthority.objects.filter(
                        item_id=self.pk,
                        wait_generation=self.external_wait_generation,
                        state=ExternalWaitAuthority.STATE_EXIT_ARMED,
                    ).exists()
                    if not exit_armed:
                        raise ValidationError({
                            'status': 'waiting_external_requires_typed_release',
                        })
            current_pair = (
                self.assignee_id,
                (self.assignee_email or '').strip().lower(),
            )
            normalized_previous = (
                previous_tuple[0], (previous_tuple[1] or '').strip().lower(),
            ) if previous_tuple is not None else (None, '')
            current_context = (
                self.project_id,
                str(self.assignment_group or '').strip().lower().replace('_', '-'),
                str(self.workflow_name or '').strip(),
            )
            previous_context = (
                previous_tuple[2],
                str(previous_tuple[3] or '').strip().lower().replace('_', '-'),
                str(previous_tuple[4] or '').strip(),
            ) if previous_tuple is not None else (None, '', '')
            assignment_write = (
                (is_insert and bool(current_pair[0] or current_pair[1]))
                or (not is_insert and current_pair != normalized_previous)
            )
            context_write = (
                not is_insert
                and bool(normalized_previous[0] or normalized_previous[1])
                and current_context != previous_context
            )
            guarded_tuple_write = assignment_write or context_write
            # HIVE-1153 P1 #1(a) (akira): a RETAINED owner whose STAGE (status) or
            # routing context drifts is still an authorizing write for the substrate
            # fence — a seat moving INTO a substrate-required stage without a fresh
            # receipt must fail closed, not slip through as a status-only no-op. The
            # substrate fence's own substrate_required early-return still no-ops any
            # write into a non-substrate seat, so this only tightens writes that
            # actually (re)authorize substrate access. previous_tuple[5] = pre-save
            # status.
            previous_status = previous_tuple[5] if previous_tuple is not None else None
            # PLS-620: pending urgent data-loss/user-harm containment is a
            # write-time invariant, not only an API-transition convention.  A
            # direct model/admin/status writer must not bypass the same gate.
            if (
                (is_insert or str(getattr(self, 'status', '') or '') != str(previous_status or ''))
                and not getattr(self, '_suppress_containment_status_gate', False)
            ):
                from .services.urgent_risk_containment import assert_status_write_allowed
                assert_status_write_allowed(self)
            stage_write = (
                not is_insert
                and bool(normalized_previous[0] or normalized_previous[1])
                and str(getattr(self, 'status', '') or '') != str(previous_status or '')
            )
            # HIVE-1153 (review P1 #1 / HIVE-1176 rule): a requirement-input-only
            # change (labels/tags/metadata/title/description) on a RETAINED concrete
            # owner is an authorizing write — the NEW content can introduce a
            # runtime-substrate requirement (e.g. needs:kubeconfig) that must fail
            # closed without a fresh signed receipt, not slip through as a non-owner
            # write. Compare the persisted OLD (indices 6-10) against the NEW values.
            requirement_input_write = False
            if (not is_insert and previous_tuple is not None
                    and bool(normalized_previous[0] or normalized_previous[1])):
                requirement_input_write = (
                    (previous_tuple[6] or {}) != (self.labels or {})
                    or (previous_tuple[7] or '') != (self.tags or '')
                    or (previous_tuple[8] or {}) != (self.metadata or {})
                    or (previous_tuple[9] or '') != (self.title or '')
                    or (previous_tuple[10] or '') != (self.description or '')
                )
            substrate_authorizing_write = (
                assignment_write or context_write or stage_write
                or requirement_input_write)
            wip_reservation = None
            if getattr(self, '_wip_reservation_token', None):
                from .services.workflow_service import WorkflowService
                try:
                    wip_reservation = WorkflowService._verify_wip_reservation(self)
                except Exception:
                    # PLS-711 §7.1 containment (@hiro's ruling). This verify
                    # raises BEFORE the try whose except sits further down, so
                    # on failure NOTHING is cleaned up: the reservation row
                    # survives and -- because `_wip_reservation_token` is an
                    # instance attribute, not DB state -- the token stays
                    # ATTACHED. Every subsequent save on the same in-memory
                    # instance therefore re-enters this same failing verify.
                    #
                    # That is what turns one crash into repeated destruction of
                    # the payload: the caller catches, continues, saves again,
                    # and fails again for the same reason. Releasing here breaks
                    # the chain.
                    #
                    # This is CONTAINMENT, not the fix. The mismatch itself is
                    # the sentinel asymmetry at ws.py:11814 vs :12020 -- mint
                    # reads a None slot_kind as "consumes no capacity, don't
                    # reserve", verify reads it as a comparand and raises. The
                    # exception is re-raised unchanged so that fix is still
                    # required and this cannot mask it.
                    #
                    # `_release_unconsumed_wip_reservation` deletes the row and
                    # detaches the token in a finally, which is the existing
                    # precedent at ws.py:2792-2796.
                    #
                    # ⚠️ The release is itself wrapped, because it CAN raise --
                    # its queryset filters on a UUID column, so a malformed
                    # token turns cleanup into a `not a valid UUID` error that
                    # REPLACES the verify's real diagnosis. Cleanup must never
                    # be able to mask the fault it is cleaning up after; the
                    # original exception is what tells an operator why the
                    # escalation failed. Pinned by
                    # test_CONTROL_the_original_error_survives_a_failing_release.
                    try:
                        WorkflowService._release_unconsumed_wip_reservation(self)
                    except Exception:
                        logger.exception(
                            'PLS-711: releasing the WIP reservation failed after a '
                            'verify error on %s; re-raising the ORIGINAL error',
                            getattr(self, 'item_key', None) or self.pk,
                        )
                        if hasattr(self, '_wip_reservation_token'):
                            delattr(self, '_wip_reservation_token')
                    raise
            if guarded_tuple_write:
                from .services.routing_authority import (
                    apply_direct_handoff_command,
                    prepare_assignment_clear_receipt,
                    prepare_assignment_direct_handoff_receipt,
                    prepare_assignment_write_receipt,
                    routing_authority_applies_to_context,
                    subject_authorized_for_item,
                )
                # PLS-539 (hiro/ADR-0010): the entrypoint authorized this direct
                # override (reason gate + task-org authorization) and set the typed
                # _pls539_direct_override signal. Snapshot the crossing HERE — inside
                # the guarded transaction, with the NEW owner/lane already applied to
                # self — so the mint reads the actually-persisted result (no serializer
                # drift) and the fence below keys on the resulting typed decision.
                # apply_...() no-ops (no signal) unless the OLD lane is guarded.
                if (getattr(self, '_pls539_direct_override', False)
                        and not hasattr(self, '_routing_authority_direct_handoff')):
                    apply_direct_handoff_command(
                        self,
                        target_subject_id=self.assignee_id,
                        target_email=self.assignee_email,
                        reason=getattr(self, '_pls539_direct_handoff_reason', '') or '',
                    )
                old_guarded = routing_authority_applies_to_context(
                    previous_context[0], previous_context[1],
                )
                new_guarded = routing_authority_applies_to_context(
                    current_context[0], current_context[1],
                )
                # ADR-0041 binding lanes carry a stronger, one-use exact-org
                # authorization receipt below. Let that fence produce its
                # precise denial/receipt instead of pre-empting it with the
                # legacy TeamRoster guard.
                if (
                    current_pair[1]
                    and self.project_id is not None
                    and not (old_guarded or new_guarded)
                ):
                    from .services.workflow_service import WorkflowService
                    # PLS-539 (revi #47 P1.2): validate the assignee against the group
                    # that is ACTUALLY BEING PERSISTED — uniformly, for direct overrides
                    # and team routes alike. The entry points already clear a LEFTOVER
                    # stale group on a direct override (so self.assignment_group is None
                    # here → the assignee is validated by task-org binding only, which is
                    # what lets a valid same-org named-decider handoff through) and RETAIN
                    # a CO-SUPPLIED group (so self.assignment_group names the team → exact
                    # (org, team) membership is enforced, identical to create). This
                    # replaces the old ``None if _pls539_direct_override`` short-circuit,
                    # which let /assign + PATCH persist a retained team label whose owner
                    # was only org-validated, not an exact member (the P1.2 gap).
                    mismatch, roster_org, task_org = (
                        WorkflowService.assignment_org_mismatch(
                            self,
                            current_pair[1],
                            self.assignment_group,
                            user_id=current_pair[0],
                        )
                    )
                    group = WorkflowService._canonical_team_name(
                        self.assignment_group,
                    )
                    is_team_lead = bool(
                        group
                        and (
                            WorkflowService._resolve_team_lead_email(
                                group,
                                item=self,
                            ) or ''
                        ).strip().lower() == current_pair[1]
                    )
                    operator_lane = bool(
                        (
                            group == 'admin-ops'
                            or WorkflowService._is_operator_capable_team(group)
                        )
                        and WorkflowService._is_operator_target(
                            current_pair[0],
                            current_pair[1],
                        )
                    )
                    if mismatch and not (is_team_lead or operator_lane):
                        # PLS-1080: use the truthful denial reason (which maps the
                        # not-in-team/unresolved/routing-authority sentinels) instead
                        # of the raw ``({roster_org} != {task_org})`` tuple. The old
                        # format printed ``(shizuha != shizuha)`` — two EQUAL org
                        # strings — whenever the assignee was a same-org member who
                        # was simply not in the target team, hiding the real reason.
                        raise ValidationError({
                            'assignee_email': (
                                'Assignee is not authorized for this task '
                                'organization Team: '
                                + WorkflowService.assignment_denial_reason(
                                    current_pair[1],
                                    roster_org,
                                    task_org,
                                )
                            ),
                        })
                    if connection.vendor == 'postgresql':
                        proof = '|'.join([
                            str(self._project_organization_id(self.project_id) or 0),
                            str(self.project_id or 0),
                            str(current_pair[0] or 0),
                            self._canonical_assignment_guard_email(current_pair[1]),
                            group,
                        ])
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT set_config("
                                "'pulse.assignment_org_proof', %s, true)",
                                [proof],
                            )
                if old_guarded or new_guarded:
                    assignment_clear = bool(
                        previous_tuple is not None
                        and (normalized_previous[0] or normalized_previous[1])
                        and not current_pair[0]
                        and not current_pair[1]
                    )
                    if assignment_clear:
                        # Clearing a stale concrete owner is a distinct guarded
                        # command, not a weakened assignment write.  Same-lane
                        # HOLD, public unassign, and destination group-queue
                        # reroute are separate reviewed actions.  Each command
                        # snapshot binds the complete OLD and exact result tuple
                        # and is revalidated against this row lock before its
                        # one-use DB receipt is armed.
                        proof = getattr(
                            self, '_routing_authority_clear_decision', None,
                        )
                        if (not proof
                                or proof.item_id != self.pk
                                or proof.project_id != previous_context[0]
                                or proof.assignment_group != previous_context[1]
                                or proof.workflow_name != previous_context[2]
                                or proof.subject_id != normalized_previous[0]
                                or proof.assignee_email != normalized_previous[1]
                                or proof.result_project_id != current_context[0]
                                or proof.result_assignment_group != current_context[1]
                                or proof.result_workflow_name != current_context[2]):
                            raise ValidationError({
                                'assignee_id': 'routing_authority:direct_writer_denied',
                            })
                        prepare_assignment_clear_receipt(self, proof)
                    else:
                        # PLS-539 (hiro/ADR-0010): an authorized direct handoff is an
                        # INTENTIONAL crossing OUT of a guarded lane into a concrete
                        # owner in an UNGUARDED lane. It is permitted iff the shared
                        # direct-handoff command carried a typed decision whose exact
                        # OLD-lane / NEW-owner / NEW-lane tuple matches THIS transition
                        # (the fence keys on that typed signal, not a bare group-diff).
                        # prepare_...() re-validates the NEW owner/lane against the item
                        # and arms the one-use receipt the trigger consumes. A reasonless
                        # or unauthorized clear carries no decision and still falls to the
                        # guarded-move denial below.
                        dh = getattr(self, '_routing_authority_direct_handoff', None)
                        dh_ok = bool(
                            dh is not None
                            and not new_guarded
                            and (current_pair[0] or current_pair[1])
                            and dh.item_id == self.pk
                            and dh.project_id == previous_context[0]
                            and dh.assignment_group == previous_context[1]
                            and dh.workflow_name == previous_context[2]
                            and dh.subject_id == self.assignee_id
                            and dh.assignee_email == current_pair[1]
                            and dh.result_project_id == current_context[0]
                            and dh.result_assignment_group == current_context[1]
                            and dh.result_workflow_name == current_context[2]
                        )
                        if dh_ok:
                            prepare_assignment_direct_handoff_receipt(self, dh)
                            # The org-isolation trigger (0105) independently guards
                            # this write and accepts a matching pulse.assignment_org_
                            # proof for the NEW tuple. The guarded receipt path does
                            # not set it (it arms the receipt config instead), so an
                            # authorized crossing that lands an owner in the cleared
                            # NEW lane must supply the NEW-tuple org proof here too,
                            # exactly as the unguarded assignment path does.
                            if connection.vendor == 'postgresql':
                                org_proof = '|'.join([
                                    str(self._project_organization_id(self.project_id) or 0),
                                    str(self.project_id or 0),
                                    str(current_pair[0] or 0),
                                    self._canonical_assignment_guard_email(current_pair[1]),
                                    current_context[1],
                                ])
                                with connection.cursor() as cursor:
                                    cursor.execute(
                                        "SELECT set_config("
                                        "'pulse.assignment_org_proof', %s, true)",
                                        [org_proof],
                                    )
                        else:
                            # Moving an assigned item away from a guarded exact lane
                            # is not a way to escape the fence.  A valid destination
                            # must itself be an armed exact-org lane with a concrete
                            # subject.
                            if (not new_guarded
                                    or not current_pair[0]
                                    or not current_pair[1]):
                                raise ValidationError({
                                    'assignee_id': 'routing_authority:direct_writer_denied',
                                })
                            proof = getattr(self, '_routing_authority_decision', None)
                            if (not proof or not proof.allowed
                                    or proof.subject_id != self.assignee_id
                                    or (self.pk and proof.item_id != self.pk)
                                    or proof.project_id != self.project_id
                                    or str(proof.assignment_group or '').strip().lower().replace(
                                        '_', '-') != current_context[1]
                                    or str(proof.workflow_name or '').strip()
                                       != current_context[2]):
                                raise ValidationError({
                                    'assignee_id': 'routing_authority:direct_writer_denied',
                                })
                            fresh = subject_authorized_for_item(
                                self.assignee_id, self, team_name=self.assignment_group,
                                lock=True,
                                expected_project_id=self.project_id,
                                expected_assignment_group=self.assignment_group,
                                expected_workflow_name=self.workflow_name,
                            )
                            if (not fresh.allowed
                                    or fresh.binding_version != proof.binding_version
                                    or fresh.entitlement_version != proof.entitlement_version
                                    or fresh.subject_generation != proof.subject_generation
                                    or fresh.revocation_high_water != proof.revocation_high_water
                                    or fresh.freshness_vector_digest != proof.freshness_vector_digest
                                    or fresh.delegation_version != proof.delegation_version):
                                raise ValidationError({
                                    'assignee_id': f'routing_authority:{fresh.reason}',
                                })
                            # The PostgreSQL trigger independently rejects QuerySet,
                            # bulk and raw-SQL assignment writes.  This append-only,
                            # transaction-local receipt binds the exact decision tuple
                            # and is consumed by that trigger in the same transaction.
                            prepare_assignment_write_receipt(self, fresh)
            # PLS-378: pass effective update_fields so status-only transitions
            # do not fail on pre-existing identity drift.
            self._validate_assignee_identity_consistency(kwargs.get('update_fields'))
            self._validate_recurring_configuration()
            self._validate_blocked_entry(kwargs.get('update_fields'))
            self._validate_blocker_non_wip_entry(kwargs.get('update_fields'))
            # HIVE-1153 P1 #3 increment 3c: under enforce, revalidate the carried
            # substrate-capability receipt LOCALLY under the Item(+Workflow) row
            # locks before the owner write commits. Any policy/stage/intent, writer-
            # generation, producer-tuple, or 5s-expiry drift HOLDs with zero
            # ownership write. No-op unless mode==enforce AND a receipt was
            # snapshotted for the seat being committed (no producer fetch here).
            from .services.workflow_service import WorkflowService as _SubstrateWFS
            _substrate_drift = _SubstrateWFS._revalidate_substrate_receipt_at_commit(
                self,
                # P1 #1(a): owner-delta OR retained-owner context/stage drift — any
                # write that (re)authorizes a substrate-required seat requires proof.
                is_owner_write=substrate_authorizing_write,
                previous_assignee_email=normalized_previous[1])
            if _substrate_drift:
                raise ValidationError({
                    'assignee_id': f'substrate_capability:{_substrate_drift}',
                })
            # HIVE-1153 P1 #3 arming: under enforce, arm the one-use write-receipt the
            # tasks_task guard triggers require+consume, so a QuerySet.update / raw SQL
            # concrete-owner write (which arms nothing, sets no GUC) fails closed. No-op
            # unless mode==enforce (activation OFF -> strict no-op). For a substrate-
            # required seat this runs only after the signed-receipt revalidation above
            # returned None. guarded_write = this UPDATE writes a trigger UPDATE-OF column.
            _uf = kwargs.get('update_fields')
            _guarded_cols = {
                'assignee', 'assignee_id', 'assignee_email', 'project', 'project_id',
                'assignment_group', 'workflow_name', 'status', 'labels', 'tags',
                'metadata', 'title', 'description',
            }
            _guarded_write = (_uf is None) or bool(set(_uf) & _guarded_cols)
            _SubstrateWFS._arm_substrate_write_receipt_at_commit(
                self, is_insert=is_insert, guarded_write=_guarded_write)
            _state_mutation = None
            if state_previous is not None:
                from .services.item_state_mutation import (
                    abort_model_save,
                    finish_model_save,
                    prepare_model_save,
                )
                _state_mutation = prepare_model_save(
                    self, state_previous, kwargs.get('update_fields')
                )
                if _state_mutation is not None and kwargs.get('update_fields') is not None:
                    _state_update_fields = set(kwargs['update_fields'])
                    _state_update_fields.add('state_generation')
                    kwargs['update_fields'] = list(_state_update_fields)
            try:
                super().save(*args, **kwargs)
                if _state_mutation is not None:
                    finish_model_save(self, _state_mutation)
                _deferred_containment_trigger = getattr(
                    self, '_deferred_urgent_risk_reconcile_trigger', None
                )
                if _deferred_containment_trigger:
                    delattr(self, '_deferred_urgent_risk_reconcile_trigger')
                    from .services.urgent_risk_containment import reconcile_item
                    _containment_result = reconcile_item(
                        self.pk, trigger=_deferred_containment_trigger
                    )
                    if _containment_result.get('owner_changed'):
                        self.assignee_id = _containment_result.get('assignee_id')
                        self.assignee_email = _containment_result.get('assignee_email')
                        self.assignment_group = _containment_result.get(
                            'assignment_group'
                        )
            except Exception:
                if _state_mutation is not None:
                    abort_model_save(self, _state_mutation)
                if hasattr(self, '_deferred_urgent_risk_reconcile_trigger'):
                    delattr(self, '_deferred_urgent_risk_reconcile_trigger')
                raise
            if wip_reservation is not None:
                wip_reservation.delete()
        if hasattr(self, '_substrate_capability_receipts'):
            delattr(self, '_substrate_capability_receipts')
        if hasattr(self, '_substrate_commit_binding'):
            delattr(self, '_substrate_commit_binding')
        if hasattr(self, '_routing_authority_decision'):
            delattr(self, '_routing_authority_decision')
        # PLAT-10496: the clear receipt is posted inside the clear command's
        # atomic block. Registering it here, after that block has exited, made
        # on_commit run immediately and survive a later abort of this save.
        if hasattr(self, '_routing_authority_clear_decision'):
            delattr(self, '_routing_authority_clear_decision')
        if hasattr(self, '_routing_authority_direct_handoff'):
            delattr(self, '_routing_authority_direct_handoff')
        if hasattr(self, '_pls539_direct_handoff_reason'):
            delattr(self, '_pls539_direct_handoff_reason')
        if hasattr(self, '_wip_reservation_token'):
            delattr(self, '_wip_reservation_token')

    def _validate_blocked_entry(self, update_fields=None):
        """Reject ORM saves that enter ``blocked`` without an owned blocker.

        API PATCH already requires workflow transitions, but internal automation
        and ad-hoc service code also call ``save``. This model choke-point covers
        those writers; the deferred PostgreSQL trigger added by PLAT-4215 covers
        inserts, ``QuerySet.update`` and raw SQL below the ORM.
        """
        if (self.status or '').strip().lower() != 'blocked':
            return
        if update_fields is not None and 'status' not in set(update_fields):
            return
        if self._state.adding:
            raise ValidationError({
                'status': (
                    'Cannot create an Item directly in `blocked`: a durable parent '
                    'row must exist before an active assigned blocker task can be '
                    'linked (PLAT-4215). Create it in its working status, then use '
                    'WorkflowService.execute_transition / the execute-transition API.'
                ),
            })
        previous = type(self).objects.filter(pk=self.pk).values_list('status', flat=True).first()
        if (previous or '').strip().lower() == 'blocked':
            return

        from .services.workflow_service import WorkflowService
        if WorkflowService._has_active_assigned_blocker(self):
            return
        raise ValidationError({
            'status': (
                'Cannot persist `blocked` without an active linked blocker task '
                'that has a concrete assignee or assignment_group (PLAT-4215). Use '
                'WorkflowService.execute_transition / the execute-transition API.'
            ),
        })

    def _validate_blocker_non_wip_entry(self, update_fields=None):
        """Reject direct ORM writes that park an active blocker outside WIP."""
        if update_fields is not None and 'status' not in set(update_fields):
            return
        from .services.workflow_service import WorkflowService
        if not WorkflowService._is_parked_blocker_status(self.status):
            return
        if self._state.adding or not self.pk:
            return
        previous = type(self).objects.filter(pk=self.pk).values_list(
            'status', flat=True,
        ).first()
        if WorkflowService._is_parked_blocker_status(previous):
            return
        if WorkflowService._has_active_blocked_descendants(self):
            raise ValidationError({
                'status': (
                    'Cannot park an active blocker outside WIP. Its blocked '
                    'descendants must be released first.'
                ),
            })

    def _auto_apply_risk_labels(self):
        """Merge keyword-derived QA risk labels into tags at insert (PLAT-1209).

        Additive only — author-provided tags/labels are preserved; derivation
        failures must never block task creation.
        """
        try:
            from .services.risk_labels import derive_risk_labels, merge_into_tags
            derived = derive_risk_labels(self.title, self.description)
            if derived:
                self.tags = merge_into_tags(self.tags, derived)
        except Exception:
            logger.warning("risk-label derivation failed for %r", self.title, exc_info=True)

    def _coerce_invalid_status(self):
        """Heal the item's status when it's not a valid slug in the workflow.

        A recurring class of bugs in Pulse has been tasks stranded with
        legacy status slugs (`pending`, `completed`, `open` against a
        workflow that doesn't contain them). The UI reports "No transitions
        available from X" because WorkflowTransition.from_status only stores
        slugs actually defined on the workflow.

        Healing strategy:
          1. Legacy slug → canonical equivalent if the workflow has one.
             (pending→todo/open, completed→done, cancelled→cancelled,
             closed→closed, todo→todo, etc.)
          2. Category match — pick the same-category status with the
             lowest position (e.g. legacy 'pending' is todo-category →
             use the workflow's first todo-category status).
          3. Last resort — workflow.initial_status.slug.

        Runs on every save so any writer (import, MCP, shell, migration
        rerun) that introduces a stranded status gets corrected
        deterministically, with a warning logged for operator visibility.
        """
        if self.mode != 'task':
            return
        slug = (self.status or '').strip()
        if not slug or ' ' in slug or '[' in slug:
            # Empty or malformed — _sync_status_fields / _auto_attach_workflow
            # will handle those cases separately.
            return
        if not self.workflow_name:
            return

        # waiting_external is retired. A row already parked may still be
        # saved so a typed release can move it. A new entry is refused.
        if slug == 'waiting_external':
            if self.pk and Item.objects.filter(
                pk=self.pk, status='waiting_external',
            ).exists():
                return
            raise ValidationError({
                'status': 'waiting_external_retired',
            })

        wf = Workflow.resolve(self.workflow_name)
        if not wf:
            return

        healed = Item.remap_status_for_workflow(slug, wf)
        if not healed or healed == slug:
            return  # already valid, or couldn't resolve a better target

        logger.warning(
            "Coercing invalid status %r -> %r on %s (workflow=%s)",
            slug, healed, self.item_key or f'#{self.pk}', wf.slug,
        )
        self.status = healed
        # Also clear stale workflow_status so _sync_status_fields picks
        # up the healed slug's display name.
        self.workflow_status = None

    @staticmethod
    def remap_status_for_workflow(slug, wf):
        """Resolve the status slug to use for ``slug`` under workflow ``wf``.

        Returns the slug unchanged if it is already valid in ``wf``; else a
        legacy-alias match, else the lowest-position status sharing ``slug``'s
        category, else ``wf``'s initial status, else None. PURE (no mutation) —
        shared by save-time coercion (``_coerce_invalid_status``) and the
        PLS-54 workflow-migration preview so dry-run matches the real apply.
        """
        slug = (slug or '').strip()
        if not wf or not slug or ' ' in slug or '[' in slug:
            return None
        valid_slugs = set(wf.workflow_statuses.values_list('status__slug', flat=True))
        if slug in valid_slugs:
            return slug
        # Look up the source Status row for category-based coercion.
        from_status = Status.objects.filter(slug=slug).first()
        # 1) Legacy-name aliases (historical slugs predating per-workflow tables).
        LEGACY_ALIASES = {
            'pending':   ['todo', 'open', 'pending'],
            'todo':      ['todo', 'open'],
            'open':      ['open', 'todo'],
            'completed': ['done', 'completed', 'closed'],
            'closed':    ['closed', 'done'],
            'cancelled': ['cancelled', 'canceled'],
            'canceled':  ['cancelled', 'canceled'],
            'in_progress': ['in_progress'],
            'blocked':   ['blocked'],
        }
        for candidate in LEGACY_ALIASES.get(slug, []):
            if candidate in valid_slugs:
                return candidate
        # 2) Category match — lowest-position status sharing the source category.
        if from_status:
            ws_match = (
                wf.workflow_statuses
                  .filter(status__category=from_status.category)
                  .order_by('position')
                  .select_related('status')
                  .first()
            )
            if ws_match:
                return ws_match.status.slug
        # 3) Last resort — workflow's initial status.
        if wf.initial_status_id:
            return wf.initial_status.slug
        return None

    def _sync_status_fields(self):
        """Resolve status↔workflow_status so they always agree."""
        if self.mode != 'task':
            return
        # Prefer status (slug) as the source of truth when present — it's
        # what every filter & workflow engine query uses.
        slug = (self.status or '').strip()
        if slug and ' ' not in slug and '[' not in slug:
            from .models import Status
            try:
                s = Status.objects.get(slug=slug)
                if self.workflow_status != s.name:
                    self.workflow_status = s.name
                return
            except Status.DoesNotExist:
                pass
        # No valid status slug — fall back to resolving from workflow_status.
        wf_name = (self.workflow_status or '').strip()
        if wf_name:
            from .models import Status
            s = Status.objects.filter(name__iexact=wf_name).first()
            if s:
                self.status = s.slug
                self.workflow_status = s.name

    def _sync_completed_at_for_status(self):
        """Keep completed_at aligned with workflow done-category status.

        Workflow transitions are the normal path, but tasks also move through
        imports, shell fixes, API bulk updates, and scheduler code. This model
        guard makes the completion timestamp a semantic invariant instead of a
        best-effort transition side-effect.
        """
        if self.mode != 'task':
            return
        category = None
        try:
            from .models import Status
            category = (
                Status.objects
                .filter(slug=self.status)
                .values_list('category', flat=True)
                .first()
            )
        except Exception:
            category = None

        # Fallback for legacy/simple statuses if the Status table is unavailable
        # during early migrations or tests with hand-built rows.
        if category is None:
            if self.status in {
                'completed', 'done', 'resolved', 'merged', 'closed',
                'cancelled', 'deferred', 'rejected', 'wont_fix', 'duplicate',
                'failed', 'expired',
            }:
                category = 'done'
            elif self.status:
                category = 'not_done'

        if category == 'done':
            if self.completed_at is None:
                self.completed_at = timezone.now()
        elif category and self.completed_at is not None:
            self.completed_at = None

    def _auto_attach_workflow(self):
        """Resolve and attach a workflow via project scheme → Simple fallback."""
        from .services.workflow_service import WorkflowService
        wf = None
        if self.project_id:
            try:
                wf = WorkflowService.get_workflow_for_item(self)
            except Exception as exc:
                logger.warning(
                    "Item %s: workflow resolution via project scheme failed, "
                    "falling back to Simple (%s)",
                    self.item_key or 'new', exc,
                )
        if wf is None:
            wf = Workflow.objects.filter(slug='simple', is_system=True).first()
        if wf is None:
            # System is genuinely misconfigured — let the caller see the NULL
            return
        self.workflow_name = wf.name
        if not self.workflow_status and wf.initial_status_id:
            self.workflow_status = wf.initial_status.name
        # Sync the legacy `status` field to the workflow's initial slug when
        # the caller left it at the model default. Callers setting an explicit
        # status (e.g. bulk importers reconstructing a mid-workflow task) are
        # respected.
        #
        # NOTE: must use .slug, NOT str(initial_status). The latter returns
        # 'Name [category]' (from Status.__str__) — that display string was
        # the source of the 'Resolved [done]' corruption on the Channel
        # Message Review workflow. `status` is a slug column everywhere else.
        if wf.initial_status_id and self.status in ('', 'pending'):
            self.status = wf.initial_status.slug

    # === JSON Field Helpers ===
    def get_metadata_dict(self):
        """Get metadata as dict (handles both JSONField and legacy TextField)"""
        if isinstance(self.metadata, dict):
            return self.metadata
        if self.metadata:
            try:
                return json.loads(self.metadata)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def get_recurrence_pattern_dict(self):
        """Parse recurrence pattern"""
        if isinstance(self.recurrence_pattern, dict):
            return self.recurrence_pattern
        if self.recurrence_pattern:
            try:
                return json.loads(self.recurrence_pattern)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def set_recurrence_pattern(self, pattern):
        """Set recurrence pattern"""
        self.recurrence_pattern = json.dumps(pattern) if pattern else None

    def get_tags_list(self):
        """Get tags as a list"""
        if self.tags:
            return [tag.strip() for tag in self.tags.split(',') if tag.strip()]
        return []

    def set_tags_list(self, tags_list):
        """Set tags from a list"""
        self.tags = ','.join(tags_list) if tags_list else None

    # === Status Helpers ===
    def is_overdue(self):
        """Check if item is overdue"""
        # PLS-230: recognize the canonical terminal slugs, not just legacy ones.
        # Task-mode items auto-attach the Simple workflow and coerce e.g.
        # completed/cancelled -> done, so a hardcoded legacy-only terminal list
        # left done/closed/merged/etc. reading as overdue.
        terminal = {
            'completed', 'cancelled', 'canceled', 'resolved', 'rejected', 'deferred',
            'done', 'closed', 'merged', 'duplicate', 'wont_fix', 'failed', 'expired',
        }
        if self.due_date and self.status not in terminal:
            return timezone.now() > self.due_date
        return False

    def days_until_due(self):
        """Get days until due date"""
        if self.due_date:
            delta = self.due_date - timezone.now()
            return delta.days
        return None

    def is_silenced(self):
        """Check if item is currently silenced"""
        if self.silenced_until:
            return timezone.now() < self.silenced_until
        return False

    def is_active(self):
        """Check if item is in an active state"""
        if self.mode == 'task':
            # PLS-230: include the canonical todo-category slugs — task-mode
            # items coerce pending/open -> todo via the Simple workflow, so a
            # legacy-only ['pending', ...] list mis-read live 'todo' tasks as
            # inactive.
            return self.status in ['pending', 'todo', 'open', 'reopened', 'in_progress', 'blocked']
        elif self.mode == 'action':
            return self.status in ['pending', 'approved', 'executing']
        elif self.mode == 'alert':
            return self.status in ['firing', 'acknowledged']
        return False

    # === Assignment (PLAT-192: atomic, drift-proof) ===
    def assign(self, user_ref, *, save=True):
        """Atomically set assignee_id + assignee_email from ONE canonical user.

        Both fields are derived from the SAME resolved user, so they can never
        drift apart — the dual-reference bug (#1072 Shizuha-as-Hritik, PLAT-245).
        `user_ref` may be a user-service dict, an int shizuha-id user id, or an
        email str (incl. agents '*@agents.shizuha.io'). An unresolvable ref is a
        no-op (logs, leaves the current assignee untouched) — NEVER a half-set
        write. Returns True when the assignee was set.
        """
        uid, email = self._resolve_user_ref(user_ref)
        if uid is None and not email:
            logger.warning(
                f"Item.assign: could not resolve user_ref={user_ref!r}; assignee unchanged"
            )
            return False
        self.assignee_id = uid
        self.assignee_email = email
        if save:
            self.save(update_fields=['assignee_id', 'assignee_email'])
        # HIVE-608: the Home `task.assignment` event is emitted from the post-save
        # assignee diff in signals.track_item_changes — NOT here — so it fires only
        # after the assignee row is actually saved/committed (assign(save=False)
        # callers batch the save later; emitting here would ghost-publish first).
        return True

    def unassign(self, *, save=True, reason='explicit_unassign'):
        """Clear both assignee fields through the guarded group-queue command."""
        from .services.routing_authority import clear_assignment_to_group_queue

        return clear_assignment_to_group_queue(self, reason=reason, save=save)

    def clean(self):
        """Reject an assignee id/email pair that names two different users.

        PLAT-192 phase 1 still stores assignee_id + assignee_email as denormalized
        scalar columns, so this is the defense-in-depth guard until phase 2
        replaces them with a real local User FK.  Half-resolved rows are repaired
        by the one-time data migration/management command; this validator only
        rejects the dangerous case where both fields are present but disagree.
        """
        super().clean()
        self._validate_assignee_identity_consistency()
        self._validate_recurring_configuration()

    def _validate_recurring_configuration(self):
        from .services.recurring import validate_recurring_configuration

        try:
            validate_recurring_configuration(
                self.is_recurring, self.frequency, self.recurrence_pattern
            )
        except ValueError as exc:
            raise ValidationError({'frequency': str(exc)})

    def _validate_assignee_identity_consistency(self, update_fields=None):
        # PLS-378: only enforce identity-pair consistency for pairs actually
        # WRITTEN in this save. A status-only transition (or any save that does
        # not touch these columns) must not 500 on PRE-EXISTING legacy id/email
        # drift — that left KOT-40/41 permanently un-transitionable when their
        # created_by_id/email (and assignee 1175=tomo vs shizuha-id 22) had
        # drifted (same class as PLAT-1049's alias case). item.save() raises a
        # bare django ValidationError here, which DRF does not map → HTTP 500.
        # The invariant still guards new/changed writes (assign, create) where
        # the columns ARE in update_fields, and clean()/full_clean() (no
        # update_fields) still validates both pairs.
        if update_fields is not None:
            uf = set(update_fields)
            if {'assignee_id', 'assignee_email'} & uf:
                self._validate_identity_pair_consistency('assignee')
            if {'created_by_id', 'created_by_email'} & uf:
                self._validate_identity_pair_consistency('created_by')
            return
        self._validate_identity_pair_consistency('assignee')
        self._validate_identity_pair_consistency('created_by')

    def _validate_identity_pair_consistency(self, field):
        uid = getattr(self, f'{field}_id')
        email = getattr(self, f'{field}_email')
        if not uid or not email:
            return
        try:
            _, canonical_email = self._resolve_user_ref(int(uid))
        except Exception:
            canonical_email = None
        if canonical_email and not self._emails_same_identity(canonical_email, email):
            from django.core.exceptions import ValidationError
            raise ValidationError({
                f'{field}_email': (
                    f'{field}_id and {field}_email refer to different users; '
                    'derive both fields from the same identity source.'
                )
            })

    @staticmethod
    def _canonical_assignment_guard_email(email):
        """Match the PostgreSQL assignment fence's canonical identity key."""
        normalized = str(email or '').strip().lower()
        agent_suffix = '@agents.shizuha.io'
        if normalized.endswith(agent_suffix):
            return f'{normalized[:-len(agent_suffix)]}@shizuha.com'
        return normalized

    @staticmethod
    def _emails_same_identity(a, b):
        """True if two emails name the SAME identity.

        Beyond exact match, an agent's primary `<name>@shizuha.com` and its
        internal `<name>@agents.shizuha.io` alias are ONE identity (PLAT-533) —
        the denormalized `*_email` column may hold either form while the
        `*_id` resolves to the other, which is NOT a real id/email mismatch.
        Without this, a status-only transition on an item whose stored
        assignee/created_by email is in the other alias domain raised a
        ValidationError on save() and 500'd, leaving the item permanently
        un-transitionable (PLAT-1049: EVOL-22/KURA-4, banto@shizuha.com vs
        canonical banto@agents.shizuha.io).

        Conservative: only the two known agent-alias domains collapse to a
        shared local-part; arbitrary domains sharing a local-part are NOT
        treated as the same identity.
        """
        a = (a or '').strip().lower()
        b = (b or '').strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        _ALIAS_DOMAINS = ('@shizuha.com', '@agents.shizuha.io')

        def alias_localpart(e):
            for d in _ALIAS_DOMAINS:
                if e.endswith(d):
                    return e[:-len(d)]
            return None

        la, lb = alias_localpart(a), alias_localpart(b)
        return la is not None and la == lb

    @staticmethod
    def _resolve_user_ref(user_ref):
        """Resolve a user_ref -> (id:int|None, email:str|None) from ONE canonical
        user record via the user_service singleton, so the two values can never
        disagree. Returns (None, None) when unresolvable. bool is rejected (it is
        an int subclass)."""
        from .user_service import user_service
        if isinstance(user_ref, bool) or user_ref in (None, '', 0):
            return None, None
        info = None
        try:
            if isinstance(user_ref, dict):
                rid = user_ref.get('id')
                remail = user_ref.get('email')
                if rid not in (None, '', 0) and str(rid).isdigit():
                    info = user_service.get_user_by_id(int(rid)) or user_ref
                elif remail:
                    info = user_service.get_user_by_email(remail) or user_ref
                else:
                    info = user_ref
            elif isinstance(user_ref, int):
                info = user_service.get_user_by_id(user_ref)
            elif isinstance(user_ref, str):
                ref = user_ref.strip()
                if not ref:
                    return None, None
                if '@' in ref:
                    info = user_service.get_user_by_email(ref)
                elif ref.isdigit():
                    info = user_service.get_user_by_id(int(ref))
        except Exception:
            logger.exception(f"Item._resolve_user_ref failed for {user_ref!r}")
            return None, None
        if not info:
            return None, None
        # PLS-231: fail closed on a non-mapping `info`. A user_service returning a
        # truthy-but-non-dict object (e.g. a test MagicMock, or a malformed upstream
        # payload) must NOT propagate: `MagicMock.get('email')` yields another mock
        # whose .strip()/str()/format chain can balloon the response allocation and
        # OOM the process (the CoS->operator PATCH 12GB blowup). Only ever return a
        # real int id and a real str email.
        if not isinstance(info, dict):
            logger.warning("Item._resolve_user_ref: user_service returned non-dict %s for %r", type(info).__name__, user_ref)
            return None, None
        rid = info.get('id')
        uid = int(rid) if (isinstance(rid, (int, str)) and not isinstance(rid, bool)
                           and str(rid).strip() not in ('', '0') and str(rid).strip().isdigit()) else None
        raw_email = info.get('email')
        email = (raw_email.strip() or None) if isinstance(raw_email, str) else None
        return uid, email

    # === Task Actions ===
    def mark_completed(self):
        """Mark task as completed"""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save()

    # === Action Actions ===
    def approve(self, approved_by='manual', rule=None):
        """Approve an action"""
        if self.mode != 'action':
            raise ValueError("Can only approve actions")
        self.status = 'approved'
        self.approved_by = approved_by
        self.approved_at = timezone.now()
        self.approval_rule = rule
        self.auto_approved = approved_by == 'auto'
        self.save()

    def reject(self, reason='', rejected_by='manual'):
        """Reject an action"""
        if self.mode != 'action':
            raise ValueError("Can only reject actions")
        self.status = 'rejected'
        self.rejection_reason = reason
        self.approved_by = rejected_by  # Track who rejected
        self.approved_at = timezone.now()
        self.save()

    # === Alert Actions ===
    def acknowledge(self, acknowledged_by='manual', comment=''):
        """Acknowledge an alert"""
        if self.mode != 'alert':
            raise ValueError("Can only acknowledge alerts")
        self.status = 'acknowledged'
        self.acknowledged_by = acknowledged_by
        self.acknowledged_at = timezone.now()
        if comment:
            self.resolution_note = comment
        self.save()

    def resolve(self, resolved_by='manual', note=''):
        """Resolve an alert"""
        if self.mode != 'alert':
            raise ValueError("Can only resolve alerts")
        self.status = 'resolved'
        self.resolved_by = resolved_by
        self.resolved_at = timezone.now()
        if note:
            self.resolution_note = note
        self.save()

    def silence(self, until, silenced_by='manual', reason='', rule=None):
        """Silence an item"""
        self.silenced_until = until
        self.silenced_by = silenced_by
        self.silence_reason = reason
        self.silence_rule = rule
        self.save()

    def increment_occurrence(self):
        """Increment occurrence count for deduplicated alerts"""
        self.occurrence_count += 1
        self.last_fired_at = timezone.now()
        self.save(update_fields=['occurrence_count', 'last_fired_at', 'updated_at'])

    # === Fingerprinting ===
    @staticmethod
    def compute_fingerprint(item_type, source, labels, payload=None, config=None):
        """
        Compute a fingerprint for alert deduplication.
        """
        components = [item_type, source]

        # Get label fields to use
        label_fields = None
        payload_fields = []

        if config:
            label_fields = config.get('label_fields')
            payload_fields = config.get('payload_fields', [])

        # Add labels
        if label_fields is None:
            # Use all labels
            for key in sorted(labels.keys()):
                components.append(f"{key}={labels[key]}")
        else:
            # Use configured label fields
            for key in sorted(label_fields):
                if key in labels:
                    components.append(f"{key}={labels[key]}")

        # Add payload fields if configured
        if payload and payload_fields:
            for key in payload_fields:
                if key in payload:
                    components.append(f"payload.{key}={payload[key]}")

        fingerprint_string = "|".join(str(c) for c in components)
        return hashlib.sha256(fingerprint_string.encode()).hexdigest()[:32]

    class Meta:
        db_table = 'tasks_task'  # Keep same table for backward compatibility
        ordering = ['-created_at']
        indexes = [
            # PLS-705: `?search=` uses `__icontains`, i.e. `ILIKE '%needle%'`.
            # A LEADING wildcard is unservable by btree, so every search was a
            # sequential scan whose cost was paid before anything matched --
            # which is why 0 matches, 1 match and 8,160 matches all cost the
            # same (~15-21s). pg_trgm GIN makes the predicate indexable.
            # Built CONCURRENTLY in 0135; this table serves every Pulse request.
            GinIndex(fields=['title'], opclasses=['gin_trgm_ops'],
                     name='pls705_item_title_trgm'),
            GinIndex(fields=['description'], opclasses=['gin_trgm_ops'],
                     name='pls705_item_desc_trgm'),
            GinIndex(fields=['item_key'], opclasses=['gin_trgm_ops'],
                     name='pls705_item_key_trgm'),
            # PLS-712: both array and object task-label predicates compile to
            # jsonb containment (@>).  Default jsonb_ops serves both shapes and
            # preserves object-key operators that jsonb_path_ops omits.
            GinIndex(fields=['labels'], name='pls712_item_labels_gin'),
            models.Index(fields=['mode', 'status']),
            # PLS-698: every agent heartbeat narrows the canonical queue by
            # assignee_email first.  That column is the cross-service identity
            # source of truth (PLS-23/PLAT-533), but it historically had no
            # index, so a synchronized fleet heartbeat made PostgreSQL scan the
            # full tasks table once for the visible page and again for the
            # permission hint.  Keep the remaining equality predicates in the
            # same index so the active-queue candidate set is bounded before
            # blocker/link annotations and queue ordering run.
            models.Index(
                fields=['assignee_email', 'mode', 'is_archived', 'status'],
                name='task_owner_active_queue_idx',
            ),
            models.Index(fields=['item_type']),
            models.Index(fields=['assignee_id']),
            models.Index(fields=['due_date']),
            models.Index(fields=['source', 'source_id']),
            models.Index(fields=['is_recurring']),
            models.Index(fields=['fingerprint']),
            models.Index(fields=['severity']),
            models.Index(fields=['correlation_id']),
            models.Index(fields=['silenced_until']),
            models.Index(fields=['priority']),
            # PLS-1306: the urgent-unassigned gauge exposition
            # (tasks/services/urgent_unassigned_metrics.py) filters
            # priority IN (urgent, high) AND status IN (todo, open,
            # needs_triage) AND (assignee_email IS NULL OR '') and orders by
            # created_at. No existing index leads with those equality
            # columns, so the collection seq-scanned tasks_task and overran
            # the ServiceMonitor's scrapeTimeout — Prometheus kept each
            # pod's last successful sample for tens of minutes and
            # PulseUrgentOperatorTaskStale fired on frozen values. Equality
            # columns first, assignee_email third (IS NULL is btree-servable),
            # created_at last so the oldest-item walk is an ordered scan.
            models.Index(
                fields=['priority', 'status', 'assignee_email', 'created_at'],
                name='pls1306_urgent_unassigned_idx',
            ),
            models.Index(fields=['project', 'status']),
            models.Index(fields=['item_key']),
            models.Index(fields=['sprint', 'status']),
            models.Index(fields=['component']),
            models.Index(fields=['fix_version']),
        ]
        constraints = [
            # Only one active (firing/acknowledged) alert per fingerprint
            models.UniqueConstraint(
                fields=['fingerprint'],
                condition=Q(mode='alert', status__in=['firing', 'acknowledged']),
                name='unique_active_alert_fingerprint'
            ),
            # Every task must belong to a workflow. Alerts and other non-task
            # modes are exempt — they use a separate state machine (firing /
            # resolved / silenced) rather than a workflow with transitions.
            # This is the DB-level belt under the save()-time resolver: even
            # a raw INSERT or a buggy importer can't sneak past this check.
            models.CheckConstraint(
                check=~Q(mode='task') | (
                    Q(workflow_name__isnull=False) & ~Q(workflow_name='')
                ),
                name='task_must_have_workflow_name',
            ),
            # PLS-533: at most ONE close-out assessment per (epic, scope generation).
            # Transactional dedup: a racing/retried final-child event that tries to
            # mint a second assessment for the same generation hits this constraint
            # and resolves to the existing row. Scoped to assessment items so it
            # never touches ordinary tasks.
            models.UniqueConstraint(
                fields=['closeout_parent', 'closeout_generation'],
                condition=Q(item_type='task.epic_closeout_assessment'),
                name='uniq_epic_closeout_assessment_per_generation',
            ),
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                condition=(
                    Q(source='qa_cadence_sweep') &
                    Q(source_id__isnull=False) &
                    ~Q(status__in=[
                        'done', 'completed', 'closed', 'cancelled', 'canceled',
                        'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                        'failed',
                    ])
                ),
                name='uniq_active_qa_cadence_source',
            ),
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                condition=(
                    Q(source='customer_request_portal') &
                    Q(source_id__isnull=False)
                ),
                name='uniq_customer_request_portal_source',
            ),
            # One active security-ci item per stable source_id (rolling ledger
            # or finding). Weekly scans must PATCH the keeper, never mint a sibling.
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                condition=(
                    Q(source='security-ci') &
                    Q(source_id__isnull=False) &
                    ~Q(status__in=[
                        'done', 'completed', 'closed', 'cancelled', 'canceled',
                        'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                        'failed', 'deferred', 'expired',
                    ])
                ),
                name='uniq_active_security_ci_source',
            ),
        ]

class CustomerRequest(models.Model):
    """Public/customer-facing feature request captured for a Pulse project."""

    STATUS_CHOICES = [
        ('submitted', 'Submitted'),
        ('under_review', 'Under Review'),
        ('planned', 'Planned'),
        ('in_progress', 'In Progress'),
        ('shipped', 'Shipped'),
        ('declined', 'Declined'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='customer_requests')
    title = models.CharField(max_length=240)
    description = models.TextField()
    category = models.CharField(max_length=80, blank=True)
    requester_name = models.CharField(max_length=120, blank=True)
    requester_email = models.EmailField(blank=True)
    requester_external_id = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default='submitted', db_index=True)
    upvote_count = models.PositiveIntegerField(default=0)
    created_item = models.ForeignKey(
        'Item',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='customer_request_sources',
        help_text='Pulse item created from this customer request after internal approval.',
    )
    approved_by_id = models.IntegerField(null=True, blank=True)
    approved_by_email = models.EmailField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key}: {self.title}"

    class Meta:
        db_table = 'tasks_customer_request'
        ordering = ['-upvote_count', '-created_at']
        indexes = [
            models.Index(fields=['project', 'status', '-upvote_count']),
            models.Index(fields=['project', 'created_at']),
            models.Index(fields=['created_item']),
        ]


class CustomerRequestVote(models.Model):
    """Idempotent public upvote for a customer request."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(CustomerRequest, on_delete=models.CASCADE, related_name='votes')
    voter_key = models.CharField(max_length=64, help_text='SHA-256 of voter email or anonymous visitor fingerprint')
    voter_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_customer_request_vote'
        unique_together = [['request', 'voter_key']]
        indexes = [models.Index(fields=['request', 'created_at'])]


class CustomerRequestComment(models.Model):
    """Public comment on a customer request; email is retained internally only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(CustomerRequest, on_delete=models.CASCADE, related_name='comments')
    author_name = models.CharField(max_length=120, blank=True)
    author_email = models.EmailField(blank=True)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_customer_request_comment'
        ordering = ['created_at']
        indexes = [models.Index(fields=['request', 'created_at'])]


# Backward compatibility alias
Task = Item


class DeploymentEvent(models.Model):
    """CI/CD deployment ledger entry (PLS-183/PLS-186).

    Successful deploys stay ledger-only. Failure states may link to exactly one
    urgent DevOps task per deployment event + failure kind so CI retries do not
    spam Pulse.
    """

    STATE_CHOICES = [
        ('created', 'Created'),
        ('build_started', 'Build Started'),
        ('build_failed', 'Build Failed'),
        ('manifest_failed', 'Manifest Failed'),
        ('artifact_published', 'Artifact Published'),
        ('rollout_started', 'Rollout Started'),
        ('rollout_stalled', 'Rollout Stalled'),
        ('smoke_pending', 'Smoke Pending'),
        ('smoke_passed', 'Smoke Passed'),
        ('smoke_failed', 'Smoke Failed'),
        ('rollback_started', 'Rollback Started'),
        ('rolled_back', 'Rolled Back'),
        ('superseded', 'Superseded'),
        ('qa_task_created', 'QA Task Created'),
        ('verified_by_qa', 'Verified By QA'),
    ]
    SMOKE_RESULT_CHOICES = [
        ('unknown', 'Unknown'),
        ('pending', 'Pending'),
        ('passed', 'Passed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]
    FAILURE_STATES = {'build_failed', 'manifest_failed', 'rollout_stalled', 'smoke_failed'}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=512, unique=True, db_index=True)

    project = models.ForeignKey(
        'Project', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deployment_events',
    )
    repo = models.CharField(max_length=255, db_index=True)
    branch = models.CharField(max_length=255, blank=True, default='')
    commit_sha = models.CharField(max_length=64, db_index=True)
    service = models.CharField(max_length=160, db_index=True)
    component = models.CharField(max_length=160, blank=True, default='')
    environment = models.CharField(max_length=80, default='live', db_index=True)
    cluster = models.CharField(max_length=120, default='shizuha-k3s', db_index=True)
    namespace = models.CharField(max_length=160, blank=True, default='')
    deployment_target = models.CharField(max_length=255, blank=True, default='')

    image = models.CharField(max_length=512, blank=True, default='')
    image_tag = models.CharField(max_length=255, blank=True, default='')
    image_digest = models.CharField(max_length=255, blank=True, default='')
    manifest_ref = models.CharField(max_length=512, blank=True, default='')

    pipeline_system = models.CharField(max_length=120, default='origin-forgejo')
    pipeline_run_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    pipeline_url = models.URLField(max_length=2048, blank=True, default='')
    logs_url = models.URLField(max_length=2048, blank=True, default='')

    state = models.CharField(max_length=40, choices=STATE_CHOICES, default='created', db_index=True)
    smoke_result = models.CharField(max_length=20, choices=SMOKE_RESULT_CHOICES, default='unknown', db_index=True)

    started_at = models.DateTimeField(null=True, blank=True)
    rollout_started_at = models.DateTimeField(null=True, blank=True)
    rolled_out_at = models.DateTimeField(null=True, blank=True)
    smoke_started_at = models.DateTimeField(null=True, blank=True)
    smoke_finished_at = models.DateTimeField(null=True, blank=True)

    pr_numbers = models.JSONField(default=list, blank=True)
    source_task_keys = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    rollback_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='rollback_events',
    )
    superseded_by = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='superseded_events',
    )
    failure_task = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deployment_failure_events',
    )
    qa_task = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deployment_qa_events',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_deployment_event'
        ordering = ['-updated_at', '-created_at']
        indexes = [
            models.Index(fields=['repo', 'service', 'environment', 'cluster'], name='tasks_deplo_repo_7e1d7a_idx'),
            models.Index(fields=['commit_sha', 'image_digest'], name='tasks_deplo_commit_1f8f2c_idx'),
            models.Index(fields=['state', 'smoke_result'], name='tasks_deplo_state_0e1fd5_idx'),
            models.Index(fields=['updated_at'], name='tasks_deplo_updated_18bb51_idx'),
        ]

    def __str__(self):
        return f"{self.service}@{self.environment} {self.commit_sha[:12]} {self.state}"

    @staticmethod
    def build_idempotency_key(
        *, repo, service, environment='live', cluster='shizuha-k3s', commit_sha='',
        manifest_ref='', pipeline_system='origin-forgejo', image_digest='',
        image_tag='', image='', pipeline_run_id='',
    ):
        # Retry-stable deployment identity: the desired-state manifest ref and
        # pipeline system are part of the deployment candidate identity. CI run
        # ids and image references are retry telemetry that can vary while the
        # same candidate is retried, so keep those parameters accepted for
        # caller compatibility but leave them out of the default key.
        identity = '|'.join([
            (pipeline_system or 'origin-forgejo').strip().lower(),
            (repo or '').strip().lower(),
            (service or '').strip().lower(),
            (environment or 'live').strip().lower(),
            (cluster or 'shizuha-k3s').strip().lower(),
            (commit_sha or '').strip().lower(),
            (manifest_ref or '').strip().lower(),
        ])
        return hashlib.sha256(identity.encode('utf-8')).hexdigest()

    @property
    def is_failure(self):
        return self.state in self.FAILURE_STATES or self.smoke_result == 'failed'

    def save(self, *args, **kwargs):
        if not self.idempotency_key:
            self.idempotency_key = self.build_idempotency_key(
                repo=self.repo,
                service=self.service,
                environment=self.environment,
                cluster=self.cluster,
                commit_sha=self.commit_sha,
                manifest_ref=self.manifest_ref,
                pipeline_system=self.pipeline_system,
                image_digest=self.image_digest,
                image_tag=self.image_tag,
                image=self.image,
                pipeline_run_id=self.pipeline_run_id,
            )
        super().save(*args, **kwargs)


class QaVerificationDemand(models.Model):
    """Latest-wins queue authority for deployment-backed QA (PLS-593).

    ``DeploymentEvent`` remains the immutable attempt ledger.  This row is the
    independently locked identity for the *current verification need*, so a
    burst of commits for one live surface can advance one demand without
    multiplying queue items.
    """

    KIND_CHOICES = [
        ('regression_watchlist', 'Regression watchlist'),
        ('focused_qa', 'Focused QA'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.IntegerField(db_index=True)
    project = models.ForeignKey(
        'Project', on_delete=models.CASCADE, related_name='qa_verification_demands',
    )
    demand_kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    surface_key = models.CharField(max_length=1024)
    environment = models.CharField(max_length=80, default='live')
    primary_risk = models.CharField(max_length=120, blank=True, default='')

    current_generation = models.PositiveBigIntegerField(default=0)
    verified_generation = models.PositiveBigIntegerField(null=True, blank=True)
    active_item = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='active_qa_verification_demands',
    )
    version = models.PositiveBigIntegerField(default=0)
    last_event_at = models.DateTimeField(null=True, blank=True)
    settle_after = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_qa_verification_demand'
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'organization_id', 'project', 'demand_kind', 'surface_key',
                    'environment', 'primary_risk',
                ],
                name='uniq_qa_demand_identity',
            ),
        ]
        indexes = [
            models.Index(
                fields=['demand_kind', 'settle_after'],
                name='qa_demand_settle_idx',
            ),
        ]


class QaVerificationGeneration(models.Model):
    """Persisted, server-ordered delivery generation for a QA demand."""

    STATE_CHOICES = [
        ('accepted', 'Accepted'),
        ('current', 'Current'),
        ('superseded', 'Superseded'),
        ('failed', 'Failed'),
        ('verified', 'Verified'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demand = models.ForeignKey(
        QaVerificationDemand, on_delete=models.CASCADE, related_name='generations',
    )
    generation = models.PositiveBigIntegerField()
    deployment_event = models.ForeignKey(
        DeploymentEvent, on_delete=models.PROTECT, related_name='qa_demand_generations',
    )
    commit_sha = models.CharField(max_length=64)
    artifact_identity = models.CharField(max_length=1024, blank=True, default='')
    event_fingerprint = models.CharField(max_length=64, blank=True, default='')
    surface_sequence = models.DateTimeField()
    state = models.CharField(max_length=24, choices=STATE_CHOICES, default='accepted')
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'tasks_qa_verification_generation'
        constraints = [
            models.UniqueConstraint(
                fields=['demand', 'generation'], name='uniq_qa_demand_generation',
            ),
            models.UniqueConstraint(
                fields=['demand', 'deployment_event'], name='uniq_qa_demand_event',
            ),
        ]
        indexes = [
            models.Index(
                fields=['demand', 'state'], name='qa_generation_state_idx',
            ),
        ]


class QaVerificationOutbox(models.Model):
    """Crash-safe delayed materialization intent, fenced by demand version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demand = models.ForeignKey(
        QaVerificationDemand, on_delete=models.CASCADE, related_name='outbox',
    )
    generation = models.PositiveBigIntegerField()
    version = models.PositiveBigIntegerField()
    available_at = models.DateTimeField(db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_qa_verification_outbox'
        constraints = [
            models.UniqueConstraint(
                fields=['demand', 'version'], name='uniq_qa_demand_outbox_version',
            ),
        ]
        indexes = [
            models.Index(
                fields=['delivered_at', 'available_at'], name='qa_outbox_ready_idx',
            ),
        ]


class QaVerificationAudit(models.Model):
    """Idempotent visible-effect ledger for generation supersession."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demand = models.ForeignKey(
        QaVerificationDemand, on_delete=models.CASCADE, related_name='audit_events',
    )
    from_generation = models.PositiveBigIntegerField(default=0)
    to_generation = models.PositiveBigIntegerField()
    effect_kind = models.CharField(max_length=64)
    deployment_event = models.ForeignKey(
        DeploymentEvent, null=True, blank=True, on_delete=models.PROTECT,
        related_name='qa_demand_audit_events',
    )
    item = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='qa_demand_audit_events',
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_qa_verification_audit'
        constraints = [
            models.UniqueConstraint(
                fields=['demand', 'from_generation', 'to_generation', 'effect_kind'],
                name='uniq_qa_demand_audit_effect',
            ),
        ]
        indexes = [
            models.Index(fields=['demand', 'created_at'], name='qa_audit_demand_idx'),
        ]



class LinkedPullRequestMergeAttestation(models.Model):
    """Provider-verified merge identity for one PR linked to an item.

    Item metadata is presentation/cache state and remains caller-writable.  This
    row is written only after Pulse has read the PR provider API, so deployment
    gates never treat caller-supplied ``metadata.pull_requests`` as authority.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.CASCADE, related_name='merge_attestations',
    )
    provider = models.CharField(max_length=32)
    repo = models.CharField(max_length=255)
    number = models.PositiveIntegerField()
    url = models.URLField(max_length=2048)
    head_sha = models.CharField(max_length=64)
    merge_commit = models.CharField(max_length=64, db_index=True)
    merged_at = models.DateTimeField()
    attested_by = models.CharField(max_length=120, default='pr-sync')
    attested_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_linked_pr_merge_attestation'
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'repo', 'number', 'provider'],
                name='tasks_prma_item_repo_provider_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['item', 'repo', 'number'], name='tasks_prma_lookup_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                'Merge attestations are immutable; a provider conflict must fail loud.'
            )
        return super().save(*args, **kwargs)


class VerificationRequirementManifest(models.Model):
    """Immutable, Security-reviewed deploy-verification requirements (PLS-521).

    A new requirement generation supersedes (but never overwrites) the previous
    generation.  ``classification`` is derived by the service from the finding
    type/components/targets; clients cannot submit a source-only boolean.
    """

    CLASSIFICATION_CHOICES = [
        ('source_only', 'Source only'),
        ('live_required', 'Live deployment required'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.CASCADE, related_name='verification_requirement_manifests',
    )
    requirement_generation = models.PositiveIntegerField()
    classification = models.CharField(max_length=24, choices=CLASSIFICATION_CHOICES)
    merge_commit = models.CharField(max_length=64, db_index=True)
    merge_attestation = models.ForeignKey(
        LinkedPullRequestMergeAttestation,
        on_delete=models.PROTECT,
        related_name='verification_requirement_manifests',
    )
    pull_request_repo = models.CharField(max_length=255)
    pull_request_number = models.PositiveIntegerField()
    environment = models.CharField(max_length=80, blank=True, default='')
    service = models.CharField(max_length=160, blank=True, default='')
    workload = models.CharField(max_length=255, blank=True, default='')
    # PLS-521 P1 #3: the reviewed cluster + namespace the deployment must land in.
    # Bound into the manifest (and its digest) so the gate rejects evidence from a
    # different cluster/namespace even when env/service/workload match.
    cluster = models.CharField(max_length=120, blank=True, default='')
    namespace = models.CharField(max_length=160, blank=True, default='')
    affected_components = models.JSONField(default=list, blank=True)
    scrape_targets = models.JSONField(default=list, blank=True)
    metric_series = models.JSONField(default=list, blank=True)
    prometheus_rules = models.JSONField(default=dict, blank=True)
    manifest_digest = models.CharField(max_length=71, db_index=True)
    reviewed_by_id = models.IntegerField(null=True, blank=True)
    reviewed_by_email = models.EmailField()
    reviewed_at = models.DateTimeField(default=timezone.now)
    is_current = models.BooleanField(default=True, db_index=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_verification_requirement_manifest'
        ordering = ['-requirement_generation']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'requirement_generation'],
                name='tasks_vrm_item_generation_uniq',
            ),
            models.UniqueConstraint(
                fields=['item'], condition=Q(is_current=True),
                name='tasks_vrm_one_current_per_item',
            ),
            models.CheckConstraint(
                check=Q(requirement_generation__gte=1),
                name='tasks_vrm_generation_gte_1',
            ),
        ]
        indexes = [
            models.Index(fields=['item', 'is_current'], name='tasks_vrm_item_current_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Verification requirement manifests are immutable; create a new generation.')
        return super().save(*args, **kwargs)


class DeploymentEvidenceCursor(models.Model):
    """Current evidence generation for one immutable requirement generation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey('Item', on_delete=models.CASCADE, related_name='deployment_evidence_cursors')
    requirement_generation = models.PositiveIntegerField()
    current_evidence_generation = models.PositiveIntegerField(default=1)
    invalidation_reason = models.CharField(max_length=120, blank=True, default='')
    invalidated_by_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_deployment_evidence_cursor'
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'requirement_generation'],
                name='tasks_dec_item_requirement_uniq',
            ),
            models.CheckConstraint(
                check=Q(requirement_generation__gte=1),
                name='tasks_dec_requirement_gte_1',
            ),
            models.CheckConstraint(
                check=Q(current_evidence_generation__gte=1),
                name='tasks_dec_evidence_gte_1',
            ),
        ]


class DeploymentEvidence(models.Model):
    """Immutable, producer-authored evidence snapshot for one gate decision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey('Item', on_delete=models.CASCADE, related_name='deployment_evidence')
    requirement_generation = models.PositiveIntegerField()
    evidence_generation = models.PositiveIntegerField()
    merge_commit = models.CharField(max_length=64, db_index=True)
    artifact_digest = models.CharField(max_length=255)
    config_digest = models.CharField(max_length=255)
    build_provenance = models.JSONField(default=dict)
    environment = models.CharField(max_length=80)
    service = models.CharField(max_length=160)
    cluster = models.CharField(max_length=120)
    namespace = models.CharField(max_length=160)
    workload = models.CharField(max_length=255)
    workload_uid = models.CharField(max_length=255, blank=True, default='')
    rollout_revision = models.CharField(max_length=255, blank=True, default='')
    workload_generation = models.PositiveBigIntegerField()
    observed_generation = models.PositiveBigIntegerField()
    ready = models.BooleanField(default=False)
    ready_at = models.DateTimeField()
    target_health = models.JSONField(default=dict, blank=True)
    metric_series = models.JSONField(default=dict, blank=True)
    prometheus_rules = models.JSONField(default=dict, blank=True)
    producer_id = models.IntegerField(null=True, blank=True)
    producer_identity = models.EmailField()
    evidence_digest = models.CharField(max_length=71, db_index=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_deployment_evidence'
        ordering = ['-requirement_generation', '-evidence_generation']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'requirement_generation', 'evidence_generation'],
                name='tasks_de_item_requirement_evidence_uniq',
            ),
            models.CheckConstraint(
                check=Q(requirement_generation__gte=1),
                name='tasks_de_requirement_gte_1',
            ),
            models.CheckConstraint(
                check=Q(evidence_generation__gte=1),
                name='tasks_de_evidence_gte_1',
            ),
        ]
        indexes = [
            models.Index(
                fields=['item', 'requirement_generation', 'evidence_generation'],
                name='tasks_de_gate_lookup_idx',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Deployment evidence snapshots are immutable; invalidate and create a new generation.')
        return super().save(*args, **kwargs)


class DeploymentEvidenceGateVerdict(models.Model):
    """Idempotent gate verdict bound to one immutable manifest/evidence tuple."""

    OUTCOME_CHOICES = [('pass', 'Pass'), ('hold', 'Hold')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey('Item', on_delete=models.CASCADE, related_name='deployment_gate_verdicts')
    requirement_generation = models.PositiveIntegerField()
    evidence_generation = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=8, choices=OUTCOME_CHOICES)
    reason = models.CharField(max_length=80, blank=True, default='')
    manifest_digest = models.CharField(max_length=71, blank=True, default='')
    evidence_digest = models.CharField(max_length=71, blank=True, default='')
    transition_id = models.UUIDField(null=True, blank=True)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_deployment_evidence_gate_verdict'
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'requirement_generation', 'evidence_generation'],
                name='tasks_degv_idempotency_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['item', 'created_at'], name='tasks_degv_item_created_idx'),
        ]


class DeploymentEvidenceGateOutbox(models.Model):
    """Transactional outbox entry emitted with each deployed-evidence verdict."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verdict = models.OneToOneField(
        DeploymentEvidenceGateVerdict, on_delete=models.CASCADE, related_name='outbox',
    )
    event_type = models.CharField(max_length=80, default='security_finding.deployment_gate_decided')
    payload = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    in_flight = models.BooleanField(default=False, db_index=True)
    in_flight_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_deployment_evidence_gate_outbox'
        ordering = ['created_at']


class ApprovalRule(models.Model):
    """
    Rules that auto-approve/reject actions based on conditions.
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Matching
    item_types = models.JSONField(default=list, help_text='e.g., ["action.hedging.*"]')
    sources = models.JSONField(default=list, blank=True, help_text="Source whitelist")
    conditions = models.JSONField(default=dict, help_text="JSON Logic format conditions")

    # Behavior
    auto_approve = models.BooleanField(default=False)
    auto_reject = models.BooleanField(default=False)
    priority = models.IntegerField(default=0, help_text="Higher priority rules are evaluated first")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        action = "auto-approve" if self.auto_approve else "auto-reject" if self.auto_reject else "manual"
        return f"{self.name} ({action})"

    class Meta:
        ordering = ['-priority', 'name']


class SilenceRule(models.Model):
    """
    Suppresses notifications for matching items.
    Items are still created and tracked, just not notified.
    Works across all modes (task, action, alert).
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Matching - flexible matchers
    modes = models.JSONField(default=list, blank=True, help_text='["task", "action", "alert"] or empty for all')
    item_types = models.JSONField(default=list, blank=True, help_text='["alert.warning", "task.reminder"]')
    sources = models.JSONField(default=list, blank=True)
    severities = models.JSONField(default=list, blank=True, help_text='["info", "warning"]')
    label_matchers = models.JSONField(default=dict, blank=True, help_text='{"host": "web-01", "env": "prod"}')
    conditions = models.JSONField(null=True, blank=True, help_text="JSON Logic for complex matching")

    # Time bounds
    starts_at = models.DateTimeField(null=True, blank=True, help_text="null = immediate")
    ends_at = models.DateTimeField(null=True, blank=True, help_text="null = indefinite")

    # Recurrence (e.g., silence every weekend)
    is_recurring = models.BooleanField(default=False)
    recurrence_pattern = models.JSONField(null=True, blank=True, help_text="cron-like or rrule")

    # Metadata
    created_by = models.CharField(max_length=255, null=True, blank=True)
    reason = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Stats
    match_count = models.IntegerField(default=0)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} (active={self.is_active})"

    def is_currently_active(self):
        """Check if silence rule is currently in effect"""
        if not self.is_active:
            return False
        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    class Meta:
        ordering = ['-created_at']


class EscalationRule(models.Model):
    """
    Defines escalation behavior when items aren't handled in time.
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Matching
    modes = models.JSONField(default=list, blank=True)
    item_types = models.JSONField(default=list, blank=True)
    sources = models.JSONField(default=list, blank=True)
    min_severity = models.CharField(max_length=10, choices=Item.SEVERITY_CHOICES, null=True, blank=True)
    label_matchers = models.JSONField(default=dict, blank=True)
    conditions = models.JSONField(null=True, blank=True)

    # Escalation stages (list of escalation steps)
    # Each stage defines what happens after a delay
    stages = models.JSONField(default=list, help_text="""
    Example:
    [
      {
        "delay_minutes": 5,
        "actions": [
          {"type": "bump_severity", "to": "error"},
          {"type": "add_channels", "channels": ["telegram"]}
        ]
      },
      {
        "delay_minutes": 15,
        "actions": [
          {"type": "bump_severity", "to": "critical"},
          {"type": "add_channels", "channels": ["phone_call"]}
        ]
      }
    ]
    """)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({len(self.stages)} stages)"

    class Meta:
        ordering = ['name']


class InhibitionRule(models.Model):
    """
    Suppresses target alerts when source alerts are firing.
    Useful for reducing noise during cascading failures.
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Source alert matcher (the "parent" alert that inhibits others)
    source_item_types = models.JSONField(default=list, blank=True)
    source_severities = models.JSONField(default=list, blank=True)
    source_label_matchers = models.JSONField(default=dict, blank=True)

    # Target alert matcher (alerts to suppress when source is firing)
    target_item_types = models.JSONField(default=list, blank=True)
    target_severities = models.JSONField(default=list, blank=True)
    target_label_matchers = models.JSONField(default=dict, blank=True)

    # Labels that must match between source and target
    # e.g., ["host"] means source.labels.host must equal target.labels.host
    equal_labels = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} (active={self.is_active})"

    class Meta:
        ordering = ['name']


class Subscription(models.Model):
    """
    Subscribe to item events for notifications.
    """
    NOTIFY_TYPE_CHOICES = [
        ('webhook', 'Webhook'),
        ('redis', 'Redis Pub/Sub'),
        ('shizuha_notify', 'Shizuha Notify'),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # What to subscribe to
    modes = models.JSONField(default=list, blank=True, help_text='["task", "action", "alert"] or empty for all')
    item_types = models.JSONField(default=list, blank=True)
    statuses = models.JSONField(default=list, blank=True, help_text="Trigger on these status changes")
    severities = models.JSONField(default=list, blank=True, help_text="For alerts: minimum severity")
    label_matchers = models.JSONField(default=dict, blank=True)

    # How to notify
    notify_type = models.CharField(max_length=20, choices=NOTIFY_TYPE_CHOICES, default='shizuha_notify')

    # Webhook config
    webhook_url = models.URLField(null=True, blank=True)
    webhook_headers = models.JSONField(default=dict, blank=True)

    # Redis config
    redis_channel = models.CharField(max_length=255, null=True, blank=True)

    # shizuha-notify config
    notify_channels = models.JSONField(default=list, blank=True, help_text='["telegram", "email"]')
    notify_template = models.CharField(max_length=100, null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.notify_type})"

    class Meta:
        ordering = ['name']


class FingerprintConfig(models.Model):
    """
    Configuration for how to compute fingerprints for alerts from different sources.
    """
    source = models.CharField(max_length=100, unique=True, help_text="Source identifier")
    label_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Label fields to include in fingerprint. Empty = use all labels."
    )
    payload_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Payload fields to include in fingerprint."
    )
    description = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Fingerprint config for {self.source}"

    class Meta:
        ordering = ['source']


class UserNotificationChannel(models.Model):
    """
    User-specific notification channel configurations.
    Each user can have multiple notification channels (Discord, Telegram, etc.)
    """
    CHANNEL_TYPE_CHOICES = [
        ('discord', 'Discord Webhook'),
        ('telegram', 'Telegram Bot'),
    ]

    # User identification
    user_id = models.IntegerField(db_index=True, help_text="User ID from auth service")
    user_email = models.EmailField(null=True, blank=True, help_text="User email for reference")

    # Channel configuration
    name = models.CharField(max_length=100, help_text="Friendly name for this channel")
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPE_CHOICES)
    is_active = models.BooleanField(default=True)

    # Discord-specific config
    discord_webhook_url = models.URLField(null=True, blank=True, help_text="Discord webhook URL")

    # Telegram-specific config
    telegram_bot_token = models.CharField(max_length=100, null=True, blank=True, help_text="Telegram bot token")
    telegram_chat_id = models.CharField(max_length=100, null=True, blank=True, help_text="Telegram chat/user ID")

    # Notification preferences - which events to notify
    notify_on_reminder = models.BooleanField(default=True, help_text="Notify on task reminders")
    notify_on_overdue = models.BooleanField(default=True, help_text="Notify when tasks become overdue")
    notify_on_alert = models.BooleanField(default=True, help_text="Notify on new alerts")
    notify_on_action = models.BooleanField(default=True, help_text="Notify on pending actions")

    # Severity filter for alerts
    min_alert_severity = models.CharField(
        max_length=10,
        choices=Item.SEVERITY_CHOICES,
        default='warning',
        help_text="Minimum severity for alert notifications"
    )

    # Quiet hours (optional)
    quiet_hours_start = models.TimeField(null=True, blank=True, help_text="Start of quiet hours (no notifications)")
    quiet_hours_end = models.TimeField(null=True, blank=True, help_text="End of quiet hours")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_notification_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, help_text="Last error message if notification failed")
    error_count = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.channel_type}) - User {self.user_id}"

    def is_within_quiet_hours(self):
        """Check if current time is within quiet hours"""
        if not self.quiet_hours_start or not self.quiet_hours_end:
            return False
        now = timezone.now().time()
        if self.quiet_hours_start <= self.quiet_hours_end:
            return self.quiet_hours_start <= now <= self.quiet_hours_end
        else:
            # Quiet hours span midnight
            return now >= self.quiet_hours_start or now <= self.quiet_hours_end

    def should_notify_for_severity(self, severity):
        """Check if notification should be sent based on severity"""
        severity_levels = {'info': 0, 'warning': 1, 'error': 2, 'critical': 3}
        min_level = severity_levels.get(self.min_alert_severity, 1)
        current_level = severity_levels.get(severity, 0)
        return current_level >= min_level

    class Meta:
        ordering = ['user_id', 'name']
        indexes = [
            models.Index(fields=['user_id', 'is_active']),
            models.Index(fields=['channel_type']),
        ]


class NotificationDelegate(models.Model):
    """Delegation rule: notifications for ``user_id`` are also copied to
    ``delegate_id`` (optionally only for the event types in ``delegated_events``).

    PLS-228: this table (``tasks_notification_delegate``) was created out-of-band
    in prod and long had NO Django model/migration, so ``_get_delegates()`` ran
    raw SQL against it — a fresh DB (CI test DBs, new environments) lacked the
    table, the query raised ProgrammingError, aborted the enclosing transaction,
    and (via the error path) OOM-killed the PLAT-1219 CI gate. This model + its
    create-if-absent migration make the table part of the schema so it exists
    everywhere; the field types mirror the raw-SQL contract (``delegated_events``
    is a JSON *string*).
    """
    user_id = models.IntegerField(db_index=True, help_text="Delegating user ID")
    delegate_id = models.IntegerField(help_text="User ID that receives the copy")
    delegate_email = models.CharField(max_length=254, blank=True, default='')
    # JSON string of event types this delegation applies to; empty/blank = all.
    # Kept as text to match the existing prod column and the raw-SQL parse contract.
    delegated_events = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'tasks_notification_delegate'
        indexes = [models.Index(fields=['user_id', 'is_active'],
                                name='tasks_notif_user_id_active_idx')]

    def __str__(self):
        return f'delegate {self.user_id} -> {self.delegate_id}'


class Notification(models.Model):
    """
    Persisted in-app notification log.
    Created whenever a notification is dispatched (push, Discord, Telegram, etc.)
    so users can review past notifications inside the app.
    """
    NOTIFICATION_TYPES = [
        ('reminder', 'Task Reminder'),
        ('overdue', 'Task Overdue'),
        ('alert', 'Alert'),
        ('action', 'Action Pending'),
        ('assignment', 'Assignment'),
        ('comment', 'Comment'),
        ('status_change', 'Status Change'),
        ('system', 'System'),
    ]

    user_id = models.IntegerField(db_index=True, help_text="Recipient user ID")
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
        help_text="Related item (if any)"
    )
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)
    title = models.CharField(max_length=300)
    message = models.TextField(blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delegated_from_id = models.IntegerField(null=True, blank=True, help_text="User ID this notification was delegated from")
    delegated_from_email = models.EmailField(null=True, blank=True)

    def __str__(self):
        return f"{self.notification_type}: {self.title} (user {self.user_id})"

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', '-created_at']),
            models.Index(fields=['user_id', 'is_read', '-created_at']),
        ]


class Comment(models.Model):
    """
    Comments on items (tasks, actions, alerts).
    Supports threaded discussions through parent field.
    """
    ORIGIN_USER_AUTHORED = 'USER_AUTHORED'
    ORIGIN_WORKFLOW_LIFECYCLE = 'WORKFLOW_LIFECYCLE'
    ORIGIN_VALIDATOR_AUDIT = 'VALIDATOR_AUDIT'
    ORIGIN_RESOLVER_AUDIT = 'RESOLVER_AUDIT'
    ORIGIN_LEGACY_UNTYPED = 'LEGACY_UNTYPED'
    # PLS-722: the body of a comment the queue focus guard refused. The guard
    # still refuses (the caller gets its 400 and no work is started), but the
    # content is preserved instead of destroyed — a completed diagnosis with
    # nowhere to go is how a stall becomes undocumented.
    ORIGIN_FOCUS_REFUSED = 'FOCUS_REFUSED'
    ORIGIN_KIND_CHOICES = [
        (ORIGIN_USER_AUTHORED, 'User authored'),
        (ORIGIN_WORKFLOW_LIFECYCLE, 'Workflow lifecycle'),
        (ORIGIN_VALIDATOR_AUDIT, 'Validator audit'),
        (ORIGIN_RESOLVER_AUDIT, 'Resolver audit'),
        (ORIGIN_LEGACY_UNTYPED, 'Legacy untyped'),
        (ORIGIN_FOCUS_REFUSED, 'Focus-guard refused'),
    ]
    # PLS-1294: the substantive classing (PLS-518 is_system=False). Every
    # unread/attention count surface must exclude system lifecycle/audit
    # chatter through this one constant — never a locally re-derived filter.
    SUBSTANTIVE_ORIGIN_KINDS = (ORIGIN_USER_AUTHORED, ORIGIN_LEGACY_UNTYPED)

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='comments',
        help_text="The item this comment belongs to"
    )

    # Author information (stored for display even if user is deleted)
    author_id = models.IntegerField(
        db_index=True,
        help_text="User ID from auth service"
    )
    author_email = models.EmailField(help_text="Author's email for display")
    author_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Author's display name"
    )

    # Comment content
    content = models.TextField(help_text="Comment text content")

    # PLS-478: semantic provenance is distinct from the authenticated actor who
    # caused a workflow event.  Existing/direct rows stay fail-closed as legacy;
    # only the closed MechanicalCommentFactory may mint typed audit rows.
    origin_kind = models.CharField(
        max_length=32,
        choices=ORIGIN_KIND_CHOICES,
        default=ORIGIN_LEGACY_UNTYPED,
        db_index=True,
    )
    origin_event = models.CharField(max_length=96, blank=True, default='')
    template_version = models.PositiveSmallIntegerField(null=True, blank=True)
    origin_event_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        help_text="Stable durable-domain event identity; reused on retry/replay",
    )
    origin_payload = models.JSONField(default=dict, blank=True)
    caused_by_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    caused_by_email_snapshot = models.EmailField(blank=True, default='')

    # Threading support
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='replies',
        help_text="Parent comment for threaded replies"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_edited = models.BooleanField(default=False, help_text="True if comment was edited after creation")

    def __str__(self):
        return f"Comment by {self.author_email} on Item #{self.item_id}"

    def save(self, *args, **kwargs):
        # Mechanical bodies and provenance are immutable after insertion.  A
        # repair may use one audited, exact-row QuerySet.update command; ordinary
        # model/API edits must never restamp or rewrite a workflow audit.
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                'content', 'origin_kind', 'origin_event', 'template_version',
                'origin_event_id', 'origin_payload', 'caused_by_user_id',
                'caused_by_email_snapshot',
            ).first()
            if original:
                provenance_fields = (
                    'origin_kind', 'origin_event', 'template_version',
                    'origin_event_id', 'origin_payload', 'caused_by_user_id',
                    'caused_by_email_snapshot',
                )
                if any(getattr(self, field) != original[field] for field in provenance_fields):
                    from django.core.exceptions import ValidationError
                    raise ValidationError('Comment provenance is immutable after insert.')
                if (
                    original['origin_kind'] not in (
                        self.ORIGIN_USER_AUTHORED,
                        self.ORIGIN_LEGACY_UNTYPED,
                    )
                    and self.content != original['content']
                ):
                    from django.core.exceptions import ValidationError
                    raise ValidationError('Mechanical comment content is immutable after insert.')
            self.is_edited = True
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'tasks_comment'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['item', 'created_at']),
            models.Index(fields=['author_id']),
            # PLS-705: #135 narrowed the comment match to a `pk__in` subquery,
            # but that subquery is still an unindexed ILIKE over this table and
            # would re-cap the whole search at the comment scan.
            GinIndex(fields=['content'], opclasses=['gin_trgm_ops'],
                     name='pls705_comment_content_trgm'),
        ]


class OperatorWaitDigestState(models.Model):
    """High-water cursor for the canonical blocked-on-operator digest.

    A deleted/withdrawn newest digest must not make an older acknowledgement
    authoritative again.  Keeping that high-water independently from comments
    makes withdrawal durable across reconciliation sweeps and process restarts.
    """

    STATE_CHOICES = [('active', 'Active'), ('withdrawn', 'Withdrawn')]

    digest_item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name='operator_wait_digest_state',
    )
    source_comment = models.ForeignKey(
        Comment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    source_comment_order = models.PositiveBigIntegerField(default=0)
    source_revision = models.CharField(max_length=64, blank=True, default='')
    source_message_id = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default='active')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_operator_wait_digest_state'


class OperatorWaitMarker(models.Model):
    """Durable non-runnable classification for one operator-only blocker root."""

    STATE_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name='operator_wait_marker',
    )
    surfaced_item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='surfaced_operator_wait_markers',
    )
    digest_item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='digest_operator_wait_markers',
    )
    digest_comment = models.ForeignKey(
        Comment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='operator_wait_markers',
    )
    source_revision = models.CharField(max_length=64)
    source_message_id = models.CharField(max_length=64)
    lineage_item_ids = models.JSONField(default=list)
    deadline_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default='active')
    generation = models.PositiveIntegerField(default=1)
    activated_at = models.DateTimeField(auto_now_add=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    invalidated_reason = models.CharField(max_length=80, blank=True, default='')
    # PLS-527: evidence fingerprint lives on the same operator_wait authority.
    # First complete probe records checklist+fingerprint; unchanged probes are
    # no-ops; a changed fingerprint is the sole evidence-event re-arm.
    evidence_fingerprint = models.CharField(max_length=128, blank=True, default='')
    evidence_source = models.CharField(max_length=64, blank=True, default='')
    evidence_checklist = models.JSONField(default=dict, blank=True)
    evidence_recorded_at = models.DateTimeField(null=True, blank=True)
    evidence_unchanged_probes = models.PositiveIntegerField(default=0)
    verification_queued_generation = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_operator_wait_marker'
        indexes = [
            models.Index(
                fields=['state', 'deadline_at'],
                name='tasks_opera_state_9a160c_idx',
            ),
            models.Index(
                fields=['digest_item', 'state'],
                name='tasks_opera_digest__0e7df2_idx',
            ),
        ]


class WebhookKey(models.Model):
    """
    Webhook API keys for external integrations like Grafana.

    Routing model follows the PagerDuty/Opsgenie pattern: alerts are scoped
    by project (and optionally an assignment_group/team) at ingest, and are
    left unassigned. Individuals claim an alert by acknowledging / taking
    it — we do NOT pin every firing alert to whoever owns the integration.
    """
    INTEGRATION_CHOICES = [
        ('grafana', 'Grafana'),
        ('prometheus', 'Prometheus Alertmanager'),
        ('generic', 'Generic Webhook'),
    ]

    # Owner of the integration (who created/manages the key).
    # NOTE: this user is NOT auto-assigned as the alert's assignee.
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID of the key owner (manages the integration; NOT the alert assignee)"
    )
    user_email = models.EmailField(
        null=True, blank=True,
        help_text="Email of the key owner (for reference only)"
    )

    # Alert routing — industry-standard: route to a team/service bucket
    # rather than assigning each alert to an individual.
    default_project = models.ForeignKey(
        'Project',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='webhook_keys',
        help_text="If set, alerts from this webhook land in this project (team bucket)."
    )
    default_assignment_group = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Optional ServiceNow-style team/group tag applied to alerts (e.g., 'SRE', 'Payments')."
    )

    # Key identification
    name = models.CharField(
        max_length=100,
        help_text="Friendly name for this webhook key (e.g., 'Production Grafana')"
    )
    key = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="The API key used in webhook URL"
    )

    # Integration type
    integration_type = models.CharField(
        max_length=20,
        choices=INTEGRATION_CHOICES,
        default='grafana',
        help_text="Type of integration for payload parsing"
    )

    # Configuration
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive keys will reject incoming webhooks"
    )

    # Optional filtering
    default_severity = models.CharField(
        max_length=20,
        choices=[
            ('info', 'Info'),
            ('warning', 'Warning'),
            ('error', 'Error'),
            ('critical', 'Critical'),
        ],
        default='warning',
        help_text="Default severity if not provided in webhook payload"
    )

    source_name = models.CharField(
        max_length=100,
        default='grafana',
        help_text="Source name to use for alerts (e.g., 'grafana-prod', 'prometheus-staging')"
    )

    # Labels to automatically add to all alerts from this key
    auto_labels = models.JSONField(
        default=dict,
        blank=True,
        help_text="Labels automatically added to all alerts from this webhook"
    )

    # Stats
    alerts_received = models.IntegerField(
        default=0,
        help_text="Total number of alerts received via this key"
    )
    last_used_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Last time this key received an alert"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.integration_type}) - User {self.user_id}"

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = self.generate_key()
        super().save(*args, **kwargs)

    @staticmethod
    def generate_key():
        """Generate a secure random API key"""
        import secrets
        return secrets.token_urlsafe(32)

    def record_usage(self):
        """Record that this key was used"""
        from django.db.models import F
        WebhookKey.objects.filter(pk=self.pk).update(
            alerts_received=F('alerts_received') + 1,
            last_used_at=timezone.now()
        )

    class Meta:
        db_table = 'tasks_webhook_key'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', 'is_active']),
            models.Index(fields=['key']),
            models.Index(fields=['integration_type']),
        ]
        verbose_name = 'Webhook Key'
        verbose_name_plural = 'Webhook Keys'


class ExternalMcpClient(models.Model):
    """Tenant-scoped external MCP client registration for Pulse read APIs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    owner_user_id = models.IntegerField(null=True, blank=True)
    owner_email = models.EmailField(blank=True)
    allowed_project_ids = models.JSONField(default=list, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    rate_limit_policy = models.JSONField(default=dict, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} (org={self.org_id})"

    class Meta:
        db_table = 'tasks_external_mcp_client'
        indexes = [
            models.Index(fields=['org_id', 'disabled_at'], name='tasks_exter_org_id_5c5d9d_idx'),
        ]


class ExternalMcpToken(models.Model):
    """Hashed bearer token for an ExternalMcpClient."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        ExternalMcpClient,
        on_delete=models.CASCADE,
        related_name='tokens',
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(str(raw_token).encode('utf-8')).hexdigest()

    def is_active(self):
        now = timezone.now()
        return (
            self.revoked_at is None
            and self.client.disabled_at is None
            and (self.expires_at is None or self.expires_at > now)
        )

    class Meta:
        db_table = 'tasks_external_mcp_token'
        indexes = [
            models.Index(fields=['client', 'revoked_at'], name='tasks_exter_client__6ca132_idx'),
            models.Index(fields=['expires_at'], name='tasks_exter_expires_7cc747_idx'),
        ]


class ExternalMcpCallAudit(models.Model):
    """Append-only audit row for every external MCP discovery/call attempt."""

    RESULT_CHOICES = [
        ('success', 'Success'),
        ('denied', 'Denied'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org_id = models.IntegerField(null=True, blank=True, db_index=True)
    client = models.ForeignKey(
        ExternalMcpClient,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_entries',
    )
    actor_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    tool_name = models.CharField(max_length=100, db_index=True)
    task_id = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=255, blank=True)
    request_hash = models.CharField(max_length=64, blank=True)
    result_status = models.CharField(max_length=20, choices=RESULT_CHOICES)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_external_mcp_call_audit'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'created_at'], name='tasks_exter_client__352bb2_idx'),
            models.Index(fields=['org_id', 'tool_name'], name='tasks_exter_org_id_caf465_idx'),
        ]


# =============================================================================
# Attachment Model (MinIO Storage)
# =============================================================================

class Attachment(models.Model):
    """
    File attachments for items, stored in MinIO.
    Supports images with thumbnail generation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='attachments',
        help_text="The item this attachment belongs to"
    )

    # MinIO storage
    storage_key = models.CharField(
        max_length=500,
        help_text="MinIO object key (path in bucket)"
    )
    filename = models.CharField(
        max_length=255,
        help_text="Original filename"
    )
    file_type = models.CharField(
        max_length=100,
        help_text="MIME type of the file"
    )
    file_size = models.PositiveIntegerField(
        help_text="File size in bytes"
    )

    # Image metadata
    is_image = models.BooleanField(
        default=False,
        help_text="True if file is an image"
    )
    width = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Image width in pixels"
    )
    height = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Image height in pixels"
    )
    thumbnail_key = models.CharField(
        max_length=500,
        blank=True,
        help_text="MinIO key for thumbnail (images only)"
    )

    # Source tracking
    SOURCE_DIRECT = 'direct'
    SOURCE_COMMENT = 'comment'
    SOURCE_CHOICES = [
        (SOURCE_DIRECT, 'Direct upload'),
        (SOURCE_COMMENT, 'Comment image'),
    ]
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_DIRECT,
        help_text="How the attachment was uploaded"
    )

    # Uploader
    uploaded_by_id = models.IntegerField(
        db_index=True,
        help_text="User ID who uploaded the file"
    )
    uploaded_by_email = models.EmailField(
        help_text="Email of uploader"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.filename} on Item #{self.item_id}"

    class Meta:
        db_table = 'tasks_attachment'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['item', 'created_at']),
            models.Index(fields=['uploaded_by_id']),
            models.Index(fields=['is_image']),
        ]


# =============================================================================
# Item Activity Model (Audit Log)
# =============================================================================

class ItemActivity(models.Model):
    """
    Activity/audit log for items. Tracks all changes for history display.
    """
    import uuid

    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('status_changed', 'Status Changed'),
        ('assigned', 'Assigned'),
        ('unassigned', 'Unassigned'),
        ('priority_changed', 'Priority Changed'),
        ('moved', 'Moved to Project'),
        ('commented', 'Commented'),
        ('attachment_added', 'Attachment Added'),
        ('attachment_removed', 'Attachment Removed'),
        ('linked', 'Linked'),
        ('unlinked', 'Unlinked'),
        ('watched', 'Started Watching'),
        ('unwatched', 'Stopped Watching'),
        ('completed', 'Completed'),
        ('reopened', 'Reopened'),
        ('recurring_cycle_fired', 'Recurring Cycle Fired'),
        ('recurring_cycle_skipped', 'Recurring Cycle Skipped (still active)'),
        ('recurring_cycle_stale', 'Recurring Cycle Stale'),
        ('recurring_cycle_auto_close_failed', 'Recurring Cycle Auto-Close Failed'),
        ('pr_attached', 'Pull Request Attached'),
        ('pr_removed', 'Pull Request Removed'),
        ('pr_status_changed', 'Pull Request Status Changed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='activities',
        help_text="The item this activity belongs to"
    )
    action = models.CharField(
        max_length=64,
        choices=ACTION_CHOICES,
        help_text="Type of activity"
    )
    field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Field that was changed (for updates)"
    )
    old_value = models.TextField(
        blank=True,
        help_text="Previous value (for updates)"
    )
    new_value = models.TextField(
        blank=True,
        help_text="New value (for updates)"
    )

    # Actor
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID who performed the action"
    )
    user_email = models.EmailField(
        help_text="Email of the actor"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.action} on Item #{self.item_id} by {self.user_email}"

    class Meta:
        db_table = 'tasks_item_activity'
        ordering = ['-created_at']
        indexes = [
            # Home's bounded window read must not scan recent comments and
            # unrelated audit fields for every touched item. Keep the exact
            # status predicate/time leading edge available to the planner.
            models.Index(
                fields=['created_at', 'item', 'id'],
                condition=models.Q(field_name='Status'),
                name='item_status_time_item_idx',
            ),
            models.Index(fields=['item', 'created_at']),
            models.Index(fields=['user_id']),
            models.Index(fields=['action']),
            # PLS-1244: lane-health `_last_assignment` filters action='assigned'
            # and orders by -created_at per lane; the single-column action index
            # cannot serve the ordering, so the planner sorted the full
            # action-filtered set per lane (1.66s/lane cold, ~38s across 23
            # lanes). The composite (action, created_at) index lets the planner
            # walk the assigned subset newest-first and stop at the first join
            # match.
            models.Index(
                fields=['action', 'created_at'],
                name='tasks_item_action__created_idx',
            ),
        ]
        verbose_name_plural = 'Item Activities'


# =============================================================================
# PLS-620 — urgent user-harm / data-loss containment decisions
# =============================================================================

class ContainmentDecision(models.Model):
    """Current containment gate for one urgent risk finding.

    ``ContainmentDecisionEvent`` is the append-only audit trail; this row is the
    current, cheap-to-read projection used by serializers and transition gates.
    """

    STATE_PENDING = 'pending'
    STATE_APPLIED = 'applied'
    STATE_EXPLICITLY_NOT_REQUIRED = 'explicitly_not_required'
    STATE_CHOICES = [
        (STATE_PENDING, 'Pending'),
        (STATE_APPLIED, 'Applied'),
        (STATE_EXPLICITLY_NOT_REQUIRED, 'Explicitly not required'),
    ]

    item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name='containment_decision',
    )
    state = models.CharField(
        max_length=40,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        db_index=True,
    )
    owner_id = models.IntegerField(null=True, blank=True)
    owner_email = models.EmailField(blank=True, default='')
    owner_group = models.CharField(max_length=100, blank=True, default='')
    risk_markers = models.JSONField(default=list, blank=True)
    requirement_active = models.BooleanField(default=True)
    reason = models.TextField(blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    required_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_containment_decision'
        indexes = [
            models.Index(
                fields=['state', 'updated_at'],
                name='containment_state_updated_idx',
            ),
            models.Index(
                fields=['owner_group', 'state'],
                name='containment_group_state_idx',
            ),
        ]

    def __str__(self):
        return f'{self.item.item_key or self.item_id}: {self.state} v{self.version}'


class ContainmentDecisionEvent(models.Model):
    """Append-only versioned history for a containment decision."""

    decision = models.ForeignKey(
        ContainmentDecision,
        on_delete=models.CASCADE,
        related_name='events',
    )
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=40, choices=ContainmentDecision.STATE_CHOICES)
    owner_id = models.IntegerField(null=True, blank=True)
    owner_email = models.EmailField(blank=True, default='')
    owner_group = models.CharField(max_length=100, blank=True, default='')
    risk_markers = models.JSONField(default=list, blank=True)
    requirement_active = models.BooleanField(default=True)
    reason = models.TextField(blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    trigger = models.CharField(max_length=80, blank=True, default='')
    actor_id = models.IntegerField(null=True, blank=True)
    actor_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_containment_decision_event'
        ordering = ['version']
        constraints = [
            models.UniqueConstraint(
                fields=['decision', 'version'],
                name='unique_containment_decision_version',
            ),
        ]

    def __str__(self):
        return f'{self.decision_id}: {self.state} v{self.version}'


# =============================================================================
# AI Generation Run Model (PLS-307 citation-backed recaps)
# =============================================================================

class AiGenerationRun(models.Model):
    """Audit record for bounded, citation-backed AI recap generations.

    The row stores the exact project/sprint scope, source time window, source
    snapshot hash, prompt/model metadata, generated output, and later feedback so
    sprint/project recaps are reproducible and every claim can be traced back to
    Pulse evidence.
    """

    TYPE_CHOICES = [
        ('sprint', 'Sprint recap'),
        ('project', 'Project recap'),
        ('daily', 'Daily standup'),
        ('risk', 'Risk digest'),
    ]
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org_id = models.IntegerField(db_index=True, help_text='Organization ID from shizuha-admin')
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='ai_generation_runs',
        help_text='Project whose readable Pulse evidence was summarized.',
    )
    sprint = models.ForeignKey(
        Sprint,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='ai_generation_runs',
        help_text='Optional sprint scope for sprint/daily recaps.',
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='project', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    model = models.CharField(max_length=100, default='deterministic-pulse-recap-v1')
    prompt_template_version = models.CharField(max_length=100, default='pulse-recap-v1')
    source_window_start = models.DateTimeField(db_index=True)
    source_window_end = models.DateTimeField(db_index=True)
    source_snapshot_hash = models.CharField(max_length=64, blank=True)
    source_counts = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    citations = models.JSONField(default=list, blank=True)
    feedback = models.JSONField(default=list, blank=True)
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    client_id = models.CharField(max_length=100, blank=True, default='pulse-api')
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.project.key} {self.type} recap {self.id}"

    class Meta:
        db_table = 'tasks_ai_generation_run'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['org_id', 'project', 'created_at']),
            models.Index(fields=['project', 'type', 'status']),
            models.Index(fields=['created_by_email', 'created_at']),
        ]



# =============================================================================
# Item Link Model (Issue Relationships)
# =============================================================================

class OrganizationIsolationArchive(models.Model):
    """Audit snapshot for relationships removed by the tenancy migration."""

    kind = models.CharField(max_length=40, db_index=True)
    object_key = models.CharField(max_length=80, blank=True, default='', db_index=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'tasks_org_isolation_archive'
        ordering = ['id']


class ItemLink(models.Model):
    """
    Links between items (blocks, relates to, duplicates, etc.).
    Issue links between items (blocks, is blocked by, duplicates, etc.).
    """
    import uuid

    _DUPLICATE_GRAPH_LOCK_NAMESPACE = 0x50554C44

    LINK_TYPES = [
        ('blocks', 'Blocks'),
        ('is_blocked_by', 'Is Blocked By'),
        ('relates_to', 'Relates To'),
        ('duplicates', 'Duplicates'),
        ('is_duplicated_by', 'Is Duplicated By'),
        ('parent_of', 'Parent Of'),
        ('child_of', 'Child Of'),
        ('clones', 'Clones'),
        ('is_cloned_by', 'Is Cloned By'),
        # PLS-533: an epic-closeout assessment task points at the epic it assesses
        # via `assessment_of` (assessment --assessment_of--> epic). This is a
        # NON-SCOPE relation — it must never count as a decomposition child when
        # evaluating epic close-out (only parent_of/child_of + parent_item FK do).
        ('assessment_of', 'Assessment Of'),
        ('has_assessment', 'Has Assessment'),
    ]

    # Mapping of link types to their inverse
    INVERSE_TYPES = {
        'blocks': 'is_blocked_by',
        'is_blocked_by': 'blocks',
        'duplicates': 'is_duplicated_by',
        'is_duplicated_by': 'duplicates',
        'parent_of': 'child_of',
        'child_of': 'parent_of',
        'clones': 'is_cloned_by',
        'is_cloned_by': 'clones',
        'relates_to': 'relates_to',  # Symmetric
        'assessment_of': 'has_assessment',  # PLS-533 (non-scope)
        'has_assessment': 'assessment_of',
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='outward_links',
        help_text="Source item of the link"
    )
    target_item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='inward_links',
        help_text="Target item of the link"
    )
    link_type = models.CharField(
        max_length=30,
        choices=LINK_TYPES,
        help_text="Type of relationship"
    )

    # Creator
    created_by_id = models.IntegerField(
        db_index=True,
        help_text="User ID who created the link"
    )
    created_by_email = models.EmailField(
        help_text="Email of creator"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source_item.item_key or self.source_item_id} {self.link_type} {self.target_item.item_key or self.target_item_id}"

    @staticmethod
    def _item_organization_id(item):
        project = item._state.fields_cache.get('project')
        if project is not None:
            return project.org_id
        project_id = item.project_id
        if project_id is None:
            return None
        return Project.objects.only('org_id').get(pk=project_id).org_id

    def _validate_organization_isolation(self):
        if not self.source_item_id or not self.target_item_id:
            return
        source = self._state.fields_cache.get('source_item')
        if source is None:
            source = Item.objects.only('project_id').get(pk=self.source_item_id)
        target = self._state.fields_cache.get('target_item')
        if target is None:
            target = Item.objects.only('project_id').get(pk=self.target_item_id)
        source_org_id = self._item_organization_id(source)
        target_org_id = self._item_organization_id(target)
        # Preserve legacy project-less↔project-less relationships, but an
        # organization-owned task can only link to another task in that exact
        # organization.  This covers blockers, relates-to, clones, duplicates,
        # and every future link type through one model invariant.
        if source_org_id != target_org_id:
            raise ValidationError(
                'Linked tasks must belong to the same organization.'
            )

    def clean(self):
        super().clean()
        self._validate_organization_isolation()
        self._validate_duplicate_graph()

    def _validate_duplicate_graph(self, using=None):
        """A duplicate must lead to another task, never back to itself."""
        if self.link_type not in ('duplicates', 'is_duplicated_by'):
            return
        source_id, target_id = self.source_item_id, self.target_item_id
        if self.link_type == 'is_duplicated_by':
            source_id, target_id = target_id, source_id
        if not source_id or not target_id:
            return
        frontier = {target_id}
        visited = set()
        graph = {source_id: {target_id}}
        links = ItemLink.objects.using(using).exclude(pk=self.pk)
        while frontier:
            visited.update(frontier)
            edges = links.filter(
                Q(link_type='duplicates', source_item_id__in=frontier)
                | Q(link_type='is_duplicated_by', target_item_id__in=frontier)
            ).values_list('source_item_id', 'target_item_id', 'link_type')
            successors = set()
            for source, target, kind in edges:
                if kind == 'is_duplicated_by':
                    source, target = target, source
                graph.setdefault(source, set()).add(target)
                successors.add(target)
            frontier = successors - visited
        active = set()
        finished = set()
        stack = [(source_id, False)]
        while stack:
            node_id, exiting = stack.pop()
            if exiting:
                active.remove(node_id)
                finished.add(node_id)
            elif node_id in active:
                raise ValidationError(
                    'A duplicate target cannot create a circular subsumption chain. '
                    'Choose a surviving canonical task, not this task or its duplicates.',
                    code='duplicate_cycle',
                )
            elif node_id not in finished:
                active.add(node_id)
                stack.append((node_id, True))
                stack.extend((target, False) for target in graph.get(node_id, ()))

    def save(self, *args, **kwargs):
        # Call the structural invariant directly rather than full_clean(): link
        # creation has historically allowed system writers to omit optional
        # presentation metadata, and tenancy validation must not change that
        # compatibility contract.
        self._validate_organization_isolation()
        if self.link_type in ('duplicates', 'is_duplicated_by'):
            using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
            organization_id = self._item_organization_id(self.source_item)
            with transaction.atomic(using=using):
                with connections[using].cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_advisory_xact_lock(%s, %s)',
                        [self._DUPLICATE_GRAPH_LOCK_NAMESPACE, organization_id or 0],
                    )
                self._validate_duplicate_graph(using=using)
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    @classmethod
    def get_inverse_type(cls, link_type):
        """Get the inverse link type for bidirectional display."""
        return cls.INVERSE_TYPES.get(link_type, link_type)

    class Meta:
        db_table = 'tasks_item_link'
        ordering = ['-created_at']
        unique_together = ['source_item', 'target_item', 'link_type']
        indexes = [
            models.Index(fields=['source_item', 'link_type']),
            models.Index(fields=['target_item', 'link_type']),
            models.Index(fields=['created_by_id']),
        ]


# =============================================================================
# Item Watcher Model
# =============================================================================

class ItemWatcher(models.Model):
    """
    Users watching an item for notifications.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='watchers',
        help_text="The item being watched"
    )
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID of the watcher"
    )
    user_email = models.EmailField(
        help_text="Email of the watcher"
    )
    user_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Display name of the watcher"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user_email} watching Item #{self.item_id}"

    class Meta:
        db_table = 'tasks_item_watcher'
        ordering = ['-created_at']
        unique_together = ['item', 'user_id']
        indexes = [
            models.Index(fields=['item']),
            models.Index(fields=['user_id']),
        ]


# =============================================================================
# Comment Reaction Model
# =============================================================================

class CommentReaction(models.Model):
    """
    Emoji reactions on comments.
    """
    import uuid

    REACTION_CHOICES = [
        ('thumbs_up', '+1'),
        ('thumbs_down', '-1'),
        ('heart', 'Heart'),
        ('laugh', 'Laugh'),
        ('confused', 'Confused'),
        ('celebrate', 'Celebrate'),
        ('rocket', 'Rocket'),
        ('eyes', 'Eyes'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name='reactions',
        help_text="The comment this reaction is on"
    )
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID who reacted"
    )
    user_email = models.EmailField(
        help_text="Email of the reactor"
    )
    reaction = models.CharField(
        max_length=20,
        choices=REACTION_CHOICES,
        help_text="Type of reaction"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reaction} by {self.user_email} on Comment #{self.comment_id}"

    class Meta:
        db_table = 'tasks_comment_reaction'
        ordering = ['created_at']
        unique_together = ['comment', 'user_id', 'reaction']
        indexes = [
            models.Index(fields=['comment']),
            models.Index(fields=['user_id']),
            models.Index(fields=['reaction']),
        ]


# =============================================================================
# Component Model
# =============================================================================

class Component(models.Model):
    """
    Components represent functional areas within a project (e.g., Frontend, Backend, API).
    Items can be assigned to components for better organization and filtering.
    Components represent subsystems or areas of a project.
    """
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='components',
        help_text="Project this component belongs to"
    )
    name = models.CharField(max_length=255, help_text="Component name (e.g., Frontend, API)")
    description = models.TextField(blank=True, help_text="Component description")

    # Component lead (optional)
    lead_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="User ID of the component lead"
    )
    lead_email = models.EmailField(
        null=True,
        blank=True,
        help_text="Email of the component lead"
    )

    # Default assignee for items in this component
    default_assignee_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="Default user ID for items in this component"
    )
    default_assignee_email = models.EmailField(
        null=True,
        blank=True,
        help_text="Default email for items in this component"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key} - {self.name}"

    class Meta:
        db_table = 'tasks_component'
        ordering = ['project', 'name']
        unique_together = ['project', 'name']
        indexes = [
            models.Index(fields=['project']),
            models.Index(fields=['lead_id']),
        ]


# =============================================================================
# Version Model (Releases)
# =============================================================================

class Version(models.Model):
    """
    Versions represent releases or milestones within a project.
    Items can be assigned to versions (fix version, affects versions).
    Versions represent project releases for tracking fix/affected versions.
    """
    STATUS_CHOICES = [
        ('planning', 'Planning'),
        ('active', 'Active'),
        ('released', 'Released'),
        ('archived', 'Archived'),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='versions',
        help_text="Project this version belongs to"
    )
    name = models.CharField(max_length=255, help_text="Version name (e.g., 1.0.0, 2.0-beta)")
    description = models.TextField(blank=True, help_text="Version description")

    # Version status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='planning',
        help_text="Version status"
    )

    # Dates
    start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Planned start date"
    )
    release_date = models.DateField(
        null=True,
        blank=True,
        help_text="Planned or actual release date"
    )
    released_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Actual release timestamp"
    )

    # Ordering
    sequence = models.IntegerField(
        default=0,
        help_text="Order in version list (lower = earlier)"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key} - {self.name}"

    def release(self):
        """Mark version as released."""
        self.status = 'released'
        self.released_at = timezone.now()
        if not self.release_date:
            self.release_date = timezone.now().date()
        self.save()

    def get_item_count(self):
        """Get count of items targeting this version."""
        return self.fix_items.count()

    def get_completed_item_count(self):
        """Get count of completed items in this version."""
        return self.fix_items.filter(status='completed').count()

    def get_progress_percent(self):
        """Get completion percentage for this version."""
        total = self.get_item_count()
        if total == 0:
            return 0
        completed = self.get_completed_item_count()
        return round((completed / total) * 100)

    class Meta:
        db_table = 'tasks_version'
        ordering = ['project', 'sequence', 'name']
        unique_together = ['project', 'name']
        indexes = [
            models.Index(fields=['project', 'status']),
            models.Index(fields=['release_date']),
        ]


# =============================================================================
# WorkLog Model (Time Tracking)
# =============================================================================

class WorkLog(models.Model):
    """
    Work log entries for tracking time spent on items.
    Work logs track time spent on items.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='work_logs',
        help_text="Item this work log belongs to"
    )

    # Who logged the work
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID who logged the work"
    )
    user_email = models.EmailField(help_text="Email of user who logged the work")

    # Time tracking
    duration_minutes = models.PositiveIntegerField(
        help_text="Time spent in minutes"
    )
    worked_date = models.DateField(
        help_text="Date the work was performed"
    )

    # Description
    description = models.TextField(
        blank=True,
        help_text="Description of work performed"
    )

    # Remaining estimate at time of logging (optional)
    remaining_estimate_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="New remaining estimate after this work log"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.duration_minutes}m on Item #{self.item_id} by {self.user_email}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None or not WorkLog.objects.filter(pk=self.pk).exists()
        old_duration = 0
        if not is_new:
            old_log = WorkLog.objects.get(pk=self.pk)
            old_duration = old_log.duration_minutes

        super().save(*args, **kwargs)

        # Update item's time tracking fields
        if is_new:
            # Adding new work log
            self.item.time_spent_minutes = (self.item.time_spent_minutes or 0) + self.duration_minutes
        else:
            # Updating existing work log
            self.item.time_spent_minutes = (self.item.time_spent_minutes or 0) - old_duration + self.duration_minutes

        # Update remaining estimate if provided
        if self.remaining_estimate_minutes is not None:
            self.item.remaining_estimate_minutes = self.remaining_estimate_minutes

        self.item.save(update_fields=['time_spent_minutes', 'remaining_estimate_minutes', 'updated_at'])

    def delete(self, *args, **kwargs):
        # Subtract duration from item's time spent
        self.item.time_spent_minutes = max(0, (self.item.time_spent_minutes or 0) - self.duration_minutes)
        self.item.save(update_fields=['time_spent_minutes', 'updated_at'])
        super().delete(*args, **kwargs)

    class Meta:
        db_table = 'tasks_work_log'
        ordering = ['-worked_date', '-created_at']
        indexes = [
            models.Index(fields=['item', 'worked_date']),
            models.Index(fields=['user_id']),
        ]


# =============================================================================
# Global Status Model
# =============================================================================

class Status(models.Model):
    """
    Global status definition, shared across workflows.

    - Statuses are global — same status can appear in multiple workflows
    - Each status belongs to a status category (To Do / In Progress / Done)
    - Status category drives board behavior and resolution logic
    - Statuses are referenced by slug (e.g., 'open', 'in_progress', 'closed')
    """
    STATUS_CATEGORY_CHOICES = [
        # 'backlog' is owned-but-NOT-active work: a prioritized roadmap an owner
        # pulls FROM, not active WIP — like Jira/Linear "Backlog" (distinct from a
        # sprint-ready "To Do"). Excluded from the actionable/`is_active` queues
        # (and pulse_get_my_tasks), so a large standing backlog does NOT read as
        # "advance every item every heartbeat" (root of the aoi 26-in_progress
        # false-drain, 2026-06-23). Pull a backlog item into 'todo'/'in_progress'
        # to make it active. NOT terminal — fully resumable.
        ('backlog', 'Backlog'),
        ('todo', 'To Do'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        # 'scheduled' is the gap-state a recurring task sits in between
        # cycles. The timer owns it — no human action is expected. Excluded
        # from default "Open" filters; visible via the explicit Scheduled
        # pill. See docs/recurring-tasks.md and WorkflowService.execute_transition.
        ('scheduled', 'Scheduled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=100,
        help_text="Display name (e.g., 'In Progress', 'Code Review')"
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        help_text="Machine-readable identifier (e.g., 'in_progress', 'code_review')"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of what this status means"
    )
    category = models.CharField(
        max_length=20,
        choices=STATUS_CATEGORY_CHOICES,
        help_text="Status category determines board column grouping and resolution behavior"
    )
    color = models.CharField(
        max_length=7,
        default='#6b7280',
        help_text="Display color (hex)"
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        help_text="Icon identifier for UI rendering"
    )

    # Ordering for display
    position = models.IntegerField(
        default=0,
        help_text="Global sort order for status lists"
    )

    # System flag — built-in statuses can't be deleted
    is_system = models.BooleanField(
        default=False,
        help_text="System statuses cannot be deleted (Open, In Progress, Done, etc.)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} [{self.category}]"

    # PLS-623: stable natural key (slug) so a reference-data fixture round-trips
    # idempotently across databases (existing rows matched/updated, not duped).
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_status'
        ordering = ['position', 'name']
        verbose_name = 'Status'
        verbose_name_plural = 'Statuses'

    @staticmethod
    def get_default_statuses():
        """Return default global statuses."""
        return [
            # To Do category
            {'name': 'Open', 'slug': 'open', 'category': 'todo', 'color': '#6b7280', 'position': 0, 'is_system': True,
             'description': 'Item is created but work has not started'},
            {'name': 'To Do', 'slug': 'todo', 'category': 'todo', 'color': '#6b7280', 'position': 1, 'is_system': True,
             'description': 'Item is ready to be worked on'},
            {'name': 'Reopened', 'slug': 'reopened', 'category': 'todo', 'color': '#f59e0b', 'position': 2, 'is_system': True,
             'description': 'Item was closed but has been reopened'},
            {'name': 'Backlog', 'slug': 'backlog', 'category': 'backlog', 'color': '#9ca3af', 'position': 3, 'is_system': True,
             'description': 'Owned but not active — a prioritized roadmap to pull from, not WIP. Excluded from actionable queues; pull into To Do / In Progress to make it active.'},
            # In Progress category
            {'name': 'In Progress', 'slug': 'in_progress', 'category': 'in_progress', 'color': '#3b82f6', 'position': 10, 'is_system': True,
             'description': 'Work is actively being done'},
            {'name': 'In Review', 'slug': 'in_review', 'category': 'in_progress', 'color': '#8b5cf6', 'position': 11, 'is_system': False,
             'description': 'Work is complete and awaiting review'},
            {'name': 'In QA', 'slug': 'in_qa', 'category': 'in_progress', 'color': '#ec4899', 'position': 12, 'is_system': False,
             'description': 'Item is being tested by QA'},
            {'name': 'Blocked', 'slug': 'blocked', 'category': 'in_progress', 'color': '#ef4444', 'position': 13, 'is_system': False,
             'description': 'Work is blocked by an external dependency'},
            {'name': 'Awaiting Triage', 'slug': 'awaiting_triage', 'category': 'in_progress', 'color': '#f97316', 'position': 14, 'is_system': False,
             'description': 'Bug/issue awaiting triage by QA or lead'},
            {'name': 'Design', 'slug': 'design', 'category': 'in_progress', 'color': '#14b8a6', 'position': 15, 'is_system': False,
             'description': 'Item is in the design/architecture phase'},
            # Done category
            {'name': 'Done', 'slug': 'done', 'category': 'done', 'color': '#22c55e', 'position': 20, 'is_system': True,
             'description': 'Work is complete'},
            {'name': 'Closed', 'slug': 'closed', 'category': 'done', 'color': '#16a34a', 'position': 21, 'is_system': True,
             'description': 'Item is closed (with resolution)'},
            {'name': 'Cancelled', 'slug': 'cancelled', 'category': 'done', 'color': '#9ca3af', 'position': 22, 'is_system': False,
             'description': 'Item was cancelled without completion'},
            {'name': 'Deferred', 'slug': 'deferred', 'category': 'done', 'color': '#64748b', 'position': 24, 'is_system': False,
             'description': 'Item is intentionally parked by a human/operator decision; do not recreate without explicit direction'},

            # Legacy compatibility — map old Pulse statuses
            {'name': 'Pending', 'slug': 'pending', 'category': 'todo', 'color': '#6b7280', 'position': 4, 'is_system': True,
             'description': 'Legacy: equivalent to Open/To Do'},
            {'name': 'Completed', 'slug': 'completed', 'category': 'done', 'color': '#22c55e', 'position': 23, 'is_system': True,
             'description': 'Legacy: equivalent to Done'},
            # Scheduled — the gap-state for recurring tasks between cycles.
            # Auto-entered by execute_transition when is_recurring=True and
            # the target is Done-category; exited by fire_recurring_due at
            # next_fire_at which resets the item to its workflow's initial.
            {'name': 'Scheduled', 'slug': 'scheduled', 'category': 'scheduled', 'color': '#a855f7', 'position': 30, 'is_system': True,
             'description': 'Waiting for next recurrence — timer-owned, no human action expected'},
        ]


# =============================================================================
# Resolution Model
# =============================================================================

class Resolution(models.Model):
    """
    Resolution type for closed/done items.

    - Resolution is set when an item transitions to a 'done' category status
    - Resolution is cleared when item is reopened
    - Common resolutions: Fixed, Won't Fix, Duplicate, Cannot Reproduce, Incomplete
    - Resolution + Status together define the final state of an item
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=100,
        help_text="Display name (e.g., 'Fixed', 'Won't Fix')"
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        help_text="Machine-readable identifier (e.g., 'fixed', 'wont_fix')"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of when this resolution applies"
    )
    position = models.IntegerField(
        default=0,
        help_text="Sort order in resolution picker"
    )
    is_system = models.BooleanField(
        default=False,
        help_text="System resolutions cannot be deleted"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    # PLS-623: stable natural key (slug) for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_resolution'
        ordering = ['position', 'name']
        verbose_name = 'Resolution'
        verbose_name_plural = 'Resolutions'

    @staticmethod
    def get_default_resolutions():
        """Return default resolutions."""
        return [
            {'name': 'Fixed', 'slug': 'fixed', 'position': 0, 'is_system': True,
             'description': 'A fix for this issue has been implemented'},
            {'name': "Won't Fix", 'slug': 'wont_fix', 'position': 1, 'is_system': True,
             'description': 'This issue will not be fixed (by design, out of scope, etc.)'},
            {'name': 'Duplicate', 'slug': 'duplicate', 'position': 2, 'is_system': True,
             'description': 'This issue is a duplicate of an existing issue'},
            {'name': 'Cannot Reproduce', 'slug': 'cannot_reproduce', 'position': 3, 'is_system': True,
             'description': 'The issue cannot be reproduced with the information provided'},
            {'name': 'Incomplete', 'slug': 'incomplete', 'position': 4, 'is_system': True,
             'description': 'The issue is incomplete and cannot be acted upon'},
            {'name': 'Done', 'slug': 'done', 'position': 5, 'is_system': True,
             'description': 'Work on this issue is complete'},
            {'name': "Won't Do", 'slug': 'wont_do', 'position': 6, 'is_system': False,
             'description': 'This issue will not be actioned'},
            {'name': 'Known Error', 'slug': 'known_error', 'position': 7, 'is_system': False,
             'description': 'This is a known error that is being tracked but not fixed now'},
            {'name': 'Deferred', 'slug': 'deferred', 'position': 8, 'is_system': False,
             'description': 'Valid work intentionally deferred by a human/operator decision'},
        ]


# =============================================================================
# Workflow Model (Standalone, Shareable)
# =============================================================================

class Workflow(models.Model):
    """
    Standalone workflow definition, independent of any project.

    - Workflows are standalone entities with statuses and transitions
    - Same workflow can be used across multiple projects via WorkflowScheme
    - Workflows have an initial status (entry point for new items)
    - Workflows can be active (in use) or draft (being edited)
    - System workflows ship with the platform and cannot be deleted

    A Workflow defines:
    1. Which statuses are valid (via WorkflowStatus M2M)
    2. Which transitions are allowed (via WorkflowTransition FK)
    3. What happens on each transition (conditions, validators, post-functions)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        help_text="Workflow name (e.g., 'Software Bug Workflow', 'Simple Workflow')"
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text="Machine-readable identifier"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of when to use this workflow"
    )

    # Statuses in this workflow (ordered)
    statuses = models.ManyToManyField(
        Status,
        through='WorkflowStatus',
        related_name='workflows',
        help_text="Statuses available in this workflow"
    )

    # Initial status — the status assigned to new items using this workflow
    initial_status = models.ForeignKey(
        Status,
        on_delete=models.PROTECT,
        related_name='initial_for_workflows',
        help_text="Default status for new items in this workflow"
    )

    # Lifecycle
    is_active = models.BooleanField(
        default=True,
        help_text="Active workflows can be assigned to projects. Inactive ones are drafts."
    )
    is_system = models.BooleanField(
        default=False,
        help_text="System workflows cannot be deleted"
    )

    # === Knowledge Base (ServiceNow-style) ===
    # General guidelines for the entire workflow — applies to all states.
    # Format: [{"type": "inline", "title": "...", "content": "markdown..."},
    #          {"type": "wiki", "title": "...", "url": "/wiki/spaces/SEC/..."},
    #          {"type": "link", "title": "...", "url": "https://docs.google.com/..."}]
    knowledge_base = models.JSONField(
        default=list,
        blank=True,
        help_text="Knowledge articles for this workflow — general guidelines, playbooks, external references"
    )

    # Audit
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def get_status_slugs(self):
        """Get ordered list of status slugs in this workflow."""
        return list(
            self.workflow_statuses.order_by('position')
            .values_list('status__slug', flat=True)
        )

    def get_initial_status_slug(self):
        """Get the slug of the initial status."""
        return self.initial_status.slug

    @classmethod
    def resolve(cls, value):
        """Resolve a workflow by exact name (case-insensitive) or by slug,
        tolerating space/hyphen/underscore variants. Returns the Workflow or
        None. Shared by save-time status coercion and workflow migration."""
        if not value:
            return None
        wf = cls.objects.filter(name__iexact=value).first()
        if wf:
            return wf
        base = str(value).lower()
        for s in {base, base.replace('-', '_'), base.replace('_', '-'),
                  '-'.join(base.split()), '_'.join(base.split())}:
            wf = cls.objects.filter(slug__iexact=s).first()
            if wf:
                return wf
        return None

    # PLS-623: stable natural key (slug) for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_workflow'
        ordering = ['name']
        verbose_name = 'Workflow'
        verbose_name_plural = 'Workflows'


class WorkflowStatus(models.Model):
    """
    Through table for Workflow ↔ Status M2M.
    Defines which statuses are available in a workflow and their order.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name='workflow_statuses'
    )
    status = models.ForeignKey(
        Status,
        on_delete=models.CASCADE,
        related_name='workflow_memberships'
    )
    position = models.IntegerField(
        default=0,
        help_text="Order within the workflow"
    )
    owner_action_policy = models.CharField(
        max_length=20,
        choices=[('actionable', 'Actionable'), ('awareness', 'Awareness only')],
        default='actionable',
        help_text='Owner action expected in this workflow state; independent of terminality and capacity',
    )

    # PLS-623: natural key (workflow.slug, status.slug) for idempotent fixture
    # round-trips across databases.
    natural_key_fields = ('workflow__slug', 'status__slug')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.workflow.slug, self.status.slug)

    class Meta:
        db_table = 'tasks_workflow_status'
        unique_together = [['workflow', 'status']]
        ordering = ['position']


# =============================================================================
# Workflow Scheme Model (Issue Type to Workflow Mapping)
# =============================================================================

class WorkflowScheme(models.Model):
    """
    Maps issue types (item_types) to workflows for a project.

    - Each project has exactly one WorkflowScheme
    - The scheme maps issue types to workflows:
      e.g., Bug → Bug Workflow, Story → Simple Workflow, Epic → Epic Workflow
    - A default_workflow handles any unmapped issue types
    - Multiple projects can share the same scheme
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        help_text="Scheme name (e.g., 'Software Development Scheme')"
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text="Machine-readable identifier"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of this scheme"
    )

    # Default workflow for unmapped issue types
    default_workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name='default_for_schemes',
        help_text="Workflow used for issue types not explicitly mapped"
    )

    # Lifecycle
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(
        default=False,
        help_text="System schemes cannot be deleted"
    )

    # Audit
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def get_workflow_for_item_type(self, item_type):
        """
        Resolve the correct workflow for a given item_type.

        Resolution logic:
        1. Check for exact item_type match (e.g., 'task.bug')
        2. Check for parent type match (e.g., 'task' matches 'task.bug')
        3. Fall back to default_workflow
        """
        # Exact match
        mapping = self.mappings.filter(item_type=item_type).first()
        if mapping:
            return mapping.workflow

        # Parent type match (e.g., 'task' matches 'task.bug')
        if '.' in item_type:
            parent_type = item_type.rsplit('.', 1)[0]
            mapping = self.mappings.filter(item_type=parent_type).first()
            if mapping:
                return mapping.workflow

        return self.default_workflow

    # PLS-623: stable natural key (slug) for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_workflow_scheme'
        ordering = ['name']
        verbose_name = 'Workflow Scheme'
        verbose_name_plural = 'Workflow Schemes'


class WorkflowSchemeMapping(models.Model):
    """
    Maps a specific issue type to a workflow within a scheme.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        WorkflowScheme,
        on_delete=models.CASCADE,
        related_name='mappings'
    )
    item_type = models.CharField(
        max_length=100,
        help_text="Item type this mapping applies to (e.g., 'task.bug', 'task.story', 'task')"
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name='scheme_mappings',
        help_text="Workflow to use for this item type"
    )

    # PLS-623: natural key (scheme.slug, item_type) for idempotent fixture
    # round-trips across databases.
    natural_key_fields = ('scheme__slug', 'item_type')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.scheme.slug, self.item_type)

    class Meta:
        db_table = 'tasks_workflow_scheme_mapping'
        unique_together = [['scheme', 'item_type']]
        ordering = ['item_type']


# =============================================================================
# Project Workflow Model (Links Project to Workflow Scheme)
# =============================================================================

class ProjectWorkflow(models.Model):
    """
    Workflow configuration for a project.
    Links a project to a WorkflowScheme that maps issue types to workflows.

    When workflow_scheme is set, it drives all status/transition logic.
    Legacy JSON statuses field is preserved for backward compatibility during migration.
    """
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='workflow',
        help_text="Project this workflow belongs to"
    )

    # === Legacy fields (preserved for backward compatibility) ===
    # Statuses as JSON array with metadata
    # Format: [{"name": "pending", "label": "Pending", "category": "todo", "color": "#gray"}, ...]
    statuses = models.JSONField(
        default=list,
        help_text="Legacy: List of workflow statuses with metadata"
    )
    initial_status = models.CharField(
        max_length=50,
        default='pending',
        help_text="Legacy: Default status for new items"
    )

    # === Workflow Scheme ===
    workflow_scheme = models.ForeignKey(
        WorkflowScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Workflow scheme. When set, takes precedence over legacy statuses."
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def has_workflow_scheme(self):
        """Whether this project uses the full workflow system (scheme-based)."""
        return self.workflow_scheme_id is not None

    def __str__(self):
        return f"Workflow for {self.project.key}"

    @staticmethod
    def get_default_statuses():
        """Return default workflow statuses (legacy mode)."""
        return [
            {"name": "pending", "label": "Pending", "category": "todo", "color": "#6b7280"},
            {"name": "in_progress", "label": "In Progress", "category": "in_progress", "color": "#3b82f6"},
            {"name": "blocked", "label": "Blocked", "category": "in_progress", "color": "#ef4444"},
            {"name": "in_review", "label": "In Review", "category": "in_progress", "color": "#8b5cf6"},
            {"name": "completed", "label": "Completed", "category": "done", "color": "#22c55e"},
            {"name": "cancelled", "label": "Cancelled", "category": "done", "color": "#9ca3af"},
        ]

    def get_status_names(self):
        """Get list of valid status names/slugs."""
        if self.has_workflow_scheme:
            # Collect all statuses from all workflows in the scheme
            slugs = set()
            for mapping in self.workflow_scheme.mappings.select_related('workflow'):
                slugs.update(mapping.workflow.get_status_slugs())
            slugs.update(self.workflow_scheme.default_workflow.get_status_slugs())
            return list(slugs)
        return [s['name'] for s in self.statuses]

    def get_status_by_name(self, name):
        """Get status metadata by name/slug."""
        if self.has_workflow_scheme:
            try:
                status_obj = Status.objects.get(slug=name)
                return {
                    'name': status_obj.slug,
                    'label': status_obj.name,
                    'category': status_obj.category,
                    'color': status_obj.color,
                }
            except Status.DoesNotExist:
                return None
        for status in self.statuses:
            if status['name'] == name:
                return status
        return None

    def get_statuses_by_category(self, category):
        """Get all statuses in a category."""
        if self.has_workflow_scheme:
            status_slugs = self.get_status_names()
            return [
                {'name': s.slug, 'label': s.name, 'category': s.category, 'color': s.color}
                for s in Status.objects.filter(slug__in=status_slugs, category=category)
            ]
        return [s for s in self.statuses if s.get('category') == category]

    def get_workflow_for_item(self, item):
        """
        Get the specific Workflow for an item based on its item_type.
        Returns None if in legacy mode.
        """
        if not self.has_workflow_scheme:
            return None
        return self.workflow_scheme.get_workflow_for_item_type(item.item_type)

    class Meta:
        db_table = 'tasks_project_workflow'
        verbose_name = 'Project Workflow'
        verbose_name_plural = 'Project Workflows'


class WorkflowTransition(models.Model):
    """
    Defines allowed transitions between workflow statuses.

    Features:
    - Conditions (who can see/use the transition)
    - Validators (what must be true before transition)
    - Post-functions (what happens after transition)
    - Transition screens (what form to show during transition)
    - Role-based permissions

    Each transition goes from one status to another (or from 'any' via global transitions).
    Conditions control visibility, validators control execution, post-functions run after.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Workflow this transition belongs to
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name='transitions',
        null=True,
        blank=True,
        help_text="Workflow this transition belongs to"
    )

    # Transition definition
    name = models.CharField(
        max_length=100,
        help_text="Transition name (e.g., 'Start Progress', 'Complete', 'Close')"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of when/why to use this transition"
    )
    from_status = models.CharField(
        max_length=50,
        help_text="Source status slug (* for any/global transition)"
    )
    to_status = models.CharField(
        max_length=50,
        help_text="Target status slug"
    )

    # === Conditions (who can see/use this transition) ===
    # JSON Logic format for programmatic conditions
    conditions = models.JSONField(
        null=True,
        blank=True,
        help_text="JSON Logic conditions for transition visibility"
    )

    # Role-based conditions
    # Which project roles can execute this transition
    # Empty = anyone with transition permission
    allowed_roles = models.JSONField(
        default=list,
        blank=True,
        help_text="Project roles that can use this transition (e.g., ['admin', 'member']). Empty = all roles."
    )
    # Only the current assignee can execute
    assignee_only = models.BooleanField(
        default=False,
        help_text="Only the current assignee can execute this transition"
    )
    # Only the reporter can execute
    reporter_only = models.BooleanField(
        default=False,
        help_text="Only the reporter/creator can execute this transition"
    )

    # === Validators (what must be true before transition) ===
    # Required fields that must be filled before transition
    required_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Fields required for this transition (e.g., ['assignee_id', 'due_date'])"
    )

    # Validators as structured rules
    # Format: [{"type": "field_required", "field": "assignee_id"},
    #          {"type": "subtasks_done", "message": "All sub-tasks must be completed"},
    #          {"type": "field_matches", "field": "priority", "value": ["high", "urgent"]},
    #          {"type": "permission_check", "permission": "can_edit"},
    #          {"type": "regex_match", "field": "item_key", "pattern": "^BUG-"}]
    validators = models.JSONField(
        default=list,
        blank=True,
        help_text="Structured validator rules that must pass before transition"
    )

    # === Post-Functions (what happens after transition) ===
    # Format: [{"type": "set_field", "field": "resolution", "value": "fixed"},
    #          {"type": "assign_to", "target": "project_lead"},
    #          {"type": "assign_to", "target": "user", "user_id": 123},
    #          {"type": "clear_field", "field": "resolution"},
    #          {"type": "send_notification", "template": "status_changed", "recipients": ["assignee", "reporter", "watchers"]},
    #          {"type": "fire_webhook", "url": "https://...", "method": "POST"},
    #          {"type": "add_comment", "body": "Automatically transitioned to {{to_status}}"},
    #          {"type": "set_resolution", "resolution": "fixed"},
    #          {"type": "clear_resolution"},
    #          {"type": "copy_field", "from": "assignee_id", "to": "metadata.last_assignee"},
    #          {"type": "update_parent", "when": "all_subtasks_done", "action": "transition", "to_status": "done"}]
    post_functions = models.JSONField(
        default=list,
        blank=True,
        help_text="Actions to execute after successful transition"
    )

    # === Transition Screen (popup form during transition) ===
    # Fields to display in the transition dialog
    # Format: [{"field": "resolution", "required": true, "label": "Resolution"},
    #          {"field": "comment", "required": false, "label": "Comment"},
    #          {"field": "assignee_id", "required": true, "label": "Assign to"}]
    screen_fields = models.JSONField(
        default=list,
        blank=True,
        help_text="Fields to show in transition dialog/screen"
    )

    # === Knowledge Articles (ServiceNow-style) ===
    # Inline instructions + external links shown when executing this transition.
    # Agents MUST read these before proceeding.
    # Format: [{"type": "inline", "title": "Remediation Rules", "content": "markdown text..."},
    #          {"type": "wiki", "title": "Security Playbook", "url": "/wiki/spaces/SEC/pages/playbook"},
    #          {"type": "link", "title": "Google Sheet", "url": "https://docs.google.com/..."}]
    knowledge_articles = models.JSONField(
        default=list,
        blank=True,
        help_text="Knowledge articles for this transition — inline instructions or links to Wiki/external docs"
    )

    # === Display ===
    position = models.IntegerField(
        default=0,
        help_text="Order in transition picker UI"
    )
    button_style = models.CharField(
        max_length=20,
        default='default',
        help_text="UI style: default, primary, success, warning, danger"
    )

    # === Global transition flag ===
    is_global = models.BooleanField(
        default=False,
        help_text="Global transitions are available from any status (from_status is ignored)"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        src = 'any' if self.is_global else self.from_status
        wf_name = self.workflow.name if self.workflow else ''
        return f"{wf_name}: {src} → {self.to_status} ({self.name})"

    _DEPLOYMENT_EVIDENCE_WORKFLOW = 'security-finding'
    _DEPLOYMENT_EVIDENCE_FROM_STATUS = 'awaiting_merge'
    _DEPLOYMENT_EVIDENCE_TO_STATUS = 'verification'
    _DEPLOYMENT_EVIDENCE_VALIDATOR = 'deployment_evidence_gate'

    def _is_deployment_evidence_transition(self):
        if (
            self.from_status != self._DEPLOYMENT_EVIDENCE_FROM_STATUS
            or self.to_status != self._DEPLOYMENT_EVIDENCE_TO_STATUS
            or not self.workflow_id
        ):
            return False
        workflow = self._state.fields_cache.get('workflow')
        workflow_slug = (
            workflow.slug if workflow is not None
            else Workflow.objects.filter(pk=self.workflow_id)
            .values_list('slug', flat=True).first()
        )
        return workflow_slug == self._DEPLOYMENT_EVIDENCE_WORKFLOW

    def _enforce_deployment_evidence_gate(self):
        """Keep the Release-B security handoff gate non-waivable.

        Release B's data migration activates the one live transition.  This
        write boundary protects later ordinary creates/edits: removing the
        validator merely reinstates it, while malformed or duplicate
        validator state fails loud rather than being rewritten ambiguously.
        """
        if not self._is_deployment_evidence_transition():
            return False
        if not isinstance(self.validators, list):
            raise ValidationError({
                'validators': 'Security Merge→Verification validators must be a list.',
            })
        malformed = [
            index for index, validator in enumerate(self.validators)
            if not isinstance(validator, dict)
            or not isinstance(validator.get('type'), str)
            or not validator.get('type').strip()
        ]
        if malformed:
            raise ValidationError({
                'validators': (
                    'Security Merge→Verification has malformed validator '
                    f'entries at indexes {malformed}.'
                ),
            })
        gate_count = sum(
            validator.get('type') == self._DEPLOYMENT_EVIDENCE_VALIDATOR
            for validator in self.validators
        )
        if gate_count > 1:
            raise ValidationError({
                'validators': 'Security Merge→Verification has duplicate deployment evidence gates.',
            })
        if gate_count == 0:
            self.validators = [
                *self.validators,
                {'type': self._DEPLOYMENT_EVIDENCE_VALIDATOR},
            ]
            return True
        return False

    def save(self, *args, **kwargs):
        gate_added = self._enforce_deployment_evidence_gate()
        if gate_added and kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = list({
                *kwargs['update_fields'],
                'validators',
            })
        return super().save(*args, **kwargs)

    # PLS-623: natural key (workflow.slug, from_status, name) for idempotent
    # fixture round-trips. The tuple is unique in the seeded corpus (measured:
    # 0 collisions across 141 transitions) and enforced below.
    natural_key_fields = ('workflow__slug', 'from_status', 'name')
    objects = NaturalKeyManager()

    def natural_key(self):
        return ((self.workflow.slug if self.workflow else None), self.from_status, self.name)

    class Meta:
        db_table = 'tasks_workflow_transition'
        ordering = ['position', 'from_status', 'to_status']
        indexes = [
            models.Index(fields=['workflow', 'from_status'], name='tasks_wft_workflow_status_idx'),
        ]
        # PLS-623: a transition is an EDGE — the same action name legitimately
        # appears from several source states (`Cancel / Discard` from six of
        # them on autonomous-bug), so name alone is not unique and never was.
        # Adding from_status makes the triple identifying, which is what lets a
        # fixture round-trip this table by natural key instead of by UUID PK
        # (UUIDs are per-database, so a dumped PK collides on reload).
        #
        # Measured across 183 of 327 seeded transitions before adding this:
        # zero collisions, but uniqueness was resting on editorial convention
        # rather than a constraint. security-finding is the near-miss that
        # proves the point — `Merge` and `Merge (DEPRECATED — replaced by
        # Merge → Verification)` both depart Awaiting Merge and are distinct
        # only because PLS-310 renamed one of them. Had it kept the name, a
        # natural-key load would have silently collapsed the two.
        # DB constraint lands via 0146_alter_workflowtransition_unique_together
        # (carrier: PR #207); this PR must not ship a second migration for it.
        unique_together = [['workflow', 'from_status', 'name']]


# =============================================================================
# Board Column Model (Board Configuration)
# =============================================================================

class BoardColumn(models.Model):
    """
    Board column configuration for Kanban/Sprint boards.
    Maps statuses to columns and supports WIP limits.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='board_columns',
        help_text="Project this column belongs to"
    )

    # Column definition
    name = models.CharField(max_length=100, help_text="Column display name")

    # Status mapping - which statuses map to this column
    # Example: ["pending"] or ["in_progress", "blocked"]
    status_mapping = models.JSONField(
        default=list,
        help_text="List of statuses that map to this column"
    )

    # Position in board (left to right)
    position = models.IntegerField(
        default=0,
        help_text="Column position (0 = leftmost)"
    )

    # WIP (Work In Progress) limit
    wip_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum items allowed in this column"
    )

    # Appearance
    color = models.CharField(
        max_length=7,
        default='#6b7280',
        help_text="Column header color (hex)"
    )

    # Column behavior
    is_collapsed = models.BooleanField(
        default=False,
        help_text="Whether column is collapsed by default"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key} - {self.name}"

    def get_item_count(self):
        """Get count of items in this column."""
        return Item.objects.filter(
            project=self.project,
            status__in=self.status_mapping,
            mode='task'
        ).count()

    def is_over_wip_limit(self):
        """Check if column is over WIP limit."""
        if not self.wip_limit:
            return False
        return self.get_item_count() > self.wip_limit

    @staticmethod
    def get_default_columns():
        """Return default board columns configuration."""
        return [
            {"name": "To Do", "status_mapping": ["pending"], "position": 0, "color": "#6b7280"},
            {"name": "In Progress", "status_mapping": ["in_progress", "blocked"], "position": 1, "color": "#3b82f6"},
            {"name": "In Review", "status_mapping": ["in_review"], "position": 2, "color": "#8b5cf6"},
            {"name": "Done", "status_mapping": ["completed", "cancelled"], "position": 3, "color": "#22c55e"},
        ]

    class Meta:
        db_table = 'tasks_board_column'
        ordering = ['project', 'position']
        indexes = [
            models.Index(fields=['project', 'position']),
        ]


# =============================================================================
# Saved Filter Model (JQL Queries)
# =============================================================================

class SavedFilter(models.Model):
    """
    Saved JQL queries for quick access.
    Saved filters for quick access to common queries.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Filter metadata
    name = models.CharField(max_length=255, help_text="Filter name")
    description = models.TextField(blank=True, help_text="Filter description")

    # JQL query
    jql = models.TextField(help_text="JQL query string")

    # Ownership
    user_id = models.IntegerField(
        db_index=True,
        help_text="User ID who created the filter"
    )
    user_email = models.EmailField(help_text="Email of filter owner")

    # Scope
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='saved_filters',
        help_text="Project scope (null = global)"
    )

    # Sharing
    is_shared = models.BooleanField(
        default=False,
        help_text="Whether filter is shared with team"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} by {self.user_email}"

    class Meta:
        db_table = 'tasks_saved_filter'
        ordering = ['user_id', 'name']
        indexes = [
            models.Index(fields=['user_id']),
            models.Index(fields=['project', 'is_shared']),
        ]


# =============================================================================
# Issue Type Model
# =============================================================================

class IssueType(models.Model):
    """
    Global issue type definition.


    - Issue types are global (shared across projects)
    - Each has a hierarchy level: Epic (0) > Story/Bug (1) > Task (2) > Sub-task (3)
    - Sub-task types are flagged separately (subtask=True)
    - Projects select which issue types are available via IssueTypeScheme
    """
    HIERARCHY_CHOICES = [
        (0, 'Epic'),
        (1, 'Standard (Story/Bug)'),
        (2, 'Task'),
        (3, 'Sub-task'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, help_text="Display name (e.g., 'Bug', 'Story', 'Epic')")
    slug = models.SlugField(max_length=50, unique=True, help_text="Machine-readable identifier")
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, help_text="Icon identifier (e.g., 'bug', 'story', 'epic')")
    color = models.CharField(max_length=7, default='#6b7280')

    # Maps to item_type field on Item model
    item_type_prefix = models.CharField(
        max_length=100,
        help_text="Prefix for Item.item_type (e.g., 'task.bug' → items with item_type starting with 'task.bug')"
    )

    # Hierarchy
    hierarchy_level = models.IntegerField(
        default=1,
        choices=HIERARCHY_CHOICES,
        help_text="Hierarchy level: 0=Epic, 1=Standard, 2=Task, 3=Sub-task"
    )
    is_subtask = models.BooleanField(
        default=False,
        help_text="Whether this is a sub-task type (requires parent)"
    )

    # Default workflow for this issue type (optional override)
    default_workflow = models.ForeignKey(
        Workflow,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='default_issue_types',
        help_text="Default workflow when creating new schemes"
    )

    position = models.IntegerField(default=0)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_issue_type'
        ordering = ['hierarchy_level', 'position', 'name']

    @staticmethod
    def get_default_issue_types():
        return [
            {'name': 'Epic', 'slug': 'epic', 'item_type_prefix': 'task.epic', 'hierarchy_level': 0, 'icon': 'zap', 'color': '#8b5cf6', 'is_system': True},
            {'name': 'Story', 'slug': 'story', 'item_type_prefix': 'task.story', 'hierarchy_level': 1, 'icon': 'book-open', 'color': '#22c55e', 'is_system': True},
            {'name': 'Bug', 'slug': 'bug', 'item_type_prefix': 'task.bug', 'hierarchy_level': 1, 'icon': 'bug', 'color': '#ef4444', 'is_system': True},
            {'name': 'Task', 'slug': 'task', 'item_type_prefix': 'task.generic', 'hierarchy_level': 2, 'icon': 'check-square', 'color': '#3b82f6', 'is_system': True},
            {'name': 'Sub-task', 'slug': 'subtask', 'item_type_prefix': 'task.subtask', 'hierarchy_level': 3, 'is_subtask': True, 'icon': 'corner-down-right', 'color': '#6b7280', 'is_system': True},
            {'name': 'Improvement', 'slug': 'improvement', 'item_type_prefix': 'task.improvement', 'hierarchy_level': 1, 'icon': 'trending-up', 'color': '#14b8a6', 'is_system': False},
            {'name': 'New Feature', 'slug': 'new_feature', 'item_type_prefix': 'task.feature', 'hierarchy_level': 1, 'icon': 'plus-circle', 'color': '#f59e0b', 'is_system': False},
        ]


class IssueTypeScheme(models.Model):
    """
    Defines which issue types are available per project.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    issue_types = models.ManyToManyField(
        IssueType,
        through='IssueTypeSchemeMapping',
        related_name='schemes',
        help_text="Issue types available in this scheme"
    )

    default_issue_type = models.ForeignKey(
        IssueType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='default_for_schemes',
        help_text="Default issue type for new items"
    )

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_issue_type_scheme'
        ordering = ['name']


class IssueTypeSchemeMapping(models.Model):
    """Through table for IssueTypeScheme ↔ IssueType."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(IssueTypeScheme, on_delete=models.CASCADE, related_name='mappings')
    issue_type = models.ForeignKey(IssueType, on_delete=models.CASCADE, related_name='scheme_mappings')
    position = models.IntegerField(default=0)


    # PLS-623: composite natural key for idempotent fixture round-trips.
    natural_key_fields = ('scheme__slug', 'issue_type__slug')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.scheme.slug, self.issue_type.slug)

    class Meta:
        db_table = 'tasks_issue_type_scheme_mapping'
        unique_together = [['scheme', 'issue_type']]
        ordering = ['position']


# =============================================================================
# Custom Field Model
# =============================================================================

class CustomField(models.Model):
    """
    Typed custom fields with contexts.


    - Custom fields are typed (text, number, select, multi-select, date, user, URL, etc.)
    - Each field has contexts defining which projects/issue types it appears on
    - Fields can be required, optional, or hidden per context
    - Values are stored per-item in CustomFieldValue
    """
    FIELD_TYPE_CHOICES = [
        ('text', 'Text (single line)'),
        ('textarea', 'Text Area (multi-line)'),
        ('number', 'Number'),
        ('float', 'Decimal Number'),
        ('select', 'Select (single)'),
        ('multiselect', 'Select (multiple)'),
        ('cascading_select', 'Cascading Select'),
        ('checkbox', 'Checkboxes'),
        ('radio', 'Radio Buttons'),
        ('date', 'Date Picker'),
        ('datetime', 'Date Time Picker'),
        ('user', 'User Picker'),
        ('multiuser', 'Multi User Picker'),
        ('url', 'URL Field'),
        ('labels', 'Labels'),
        ('version', 'Version Picker'),
        ('multiversion', 'Multi Version Picker'),
        ('project', 'Project Picker'),
        ('group', 'Group Picker'),
        ('readonly', 'Read Only Text'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="Display name")
    slug = models.SlugField(max_length=100, unique=True, help_text="Machine-readable key (e.g., 'customfield_10001')")
    description = models.TextField(blank=True)
    field_type = models.CharField(max_length=30, choices=FIELD_TYPE_CHOICES)

    # For select/multiselect/radio/checkbox — options as JSON
    # Format: [{"value": "option1", "label": "Option 1"}, ...]
    # For cascading_select: [{"value": "parent", "label": "Parent", "children": [{"value": "child", "label": "Child"}]}]
    options = models.JSONField(
        default=list,
        blank=True,
        help_text="Options for select-type fields"
    )

    # Default value
    default_value = models.JSONField(
        null=True,
        blank=True,
        help_text="Default value (JSON-encoded)"
    )

    # Validation
    validation_regex = models.CharField(max_length=500, blank=True, help_text="Regex pattern for validation")
    min_value = models.FloatField(null=True, blank=True, help_text="Minimum value (for number fields)")
    max_value = models.FloatField(null=True, blank=True, help_text="Maximum value (for number fields)")
    max_length = models.IntegerField(null=True, blank=True, help_text="Max length (for text fields)")

    # Searchability
    is_searchable = models.BooleanField(default=True, help_text="Include in search/JQL")

    position = models.IntegerField(default=0)
    is_system = models.BooleanField(default=False)
    created_by_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.field_type})"

    class Meta:
        db_table = 'tasks_custom_field'
        ordering = ['position', 'name']


class CustomFieldContext(models.Model):
    """
    Defines where a custom field appears (which projects/issue types).
    """
    VISIBILITY_CHOICES = [
        ('visible', 'Visible'),
        ('required', 'Required'),
        ('hidden', 'Hidden'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    custom_field = models.ForeignKey(
        CustomField,
        on_delete=models.CASCADE,
        related_name='contexts'
    )
    name = models.CharField(max_length=255, default='Default context')

    # Scope — null means "all projects" or "all issue types"
    projects = models.ManyToManyField(
        Project,
        blank=True,
        related_name='custom_field_contexts',
        help_text="Projects this context applies to (empty = all projects)"
    )
    issue_types = models.ManyToManyField(
        IssueType,
        blank=True,
        related_name='custom_field_contexts',
        help_text="Issue types this context applies to (empty = all types)"
    )

    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default='visible'
    )

    # Context-specific options override (for select fields)
    options_override = models.JSONField(
        default=list,
        blank=True,
        help_text="Override options for this context (empty = use field defaults)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.custom_field.name} - {self.name}"

    class Meta:
        db_table = 'tasks_custom_field_context'
        ordering = ['custom_field', 'name']


class CustomFieldValue(models.Model):
    """
    Stores custom field values per item.
    Values are JSON-encoded to support all field types.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='custom_field_values'
    )
    custom_field = models.ForeignKey(
        CustomField,
        on_delete=models.CASCADE,
        related_name='values'
    )

    # JSON-encoded value (supports all types)
    value = models.JSONField(
        null=True,
        blank=True,
        help_text="Field value (JSON-encoded)"
    )

    # Text representation for search
    value_text = models.TextField(
        blank=True,
        help_text="Text representation for full-text search"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.item.item_key}: {self.custom_field.name} = {self.value}"

    class Meta:
        db_table = 'tasks_custom_field_value'
        unique_together = [['item', 'custom_field']]
        indexes = [
            models.Index(fields=['item']),
            models.Index(fields=['custom_field']),
        ]


# =============================================================================
# Screen Scheme Models
# =============================================================================

class Screen(models.Model):
    """
    A screen defines which fields appear for create/edit/view operations.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    # Tabs within the screen
    # Format: [{"name": "Details", "position": 0}, {"name": "Dates", "position": 1}]
    tabs = models.JSONField(
        default=list,
        blank=True,
        help_text="Screen tabs configuration"
    )

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_screen'
        ordering = ['name']


class ScreenField(models.Model):
    """
    A field on a screen, with position and tab assignment.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    screen = models.ForeignKey(Screen, on_delete=models.CASCADE, related_name='fields')

    # Field identifier — can be a built-in field name or custom_field slug
    field_name = models.CharField(
        max_length=100,
        help_text="Built-in field name (e.g., 'summary', 'priority') or custom field slug"
    )
    custom_field = models.ForeignKey(
        CustomField,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='screen_fields',
        help_text="Link to custom field (null for built-in fields)"
    )

    # Display
    tab_name = models.CharField(max_length=100, default='Details', help_text="Which tab this field belongs to")
    position = models.IntegerField(default=0, help_text="Order within tab")
    is_required = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        db_table = 'tasks_screen_field'
        ordering = ['tab_name', 'position']
        unique_together = [['screen', 'field_name']]


class ScreenScheme(models.Model):
    """
    Maps operations (create/edit/view) to screens.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    create_screen = models.ForeignKey(
        Screen, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='create_schemes', help_text="Screen shown when creating items"
    )
    edit_screen = models.ForeignKey(
        Screen, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='edit_schemes', help_text="Screen shown when editing items"
    )
    view_screen = models.ForeignKey(
        Screen, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='view_schemes', help_text="Screen shown when viewing items"
    )

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_screen_scheme'
        ordering = ['name']


class IssueTypeScreenScheme(models.Model):
    """
    Maps issue types to screen schemes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    default_screen_scheme = models.ForeignKey(
        ScreenScheme,
        on_delete=models.PROTECT,
        related_name='default_for_issue_type_schemes',
        help_text="Default screen scheme for unmapped issue types"
    )

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_issue_type_screen_scheme'
        ordering = ['name']


class IssueTypeScreenSchemeMapping(models.Model):
    """Maps a specific issue type to a screen scheme."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(IssueTypeScreenScheme, on_delete=models.CASCADE, related_name='mappings')
    issue_type = models.ForeignKey(IssueType, on_delete=models.CASCADE, related_name='screen_scheme_mappings')
    screen_scheme = models.ForeignKey(ScreenScheme, on_delete=models.CASCADE, related_name='issue_type_mappings')


    # PLS-623: composite natural key for idempotent fixture round-trips.
    natural_key_fields = ('scheme__slug', 'issue_type__slug')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.scheme.slug, self.issue_type.slug)

    class Meta:
        db_table = 'tasks_issue_type_screen_scheme_mapping'
        unique_together = [['scheme', 'issue_type']]


# =============================================================================
# Security Level Model
# =============================================================================

class SecurityScheme(models.Model):
    """
    Security scheme with levels for per-issue visibility control.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    default_level = models.ForeignKey(
        'SecurityLevel',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='default_for_schemes',
        help_text="Default security level for new items"
    )

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'tasks_security_scheme'
        ordering = ['name']


class SecurityLevel(models.Model):
    """
    A security level within a scheme.
    Controls who can see issues at this level.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        SecurityScheme,
        on_delete=models.CASCADE,
        related_name='levels'
    )
    name = models.CharField(max_length=100, help_text="e.g., 'Internal Only', 'Confidential', 'Public'")
    slug = models.SlugField(max_length=50)
    description = models.TextField(blank=True)
    position = models.IntegerField(default=0)

    # Who can see items at this level
    # Format: [{"type": "role", "value": "admin"},
    #          {"type": "role", "value": "member"},
    #          {"type": "user", "user_id": 123},
    #          {"type": "group", "group_name": "security-team"},
    #          {"type": "project_lead"},
    #          {"type": "reporter"},
    #          {"type": "assignee"}]
    members = models.JSONField(
        default=list,
        blank=True,
        help_text="Who can see items at this security level"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.scheme.name}: {self.name}"

    class Meta:
        db_table = 'tasks_security_level'
        unique_together = [['scheme', 'slug']]
        ordering = ['scheme', 'position']


# =============================================================================
# Permission Scheme Model (Granular)
# =============================================================================

class PermissionScheme(models.Model):
    """
    Granular permission scheme for projects.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_permission_scheme'
        ordering = ['name']


class PermissionGrant(models.Model):
    """
    A single permission grant within a scheme.
    Maps a permission to a holder (role, group, user, etc.).

    Permissions: browse_projects, create_issues, edit_issues,
    transition_issues, assign_issues, assignable_user, close_issues,
    delete_issues, manage_sprints, move_issues, schedule_issues,
    set_security, create_attachments, delete_attachments,
    add_comments, edit_comments, delete_comments,
    work_on_issues, edit_own_worklogs, delete_own_worklogs,
    link_issues, manage_watchers, view_voters_and_watchers,
    administer_projects, modify_reporter, view_dev_tools,
    view_read_only_workflow, bulk_change
    """
    PERMISSION_CHOICES = [
        # Project
        ('browse_projects', 'Browse Projects'),
        ('administer_projects', 'Administer Projects'),
        # Issues
        ('create_issues', 'Create Issues'),
        ('edit_issues', 'Edit Issues'),
        ('delete_issues', 'Delete Issues'),
        ('close_issues', 'Close Issues'),
        ('transition_issues', 'Transition Issues'),
        ('assign_issues', 'Assign Issues'),
        ('assignable_user', 'Assignable User'),
        ('move_issues', 'Move Issues'),
        ('schedule_issues', 'Schedule Issues'),
        ('set_security', 'Set Issue Security'),
        ('modify_reporter', 'Modify Reporter'),
        ('link_issues', 'Link Issues'),
        ('bulk_change', 'Bulk Change'),
        # Comments
        ('add_comments', 'Add Comments'),
        ('edit_comments', 'Edit Comments'),
        ('edit_own_comments', 'Edit Own Comments'),
        ('delete_comments', 'Delete Comments'),
        ('delete_own_comments', 'Delete Own Comments'),
        # Attachments
        ('create_attachments', 'Create Attachments'),
        ('delete_attachments', 'Delete Attachments'),
        ('delete_own_attachments', 'Delete Own Attachments'),
        # Work logs
        ('work_on_issues', 'Work On Issues'),
        ('edit_own_worklogs', 'Edit Own Worklogs'),
        ('edit_all_worklogs', 'Edit All Worklogs'),
        ('delete_own_worklogs', 'Delete Own Worklogs'),
        ('delete_all_worklogs', 'Delete All Worklogs'),
        # Watchers
        ('manage_watchers', 'Manage Watchers'),
        ('view_voters_and_watchers', 'View Voters and Watchers'),
        # Sprints
        ('manage_sprints', 'Manage Sprints'),
        # Read-only
        ('view_read_only_workflow', 'View Read-Only Workflow'),
        ('view_dev_tools', 'View Development Tools'),
    ]

    HOLDER_TYPE_CHOICES = [
        ('role', 'Project Role'),
        ('group', 'Group'),
        ('user', 'Specific User'),
        ('project_lead', 'Project Lead'),
        ('reporter', 'Reporter'),
        ('assignee', 'Current Assignee'),
        ('anyone', 'Anyone'),
        ('logged_in', 'Any Logged In User'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        PermissionScheme,
        on_delete=models.CASCADE,
        related_name='grants'
    )

    permission = models.CharField(max_length=50, choices=PERMISSION_CHOICES)
    holder_type = models.CharField(max_length=20, choices=HOLDER_TYPE_CHOICES)

    # Holder identifier (depends on type)
    holder_value = models.CharField(
        max_length=255,
        blank=True,
        help_text="Role name, group name, or user ID (depends on holder_type)"
    )


    # PLS-623: composite natural key for idempotent fixture round-trips.
    natural_key_fields = ('scheme__slug', 'permission', 'holder_type', 'holder_value')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.scheme.slug, self.permission, self.holder_type, self.holder_value)

    class Meta:
        db_table = 'tasks_permission_grant'
        ordering = ['permission', 'holder_type']
        indexes = [
            models.Index(fields=['scheme', 'permission']),
        ]

    def __str__(self):
        return f"{self.permission} → {self.holder_type}:{self.holder_value}"


# =============================================================================
# Notification Scheme Model
# =============================================================================

class NotificationScheme(models.Model):
    """
    Per-event notification routing scheme.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_notification_scheme'
        ordering = ['name']


class NotificationSchemeEvent(models.Model):
    """
    Per-event notification routing within a scheme.
    Defines who gets notified for each event type.
    """
    EVENT_TYPE_CHOICES = [
        ('issue_created', 'Issue Created'),
        ('issue_updated', 'Issue Updated'),
        ('issue_assigned', 'Issue Assigned'),
        ('issue_resolved', 'Issue Resolved'),
        ('issue_closed', 'Issue Closed'),
        ('issue_reopened', 'Issue Reopened'),
        ('issue_deleted', 'Issue Deleted'),
        ('issue_moved', 'Issue Moved'),
        ('comment_created', 'Comment Created'),
        ('comment_edited', 'Comment Edited'),
        ('comment_deleted', 'Comment Deleted'),
        ('attachment_created', 'Attachment Created'),
        ('worklog_created', 'Work Logged'),
        ('worklog_updated', 'Work Log Updated'),
        ('worklog_deleted', 'Work Log Deleted'),
        ('sprint_started', 'Sprint Started'),
        ('sprint_completed', 'Sprint Completed'),
        ('version_released', 'Version Released'),
        ('status_changed', 'Generic Status Change'),
    ]

    RECIPIENT_TYPE_CHOICES = [
        ('assignee', 'Current Assignee'),
        ('reporter', 'Reporter'),
        ('project_lead', 'Project Lead'),
        ('watchers', 'All Watchers'),
        ('role', 'Project Role'),
        ('group', 'Group'),
        ('user', 'Specific User'),
        ('component_lead', 'Component Lead'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheme = models.ForeignKey(
        NotificationScheme,
        on_delete=models.CASCADE,
        related_name='events'
    )

    event_type = models.CharField(max_length=30, choices=EVENT_TYPE_CHOICES)
    recipient_type = models.CharField(max_length=20, choices=RECIPIENT_TYPE_CHOICES)

    # Recipient identifier (depends on type)
    recipient_value = models.CharField(
        max_length=255,
        blank=True,
        help_text="Role name, group name, or user ID (depends on recipient_type)"
    )


    # PLS-623: composite natural key for idempotent fixture round-trips.
    # Multiple recipients can share an event_type, so include recipient identity.
    natural_key_fields = ('scheme__slug', 'event_type', 'recipient_type', 'recipient_value')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.scheme.slug, self.event_type, self.recipient_type, self.recipient_value)

    class Meta:
        db_table = 'tasks_notification_scheme_event'
        ordering = ['event_type', 'recipient_type']
        indexes = [
            models.Index(fields=['scheme', 'event_type']),
        ]


# =============================================================================
# SLA Management Model
# =============================================================================

class SLADefinition(models.Model):
    """
    SLA definition with target response/resolution times.
    """
    METRIC_CHOICES = [
        ('time_to_first_response', 'Time to First Response'),
        ('time_to_resolution', 'Time to Resolution'),
        ('time_to_assignee', 'Time to Assignee'),
        ('time_in_status', 'Time in Status'),
        ('time_to_close', 'Time to Close'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='sla_definitions'
    )
    name = models.CharField(max_length=255, help_text="SLA name (e.g., 'P1 Bug Response')")
    description = models.TextField(blank=True)
    metric = models.CharField(max_length=30, choices=METRIC_CHOICES)

    # Target times per priority (minutes)
    # Format: {"urgent": 60, "high": 240, "normal": 480, "low": 1440}
    targets = models.JSONField(
        default=dict,
        help_text="Target time in minutes per priority level"
    )

    # Business hours calendar
    calendar = models.ForeignKey(
        'BusinessCalendar',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='sla_definitions',
        help_text="Business hours calendar (null = 24/7)"
    )

    # JQL filter — which items this SLA applies to
    jql_filter = models.TextField(
        blank=True,
        help_text="JQL filter to determine which items this SLA applies to"
    )

    # Start/stop/pause conditions (status-based)
    # Format: {"start": ["open", "reopened"], "pause": ["blocked"], "stop": ["done", "closed"]}
    conditions = models.JSONField(
        default=dict,
        blank=True,
        help_text="Status-based start/pause/stop conditions"
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key}: {self.name}"

    class Meta:
        db_table = 'tasks_sla_definition'
        ordering = ['project', 'name']


class BusinessCalendar(models.Model):
    """
    Business hours calendar with holiday support.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, help_text="e.g., 'Standard Business Hours'")
    slug = models.SlugField(max_length=100, unique=True)
    timezone = models.CharField(max_length=50, default='UTC')

    # Working hours per day of week
    # Format: {"monday": {"start": "09:00", "end": "17:00"},
    #          "tuesday": {"start": "09:00", "end": "17:00"}, ...}
    # Omitted days = non-working
    working_hours = models.JSONField(
        default=dict,
        help_text="Working hours per day of week"
    )

    # Holidays
    # Format: [{"date": "2026-12-25", "name": "Christmas"}, ...]
    holidays = models.JSONField(
        default=list,
        blank=True,
        help_text="Holiday dates"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


    # PLS-623: stable natural key for idempotent fixture round-trips.
    natural_key_fields = ('slug',)
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.slug,)

    class Meta:
        db_table = 'tasks_business_calendar'
        ordering = ['name']


class SLAInstance(models.Model):
    """
    Per-item SLA tracking instance.
    Tracks elapsed time and breach status.
    """
    SLA_STATUS_CHOICES = [
        ('active', 'Active (counting)'),
        ('paused', 'Paused'),
        ('met', 'Met (completed within target)'),
        ('breached', 'Breached (target exceeded)'),
        ('stopped', 'Stopped (no longer applicable)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='sla_instances'
    )
    sla_definition = models.ForeignKey(
        SLADefinition,
        on_delete=models.CASCADE,
        related_name='instances'
    )

    # Target for this specific item (resolved from definition + priority)
    target_minutes = models.IntegerField(help_text="Target time in minutes")

    # Tracking
    started_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    elapsed_minutes = models.IntegerField(default=0, help_text="Business-hours elapsed time")
    remaining_minutes = models.IntegerField(null=True, blank=True)

    sla_status = models.CharField(max_length=10, choices=SLA_STATUS_CHOICES, default='active')
    is_breached = models.BooleanField(default=False)
    breached_at = models.DateTimeField(null=True, blank=True)

    # Breach warning sent?
    warning_sent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_sla_instance'
        unique_together = [['item', 'sla_definition']]
        indexes = [
            models.Index(fields=['sla_status', 'is_breached']),
            models.Index(fields=['item']),
        ]


# =============================================================================
# Board Configuration (Enhanced — Swimlanes, Quick Filters, Multiple Boards)
# =============================================================================

class Board(models.Model):
    """
    Board definition supporting multiple boards per project.
    """
    BOARD_TYPE_CHOICES = [
        ('kanban', 'Kanban Board'),
        ('scrum', 'Scrum Board'),
    ]

    SWIMLANE_CHOICES = [
        ('none', 'No Swimlanes'),
        ('assignee', 'By Assignee'),
        ('priority', 'By Priority'),
        ('epic', 'By Epic'),
        ('issue_type', 'By Issue Type'),
        ('jql', 'By JQL Query'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='boards',
        help_text="Project this board belongs to"
    )

    name = models.CharField(max_length=255, help_text="Board name")
    description = models.TextField(blank=True)
    board_type = models.CharField(max_length=10, choices=BOARD_TYPE_CHOICES, default='kanban')

    # Filter — JQL query to determine which items appear on this board
    filter_jql = models.TextField(
        blank=True,
        help_text="JQL filter for board items (empty = all project items)"
    )

    # Swimlane configuration
    swimlane_by = models.CharField(
        max_length=20,
        choices=SWIMLANE_CHOICES,
        default='none'
    )
    swimlane_jql = models.TextField(
        blank=True,
        help_text="JQL query for custom swimlane grouping (when swimlane_by='jql')"
    )

    # Card layout
    # Format: {"fields": ["priority", "assignee", "labels", "due_date"], "show_days_in_column": true}
    card_layout = models.JSONField(
        default=dict,
        blank=True,
        help_text="Card display configuration"
    )

    # Card color rules
    # Format: [{"field": "priority", "value": "urgent", "color": "#ef4444"},
    #          {"jql": "labels = blocked", "color": "#f59e0b"}]
    card_colors = models.JSONField(
        default=list,
        blank=True,
        help_text="Card coloring rules"
    )

    # Estimation (for Scrum)
    estimation_field = models.CharField(
        max_length=50,
        default='story_points',
        help_text="Field used for estimation (story_points, time_tracking, etc.)"
    )

    position = models.IntegerField(default=0)
    is_default = models.BooleanField(default=False, help_text="Default board for the project")
    created_by_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.project.key}: {self.name}"

    class Meta:
        db_table = 'tasks_board'
        ordering = ['project', 'position']
        indexes = [
            models.Index(fields=['project', 'is_default']),
        ]


class BoardQuickFilter(models.Model):
    """
    Quick filter for boards — one-click JQL toggles.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    board = models.ForeignKey(
        Board,
        on_delete=models.CASCADE,
        related_name='quick_filters'
    )

    name = models.CharField(max_length=100, help_text="e.g., 'Only My Issues', 'Blockers'")
    jql = models.TextField(help_text="JQL query for this filter")
    description = models.TextField(blank=True)
    position = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.board.name}: {self.name}"

    class Meta:
        db_table = 'tasks_board_quick_filter'
        ordering = ['board', 'position']


# =============================================================================
# Dashboard Gadget Model
# =============================================================================

class Dashboard(models.Model):
    """
    Configurable dashboard with gadgets.
    """
    VISIBILITY_CHOICES = [
        ('private', 'Private (owner only)'),
        ('project', 'Project members'),
        ('org', 'Organization members'),
        ('public', 'Public'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    owner_id = models.IntegerField(db_index=True)
    owner_email = models.EmailField()

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')

    # Layout
    # Format: {"columns": 2, "layout": "2-1"} (2 columns, left wider)
    layout = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dashboard layout configuration"
    )

    is_default = models.BooleanField(default=False, help_text="System default dashboard")
    is_wallboard = models.BooleanField(default=False, help_text="Wallboard mode (fullscreen, auto-rotate)")
    position = models.IntegerField(default=0, help_text="Order in dashboard picker")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = 'tasks_dashboard'
        ordering = ['position', 'name']
        indexes = [
            models.Index(fields=['owner_id']),
        ]


class DashboardGadget(models.Model):
    """
    A gadget on a dashboard.
    """
    GADGET_TYPE_CHOICES = [
        ('filter_results', 'Filter Results'),
        ('pie_chart', 'Pie Chart'),
        ('two_dimensional', 'Two-Dimensional Filter Statistics'),
        ('created_vs_resolved', 'Created vs Resolved Chart'),
        ('sprint_burndown', 'Sprint Burndown Chart'),
        ('sprint_health', 'Sprint Health Gadget'),
        ('velocity_chart', 'Velocity Chart'),
        ('cumulative_flow', 'Cumulative Flow Diagram'),
        ('control_chart', 'Control Chart (Cycle Time)'),
        ('workload', 'Workload Pie Chart'),
        ('time_since', 'Time Since Chart'),
        ('activity_stream', 'Activity Stream'),
        ('assigned_to_me', 'Assigned to Me'),
        ('watched_issues', 'Watched Issues'),
        ('text', 'Text/HTML'),
        ('road_map', 'Road Map'),
        ('recently_created', 'Recently Created Chart'),
        ('resolution_time', 'Resolution Time Report'),
        ('average_age', 'Average Age Chart'),
        ('time_tracking', 'Time Tracking Report'),
        ('sla_met_vs_breached', 'SLA Met vs Breached'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(
        Dashboard,
        on_delete=models.CASCADE,
        related_name='gadgets'
    )

    gadget_type = models.CharField(max_length=30, choices=GADGET_TYPE_CHOICES)
    title = models.CharField(max_length=255, blank=True, help_text="Custom title (empty = use default)")

    # Position on dashboard
    column = models.IntegerField(default=0, help_text="Column index (0-based)")
    position = models.IntegerField(default=0, help_text="Row position within column")

    # Gadget-specific configuration
    # Examples:
    # filter_results: {"filter_id": "...", "columns": ["key", "summary", "status", "assignee"]}
    # pie_chart: {"stat_type": "status", "filter_id": "...", "project": "PROJ"}
    # sprint_burndown: {"board_id": "...", "sprint_id": "..."}
    # text: {"html": "<h1>Welcome</h1>"}
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Gadget-specific configuration"
    )

    # Refresh interval (seconds, 0 = no auto-refresh)
    refresh_interval = models.IntegerField(default=0)

    # Display
    is_minimized = models.BooleanField(default=False)
    color = models.CharField(max_length=7, blank=True, help_text="Gadget chrome color")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.dashboard.name}: {self.gadget_type} ({self.title or 'default'})"

    class Meta:
        db_table = 'tasks_dashboard_gadget'
        ordering = ['dashboard', 'column', 'position']


# =============================================================================
# Automation Engine Model
# =============================================================================

class AutomationRule(models.Model):
    """
    Generic automation rule with trigger → conditions → actions.


    - Triggers: events that start the rule (issue created, status changed, scheduled, etc.)
    - Conditions: filters that must pass (JQL match, field match, user role, etc.)
    - Actions: what to do (transition, edit fields, assign, comment, webhook, email, etc.)
    - Supports if/else branching
    - Smart values for templates: {{issue.summary}}, {{issue.assignee}}, etc.
    """
    TRIGGER_TYPE_CHOICES = [
        ('issue_created', 'Issue Created'),
        ('issue_updated', 'Issue Updated'),
        ('status_changed', 'Status Changed'),
        ('field_changed', 'Field Value Changed'),
        ('comment_added', 'Comment Added'),
        ('sprint_started', 'Sprint Started'),
        ('sprint_completed', 'Sprint Completed'),
        ('version_released', 'Version Released'),
        ('worklog_added', 'Work Logged'),
        ('subtask_completed', 'Sub-task Completed'),
        ('scheduled', 'Scheduled (Cron)'),
        ('manual', 'Manual Trigger'),
        ('incoming_webhook', 'Incoming Webhook'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='automation_rules',
        help_text="Project scope (null = global rule)"
    )

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # Trigger
    trigger_type = models.CharField(max_length=30, choices=TRIGGER_TYPE_CHOICES)

    # Trigger configuration
    # Format depends on trigger_type:
    # status_changed: {"from_status": "open", "to_status": "in_progress"}
    # field_changed: {"field": "priority", "from_value": "low", "to_value": "high"}
    # scheduled: {"cron": "0 9 * * 1-5", "timezone": "Asia/Kolkata"}
    # incoming_webhook: {"webhook_key": "..."}
    trigger_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Trigger-specific configuration"
    )

    # Conditions (must all pass for rule to execute)
    # Format: [{"type": "jql_match", "jql": "priority = high AND status != Done"},
    #          {"type": "field_match", "field": "assignee_id", "operator": "is_not_empty"},
    #          {"type": "user_in_role", "role": "admin"},
    #          {"type": "subtasks_done"},
    #          {"type": "field_changed_from_to", "field": "status", "from": "open", "to": "in_progress"}]
    conditions = models.JSONField(
        default=list,
        blank=True,
        help_text="Conditions that must pass for rule to execute"
    )

    # Actions (executed in order)
    # Format: [{"type": "transition", "to_status": "in_progress"},
    #          {"type": "edit_field", "field": "priority", "value": "high"},
    #          {"type": "assign", "target": "project_lead"},
    #          {"type": "comment", "body": "Auto-assigned by rule: {{rule.name}}"},
    #          {"type": "send_email", "to": "{{issue.assignee.email}}", "subject": "...", "body": "..."},
    #          {"type": "create_subtask", "summary": "Review: {{issue.summary}}", "issue_type": "subtask"},
    #          {"type": "link_issue", "link_type": "blocks", "target_jql": "..."},
    #          {"type": "webhook", "url": "...", "method": "POST", "body": {...}},
    #          {"type": "add_to_sprint", "sprint": "active"},
    #          {"type": "slack_message", "channel": "#dev", "message": "..."},
    #          {"type": "log_work", "time_spent": "30m", "comment": "Auto-logged"},
    #          {"type": "if", "condition": {"type": "jql_match", "jql": "..."}, "then": [...], "else": [...]}]
    actions = models.JSONField(
        default=list,
        blank=True,
        help_text="Actions to execute (supports if/else branching)"
    )

    # Execution
    is_enabled = models.BooleanField(default=True)
    execution_count = models.IntegerField(default=0)
    last_executed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    # Rate limiting
    max_executions_per_hour = models.IntegerField(
        default=100,
        help_text="Maximum executions per hour (prevents runaway rules)"
    )

    # Audit
    created_by_id = models.IntegerField(null=True, blank=True)
    created_by_email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        scope = self.project.key if self.project else 'Global'
        return f"[{scope}] {self.name} ({self.trigger_type})"

    class Meta:
        db_table = 'tasks_automation_rule'
        ordering = ['project', 'name']
        indexes = [
            models.Index(fields=['trigger_type', 'is_enabled']),
            models.Index(fields=['project', 'is_enabled']),
        ]


class AutomationRuleLog(models.Model):
    """
    Audit log for automation rule executions.
    """
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('error', 'Error'),
        ('skipped', 'Skipped (conditions not met)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(
        AutomationRule,
        on_delete=models.CASCADE,
        related_name='logs'
    )
    item = models.ForeignKey(
        Item,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='automation_logs'
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    trigger_data = models.JSONField(default=dict, blank=True, help_text="Trigger event data")
    actions_executed = models.JSONField(default=list, blank=True, help_text="List of actions that ran")
    error_message = models.TextField(blank=True)
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_automation_rule_log'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['rule', '-created_at']),
            models.Index(fields=['item']),
        ]


# =============================================================================
# Project Configuration Links (tie all schemes to a project)
# =============================================================================

class ProjectConfiguration(models.Model):
    """
    Central configuration linking all schemes to a project.
    This is the project-level "glue" that ties together all scheme types.

    """
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='configuration'
    )

    # Scheme assignments
    issue_type_scheme = models.ForeignKey(
        IssueTypeScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Which issue types are available"
    )
    issue_type_screen_scheme = models.ForeignKey(
        IssueTypeScreenScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Field layouts per issue type per operation"
    )
    security_scheme = models.ForeignKey(
        SecurityScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Per-issue security levels"
    )
    permission_scheme = models.ForeignKey(
        PermissionScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Granular permission mapping"
    )
    notification_scheme = models.ForeignKey(
        NotificationScheme,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='projects',
        help_text="Per-event notification routing"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Config for {self.project.key}"

    class Meta:
        db_table = 'tasks_project_configuration'
        verbose_name = 'Project Configuration'
        verbose_name_plural = 'Project Configurations'


class ProjectTeamLead(models.Model):
    """Sticky per-(project, team) lead, lazily ELECTED on first team assignment
    in that project (operator design 2026-06-11): tasks are assigned to TEAMS,
    never people; the resolver elects a suitable member (tier-preference, then
    least-loaded) on first contact and that member stays the lead for the
    (project, team) pair. A saturated lead spills individual tasks to peers but
    KEEPS the leadership (affinity is sticky). Runtime/provider unavailability
    spills individual assignments without mutating the durable election; removal
    from the authoritative team roster deactivates it on the resolver path.
    """
    # PLS-21 (review P2): scope the (project, team) lead key by org. Project
    # keys are unique only WITHIN an org, so without org_id a multi-tenant
    # platform (PLAT-352) would collide two orgs' "PLAT"/"engineering" leads in
    # one active row. Added while the table is new + empty (cheapest moment).
    # Default 0 = the implicit single/default org (today's single-tenant state);
    # NOT NULL so the partial-unique constraint actually enforces one active lead
    # (Postgres treats NULLs as distinct, which would silently allow duplicates).
    org_id = models.IntegerField(default=0, db_index=True)
    project_key = models.CharField(max_length=16, db_index=True)
    team_name = models.CharField(max_length=64)
    lead_email = models.EmailField()
    lead_id = models.IntegerField(null=True, blank=True)
    elected_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    deactivated_reason = models.CharField(max_length=128, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['org_id', 'project_key', 'team_name'],
                condition=models.Q(is_active=True),
                name='uniq_active_project_team_lead',
            ),
        ]

    def __str__(self):
        return f'{self.project_key}/{self.team_name} -> {self.lead_email}'


class TeamRoster(models.Model):
    """DB-backed team routing roster (operator directive 2026-06-12).

    Routing config like the pod roster MUST live in the persistence layer, never
    baked into the container image — a roster hardcoded in workflow_service.py was
    silently reverted on every pulse image rebuild from a stale commit, repeatedly
    breaking review routing (resolver returned None / crashed on tier mismatch).

    `tiers` is the PLAT-232 tiered structure: a list of tiers, each tier a list of
    member emails. Tier-2+ are FAILOVER-ONLY (engaged when the whole higher tier is
    unavailable). A single flat tier `[["a@x","b@x"]]` is a normal load-balanced team.

    WorkflowService._pod_roster() overlays active rows here on the code-level
    _POD_ROSTER defaults (which act only as the initial seed), so the effective
    roster survives any image roll and is editable live with no redeploy.
    """
    team_name = models.CharField(max_length=64, db_index=True)
    # PLAT-4213 (operator 2026-07-11, single-team invariant): every roster
    # belongs to exactly ONE organization. Team names are unique only inside
    # that organization, so separate orgs may each own an `accounting` roster.
    # This column makes the org explicit so routing/guards can enforce that a
    # task's assignee serves the task's project org.
    organization_slug = models.CharField(max_length=64, blank=True, default='shizuha', db_index=True)
    organization_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    tiers = models.JSONField(default=list)
    # PLAT-431 (cycle 5): explicit per-team junior member emails. For JUNIOR_ONLY
    # teams (review/documentation) this is the AUTHORITATIVE junior set, read by
    # WorkflowService._junior_emails() — replacing tier-POSITION inference
    # (tiers[-1]), which misidentified a senior failover as a junior if an operator
    # removed the junior tier. Operator-editable in the DB; never hardcoded in code.
    junior_emails = models.JSONField(default=list, blank=True)
    # PLS-745: every authoritative roster mutation carries an optimistic
    # version.  This is deliberately stored beside the roster rather than in a
    # cache so callers can distinguish an idempotent replay from a stale write.
    version = models.PositiveBigIntegerField(default=0)
    # PLS-509: WIP limits are authoritative roster configuration.  They are
    # deliberately not inferred from agent prose or from post-hoc alerts.
    # A zero cap disables that dimension.  Operator directive 2026-08-08:
    # agents routinely run many hands-on tasks (devops seats carry 6-8 today),
    # so the ACTIVE dimension defaults to a generous 10 — a genuine runaway
    # backstop, not a single-task throttle. The real per-seat pull discipline is
    # the bounded READY queue (ready_wip_cap), i.e. the limited task view an
    # agent polls for work.
    wip_enforced = models.BooleanField(default=False)
    ready_wip_cap = models.PositiveSmallIntegerField(default=5)
    active_wip_cap = models.PositiveSmallIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=128, blank=True, default='')

    # PLAT-355 aoi P4: aliases mirror WorkflowService._canonical_team_name — inline
    # to avoid a circular import (services/ imports models). Keep in sync.
    # PLAT-724: ceo-decisions/leads/triage now alias to human-ops, not admin-ops.
    _CANONICAL_ALIASES = {
        'admin_ops': 'admin-ops',
        # PLS-539 (invariant I1): `ceo-decisions` is a real, org-scoped decision
        # team and must NEVER be canonicalized to the physical-only `human-ops`
        # lane. This TeamRoster canonicalization is what rewrote TRD-26's stored
        # team label to `human-ops`. It now resolves by exact slug within its
        # organization; `human-ops` stays physical-only, explicit-only.
        'leads': 'human-ops',
        'triage': 'human-ops',
        'security-triage': 'security',
        'platform-ops': 'devops',
        # Operator 2026-07-16: Research and Research Analytics are one team.
        # Keep accepting the short/legacy labels, but never persist a second
        # roster identity beside the canonical consolidated team.
        'analytics': 'research-analytics',
        'research': 'research-analytics',
        # Organization is a separate identity dimension; it must never be
        # encoded into a second team name. Keep legacy inputs readable while
        # persisting the canonical Team key in every organization.
        'personal-accounting': 'accounting',
        'digital-accounting': 'accounting',
        'digital-quant': 'quant',
        'securities-engineering': 'engineering',
        'trading-engineering': 'engineering',
    }
    _single_membership_guard = False

    @staticmethod
    def _is_email(x):
        return (
            isinstance(x, str)
            and x.count('@') == 1
            and '.' in x.split('@')[-1]
            and len(x) >= 6
        )

    @classmethod
    def validate_roster_structure(cls, tiers, junior_emails=None):
        """Reject malformed roster JSON before it can reach the DB.

        This is the guard that makes the 2026-06-15 char-splat corruption
        impossible from ANY write path — Django admin, a shell, the REST API, an
        MCP tool, or a migration — because every TeamRoster write goes through
        save(). That incident stored emails as single characters
        (``[['r','y','o','@',...], ...]``) from a ``[list(email) for email in ...]``
        bug (``list('a@b')`` splats to chars); ``_resolve_team_assignee`` then
        returned None and NEW work for the team (e.g. engineering) could not route.

        Contract: ``tiers`` is a list of tiers, each tier a list of email strings;
        ``junior_emails`` is a flat list of email strings. An empty list is valid
        (a decommissioned team has ``tiers=[]``).
        """
        from django.core.exceptions import ValidationError

        if not isinstance(tiers, list):
            raise ValidationError(
                {'tiers': 'must be a list of tiers (each tier a list of email strings)'}
            )
        for i, tier in enumerate(tiers):
            if not isinstance(tier, list):
                raise ValidationError(
                    {'tiers': f'tier[{i}] must be a list of email strings, got '
                              f'{type(tier).__name__}'}
                )
            for j, member in enumerate(tier):
                if not cls._is_email(member):
                    raise ValidationError({'tiers': (
                        f'tier[{i}][{j}]={member!r} is not a valid email. A tier of '
                        f'single characters means a `list(email)` char-splat bug — pass '
                        f'[["a@x.com","b@x.com"], ...], not [list("a@x.com"), ...].'
                    )})
        if junior_emails is not None:
            if not isinstance(junior_emails, list):
                raise ValidationError(
                    {'junior_emails': 'must be a flat list of email strings'}
                )
            for j, member in enumerate(junior_emails):
                if not cls._is_email(member):
                    raise ValidationError(
                        {'junior_emails': f'junior_emails[{j}]={member!r} is not a valid email'}
                    )

    def clean(self):
        super().clean()
        self.validate_roster_structure(self.tiers, self.junior_emails)

    @staticmethod
    def _flat_member_emails(tiers, junior_emails=None):
        emails = set()
        for tier in tiers or []:
            for member in tier or []:
                if isinstance(member, str):
                    emails.add(member.strip().lower())
        for member in junior_emails or []:
            if isinstance(member, str):
                emails.add(member.strip().lower())
        return {email for email in emails if email}

    @classmethod
    def _remove_member_emails(cls, tiers, junior_emails, emails):
        changed = False
        new_tiers = []
        for tier in tiers or []:
            kept = []
            for member in tier or []:
                if isinstance(member, str) and member.strip().lower() in emails:
                    changed = True
                    continue
                kept.append(member)
            new_tiers.append(kept)
        new_junior = []
        for member in junior_emails or []:
            if isinstance(member, str) and member.strip().lower() in emails:
                changed = True
                continue
            new_junior.append(member)
        return new_tiers, new_junior, changed

    def _enforce_single_team_membership(self):
        if not self.is_active:
            return
        emails = self._flat_member_emails(self.tiers, self.junior_emails)
        # The operator sits on more than one lane (human-ops and
        # ceo-decisions). One-agent-one-team is for agent seats.
        from tasks.services.workflow_service import WorkflowService
        emails = {
            email for email in emails
            if not WorkflowService._is_human_account(email)
        }
        if not emails:
            return

        siblings = TeamRoster.objects.exclude(pk=self.pk)
        if self.organization_id is not None:
            siblings = siblings.filter(organization_id=self.organization_id)
        else:
            siblings = siblings.filter(
                organization_id__isnull=True,
                organization_slug=self.organization_slug,
            )
        for roster in siblings:
            tiers, junior_emails, changed = self._remove_member_emails(
                roster.tiers,
                roster.junior_emails,
                emails,
            )
            if not changed:
                continue
            roster.tiers = tiers
            roster.junior_emails = junior_emails
            roster.save(update_fields=['tiers', 'junior_emails', 'updated_at'])

    def _invalidate_workflow_roster_caches(self):
        try:
            from tasks.services.workflow_service import WorkflowService
            WorkflowService._invalidate_team_roster_caches()
        except Exception:
            logger.warning('TeamRoster cache invalidation failed', exc_info=True)

    def save(self, *args, **kwargs):
        norm = (self.team_name or '').strip().lower().replace('_', '-').replace(' ', '-')
        self.team_name = self._CANONICAL_ALIASES.get(norm, norm)
        self.organization_slug = (self.organization_slug or 'shizuha').strip().lower()
        known_org_id = {
            'shizuha': 1,
            'shizuha-digital': 2,
            'shizuha-trading': 3,
            'personal-3': 4,
        }.get(self.organization_slug)
        if self.organization_id is None:
            self.organization_id = known_org_id
        elif known_org_id is not None and int(self.organization_id) != known_org_id:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                'organization_id': (
                    f'{self.organization_slug} is organization {known_org_id}, '
                    f'not {self.organization_id}.'
                ),
            })
        # Every write advances the optimistic command version, including
        # Django-admin/lifecycle writes outside the command endpoint.  Otherwise
        # a later command could accept an expected_version that predates an
        # out-of-band roster edit.
        self.version = max(int(self.version or 0), 0) + 1
        # QuerySet.update_or_create() commonly calls save(update_fields=...).
        # Always include normalized identity fields so an alias cannot survive
        # merely because the caller updated tiers or activity in place.
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {
                'team_name', 'organization_slug', 'organization_id', 'version',
            }
        # Enforce structural validation on EVERY write path (admin/shell/API/MCP/
        # migration) so the char-splat corruption class can never recur.
        self.validate_roster_structure(self.tiers, self.junior_emails)
        # PLAT-4220 (operator 2026-07-11): the shizuha.io namespace is NOT owned —
        # agents live on @shizuha.com. Reject the invalid agent domain on every
        # write so a mis-provisioned agent can't re-introduce the split-brain.
        # (Human members like the operator's gmail are unaffected; we forbid only
        # the invalid *.shizuha.io agent domain, not "non-@shizuha.com".)
        from django.core.exceptions import ValidationError
        _bad = sorted(e for e in self._flat_member_emails(self.tiers, self.junior_emails)
                      if e.endswith('.shizuha.io'))
        if _bad:
            raise ValidationError({'tiers': (
                f'shizuha.io is not an owned domain — agent emails must be @shizuha.com: {_bad}')})
        # one-agent-one-team is already enforced by _enforce_single_team_membership()
        # below (auto-removes the agent from every OTHER team's roster on save).
        super().save(*args, **kwargs)
        if not TeamRoster._single_membership_guard:
            TeamRoster._single_membership_guard = True
            try:
                self._enforce_single_team_membership()
            finally:
                TeamRoster._single_membership_guard = False
        self._invalidate_workflow_roster_caches()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        self._invalidate_workflow_roster_caches()
        return result

    # PLS-623: natural key for idempotent fixture round-trips.
    natural_key_fields = ('organization_slug', 'team_name')
    objects = NaturalKeyManager()

    def natural_key(self):
        return (self.organization_slug, self.team_name)

    class Meta:
        db_table = 'tasks_team_roster'
        verbose_name = 'Team Roster'
        verbose_name_plural = 'Team Rosters'
        constraints = [
            models.UniqueConstraint(
                fields=['organization_slug', 'team_name'],
                name='uniq_team_roster_org_name',
            ),
            models.UniqueConstraint(
                fields=['organization_id', 'team_name'],
                condition=models.Q(organization_id__isnull=False),
                name='uniq_team_roster_org_id_name',
            ),
        ]

    def __str__(self):
        return f'{self.organization_slug}/{self.team_name}: {self.tiers}'


class TeamRosterCommandReceipt(models.Model):
    """Payload-bound idempotency receipt for authoritative roster commands."""

    command_id = models.UUIDField(unique=True, db_index=True)
    payload_hash = models.CharField(max_length=64)
    organization_id = models.PositiveIntegerField()
    organization_slug = models.CharField(max_length=64)
    team_name = models.CharField(max_length=64)
    email = models.EmailField(blank=True, default='')
    action = models.CharField(max_length=16)
    expected_version = models.PositiveBigIntegerField()
    previous_version = models.PositiveBigIntegerField()
    result_version = models.PositiveBigIntegerField()
    result = models.JSONField(default=dict)
    actor_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_team_roster_command_receipt'
        ordering = ['-created_at', '-id']


class AgentWipCapacity(models.Model):
    """Stable row-lock target for one agent's admission boundary (PLS-509)."""

    organization_slug = models.CharField(max_length=64, default='shizuha')
    assignee_email = models.EmailField()
    assignee_id = models.IntegerField(null=True, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization_slug', 'assignee_email'],
                name='uniq_agent_wip_capacity',
            ),
            models.UniqueConstraint(
                fields=['organization_slug', 'assignee_id'],
                condition=models.Q(assignee_id__isnull=False),
                name='uniq_agent_wip_capacity_identity',
            ),
        ]


class WipSlotReservation(models.Model):
    """Bounded reservation bridging resolver selection and the Item save."""

    SLOT_READY = 'ready'
    SLOT_ACTIVE = 'active'
    SLOT_CHOICES = ((SLOT_READY, 'Ready'), (SLOT_ACTIVE, 'Active'))

    token = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capacity = models.ForeignKey(
        AgentWipCapacity,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    item = models.ForeignKey(
        'Item',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='wip_reservations',
    )
    assignment_group = models.CharField(max_length=64)
    slot_kind = models.CharField(max_length=12, choices=SLOT_CHOICES)
    assignment_generation = models.PositiveBigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # PLAT-9491: Hive scope-manifest assertion stamp carried from the capability
    # selection (evidence, never a gate — the gate lives in Hive's mint path,
    # HIVE-2695). JSON stamp per tasks/services/scope_assertion.py; NULL when
    # the assertion was disabled/skipped or the dispatch had no capability
    # requirement.
    scope_assertions = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'assignment_generation'],
                condition=models.Q(item__isnull=False),
                name='uniq_item_wip_generation',
            ),
        ]


# =============================================================================
# PLAT-4572 / ADR-0041 — routing authority is not authorization membership
# =============================================================================
class RoutingTeam(models.Model):
    """Stable Pulse-owned routing-team identity; never an access membership."""
    STATE_ACTIVE = 'active'
    STATE_TOMBSTONED = 'tombstoned'
    STATE_CHOICES = [(STATE_ACTIVE, 'Active'), (STATE_TOMBSTONED, 'Tombstoned')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.PositiveIntegerField(db_index=True)
    key = models.SlugField(max_length=100)
    display_name = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_ACTIVE)
    version = models.PositiveBigIntegerField(default=1)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_routing_team'
        constraints = [models.UniqueConstraint(
            fields=['organization_id', 'key'], name='uniq_routing_team_org_key')]


class RoutingSubject(models.Model):
    """Exact-org local projection of Shizuha ID identity/activity authority."""
    STATE_ACTIVE = 'active'
    STATE_REVOKED = 'revoked'
    STATE_CHOICES = [(STATE_ACTIVE, 'Active'), (STATE_REVOKED, 'Revoked')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.PositiveIntegerField(db_index=True)
    subject_id = models.PositiveBigIntegerField(db_index=True)
    canonical_email = models.EmailField()
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_ACTIVE)
    generation = models.PositiveBigIntegerField(default=1)
    identity_event_id = models.UUIDField(default=uuid.uuid4)
    digest = models.CharField(max_length=64)
    revocation_high_water = models.PositiveBigIntegerField(default=0)
    observed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_routing_subject'
        constraints = [
            models.UniqueConstraint(fields=['organization_id', 'subject_id'],
                                    name='uniq_routing_subject_org_id'),
            models.UniqueConstraint(fields=['organization_id', 'canonical_email'],
                                    name='uniq_routing_subject_org_email'),
        ]


class RoutingTeamBinding(models.Model):
    """Append-only exact-org routing candidate binding history."""
    STATE_ACTIVE = 'active'
    STATE_TOMBSTONED = 'tombstoned'
    STATE_CHOICES = [(STATE_ACTIVE, 'Active'), (STATE_TOMBSTONED, 'Tombstoned')]

    organization_id = models.PositiveIntegerField(db_index=True)
    routing_team = models.ForeignKey(RoutingTeam, on_delete=models.PROTECT,
                                     related_name='binding_versions')
    subject = models.ForeignKey(RoutingSubject, on_delete=models.PROTECT,
                                related_name='binding_versions')
    state = models.CharField(max_length=20, choices=STATE_CHOICES)
    version = models.PositiveBigIntegerField()
    is_current = models.BooleanField(default=True, db_index=True)
    subject_generation = models.PositiveBigIntegerField()
    command_id = models.UUIDField()
    origin_event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_team_binding'
        constraints = [
            models.UniqueConstraint(
                fields=['organization_id', 'routing_team', 'subject', 'version'],
                name='uniq_routing_binding_version'),
            models.UniqueConstraint(
                fields=['organization_id', 'routing_team', 'subject'],
                condition=Q(is_current=True), name='uniq_routing_binding_current'),
        ]
        indexes = [models.Index(
            fields=['organization_id', 'routing_team', 'state', 'is_current'],
            name='routing_binding_candidate_idx')]

    def clean(self):
        super().clean()
        if self.routing_team_id and self.organization_id != self.routing_team.organization_id:
            from django.core.exceptions import ValidationError
            raise ValidationError({'organization_id': 'routing_team_org_mismatch'})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class ReceiveAssignmentEntitlement(models.Model):
    """Independent provenance-bearing permission to receive an assignment."""
    STATE_ACTIVE = 'active'
    STATE_REVOKED = 'revoked'
    STATE_CHOICES = [(STATE_ACTIVE, 'Active'), (STATE_REVOKED, 'Revoked')]

    organization_id = models.PositiveIntegerField(db_index=True)
    subject = models.ForeignKey(RoutingSubject, on_delete=models.PROTECT,
                                related_name='receive_entitlement_versions')
    routing_team = models.ForeignKey(RoutingTeam, on_delete=models.PROTECT,
                                     related_name='receive_entitlement_versions')
    project = models.ForeignKey('Project', null=True, blank=True, on_delete=models.PROTECT,
                                related_name='receive_assignment_entitlements')
    workflow_name = models.CharField(max_length=100)
    action = models.CharField(max_length=64, default='receive_assignment')
    state = models.CharField(max_length=20, choices=STATE_CHOICES)
    version = models.PositiveBigIntegerField()
    is_current = models.BooleanField(default=True, db_index=True)
    subject_generation = models.PositiveBigIntegerField()
    grant_source = models.CharField(max_length=100)
    grant_issuer_id = models.PositiveBigIntegerField()
    grant_issuer_email = models.EmailField()
    grant_event_id = models.UUIDField(default=uuid.uuid4)
    revocation_high_water = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    command_id = models.UUIDField()
    origin_event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_receive_assignment_entitlement'
        constraints = [
            models.UniqueConstraint(
                fields=['organization_id', 'subject', 'routing_team', 'project',
                        'workflow_name', 'action', 'version'],
                condition=Q(project__isnull=False),
                name='uniq_receive_entitlement_project_ver'),
            models.UniqueConstraint(
                fields=['organization_id', 'subject', 'routing_team', 'project',
                        'workflow_name', 'action'],
                condition=Q(is_current=True, project__isnull=False),
                name='uniq_receive_entitlement_project_cur'),
            models.UniqueConstraint(
                fields=['organization_id', 'subject', 'routing_team',
                        'workflow_name', 'action', 'version'],
                condition=Q(project__isnull=True),
                name='uniq_receive_entitlement_org_ver'),
            models.UniqueConstraint(
                fields=['organization_id', 'subject', 'routing_team',
                        'workflow_name', 'action'],
                condition=Q(is_current=True, project__isnull=True),
                name='uniq_receive_entitlement_org_cur'),
        ]
        indexes = [models.Index(
            fields=['organization_id', 'subject', 'routing_team', 'state', 'is_current'],
            name='receive_entitlement_lookup_idx')]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        if self.routing_team_id and self.organization_id != self.routing_team.organization_id:
            raise ValidationError({'organization_id': 'routing_team_org_mismatch'})
        if self.project_id and self.organization_id != self.project.org_id:
            raise ValidationError({'project': 'project_org_mismatch'})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class RoutingAuthorityCheckpoint(models.Model):
    """Causally-bound freshness lease for a projected authority aggregate."""
    authority_id = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    subject_generation = models.PositiveBigIntegerField(default=0)
    observed_version = models.PositiveBigIntegerField(default=0)
    digest = models.CharField(max_length=64)
    revocation_high_water = models.PositiveBigIntegerField(default=0)
    checkpoint_generation = models.PositiveBigIntegerField(default=1)
    source_event_id = models.UUIDField(null=True, blank=True)
    first_received_at = models.DateTimeField(null=True, blank=True)
    source_expires_at = models.DateTimeField(null=True, blank=True)
    observed_at = models.DateTimeField()
    lease_until = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_routing_authority_checkpoint'
        constraints = [models.UniqueConstraint(
            fields=['authority_id', 'aggregate_key'], name='uniq_routing_authority_checkpoint')]


class CrossOrgAssignmentDelegation(models.Model):
    """Separate reviewed authority for cross-organization assignment receipt."""
    STATE_ACTIVE = 'active'
    STATE_REVOKED = 'revoked'
    STATE_CHOICES = [(STATE_ACTIVE, 'Active'), (STATE_REVOKED, 'Revoked')]

    source_organization_id = models.PositiveIntegerField(db_index=True)
    target_organization_id = models.PositiveIntegerField(db_index=True)
    subject = models.ForeignKey(RoutingSubject, on_delete=models.PROTECT,
                                related_name='cross_org_delegation_versions')
    routing_team = models.ForeignKey(RoutingTeam, on_delete=models.PROTECT,
                                     related_name='cross_org_delegation_versions')
    project = models.ForeignKey('Project', on_delete=models.PROTECT,
                                related_name='cross_org_assignment_delegations')
    action = models.CharField(max_length=64, default='receive_assignment')
    state = models.CharField(max_length=20, choices=STATE_CHOICES)
    version = models.PositiveBigIntegerField()
    is_current = models.BooleanField(default=True, db_index=True)
    subject_generation = models.PositiveBigIntegerField()
    revocation_generation = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField()
    command_id = models.UUIDField()
    origin_event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_cross_org_assignment_delegation'
        constraints = [
            models.UniqueConstraint(
                fields=['source_organization_id', 'target_organization_id',
                        'subject', 'routing_team', 'project', 'action', 'version'],
                name='uniq_cross_org_delegation_version'),
            models.UniqueConstraint(
                fields=['source_organization_id', 'target_organization_id',
                        'subject', 'routing_team', 'project', 'action'],
                condition=Q(is_current=True),
                name='uniq_cross_org_delegation_current'),
        ]


class RoutingAssignmentReceipt(models.Model):
    """One-use DB receipt for an exact resulting assignment/routing tuple."""
    ACTION_ASSIGN = 'assign'
    ACTION_CLEAR = 'clear'
    ACTION_UNASSIGN = 'unassign'
    ACTION_REROUTE_GROUP_QUEUE = 'reroute_group_queue'
    # PLS-539 (hiro's ADR-0010 determination): an authorized direct handoff that
    # crosses OUT of a materialized guarded lane into a concrete owner in a
    # (possibly unguarded) result lane. subject_id/assignee_email bind the NEW
    # owner who lands; routing_team/assignment_group/workflow bind the OLD guarded
    # lane being left; result_* bind the NEW lane. Only minted by the shared
    # direct-handoff command after the reason gate + item authorization pass.
    ACTION_DIRECT_HANDOFF = 'direct_handoff'
    ACTION_CHOICES = (
        (ACTION_ASSIGN, 'Assign concrete owner'),
        (ACTION_CLEAR, 'Clear owner to group queue'),
        (ACTION_UNASSIGN, 'Explicitly unassign and clear routing group'),
        (ACTION_REROUTE_GROUP_QUEUE, 'Reroute to exact group queue'),
        (ACTION_DIRECT_HANDOFF, 'Authorized direct handoff out of a guarded lane'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES,
                              default=ACTION_ASSIGN)
    reason = models.CharField(max_length=100, blank=True, default='')
    item = models.ForeignKey('Item', on_delete=models.PROTECT,
                             related_name='routing_assignment_receipts')
    organization_id = models.PositiveIntegerField()
    project = models.ForeignKey('Project', on_delete=models.PROTECT,
                                related_name='routing_assignment_receipts')
    routing_team = models.ForeignKey('RoutingTeam', on_delete=models.PROTECT,
                                     related_name='assignment_receipts',
                                     null=True, blank=True)
    assignment_group = models.CharField(max_length=100)
    workflow_name = models.CharField(max_length=100)
    subject_id = models.PositiveBigIntegerField(null=True, blank=True)
    assignee_email = models.EmailField(blank=True, default='')
    binding_version = models.PositiveBigIntegerField()
    entitlement_version = models.PositiveBigIntegerField()
    delegation_version = models.PositiveBigIntegerField(null=True, blank=True)
    subject_generation = models.PositiveBigIntegerField()
    revocation_high_water = models.PositiveBigIntegerField()
    freshness_vector_digest = models.CharField(max_length=64)
    result_organization_id = models.PositiveIntegerField()
    result_project = models.ForeignKey(
        'Project', on_delete=models.PROTECT,
        related_name='routing_assignment_result_receipts',
    )
    result_routing_team = models.ForeignKey(
        'RoutingTeam', on_delete=models.PROTECT,
        related_name='assignment_result_receipts', null=True, blank=True,
    )
    result_assignment_group = models.CharField(max_length=100, blank=True, default='')
    result_workflow_name = models.CharField(max_length=100, blank=True, default='')
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_assignment_receipt'
        indexes = [models.Index(fields=['item', 'consumed_at', 'expires_at'],
                                name='routing_receipt_consume_idx')]


class SubstrateMaterializationHighWater(models.Model):
    """HIVE-1153 P1 #2 (HIVE-1174 Security decision): durable per-exact-seat monotonic
    high-water of the producer's SIGNED materialization, so a superseded materialization
    (a newer `iat` verified inside the 5s window) HOLDs an older carried receipt at
    commit — closing the offline-supersession gap.

    Keyed by the EXACT seat + producer generation:
    ``(producer_generation_id, organization_id, subject, team_slug)``. Verification
    advances it (newer `iat` wins; an older response never rewinds; equal `iat` with a
    different `materialization_generation` latches `ambiguous` and fails closed) BEFORE
    returning AUTHORIZED. The commit fence requires the receipt's exact
    `iat`+`materialization_generation` against this row and a non-ambiguous state. It is
    Hive-owned response state (not bound into request/rk2), and it is durable so the
    high-water is authoritative across Pulse replicas."""
    producer_generation_id = models.CharField(max_length=128)
    organization_id = models.PositiveIntegerField()
    subject = models.CharField(max_length=254)  # canonical (lowercased) subject email
    team_slug = models.CharField(max_length=100, blank=True, default='')
    iat = models.PositiveBigIntegerField()
    materialization_generation = models.CharField(max_length=128)
    # Latched TRUE when two responses share an `iat` but disagree on
    # `materialization_generation`; an ambiguous seat HOLDs until a strictly-newer
    # `iat` resolves it.
    ambiguous = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_substrate_materialization_high_water'
        constraints = [
            models.UniqueConstraint(
                fields=['producer_generation_id', 'organization_id', 'subject', 'team_slug'],
                name='uq_substrate_matgen_seat'),
        ]


class SubstrateWriteReceipt(models.Model):
    """HIVE-1153 P1 #3 (HIVE-1174 Security decision (b)): a transaction-local, one-use PG
    receipt consumed by the substrate write-guard trigger for every concrete-owner
    authorizing write of an Item under ENFORCE, so a QuerySet.update / raw SQL owner or
    requirement-input write cannot bypass the ORM's signed-evidence commit fence.

    Not a mutable per-item marker: each write arms exactly one receipt binding the exact
    before/after item tuple + owner + org/project/team/workflow/status + assignment
    generation + requirement key/vector + mode + expiry; the DB trigger requires and
    CONSUMES it in the same transaction (append-only; replay/cross-item reuse fail
    closed). The ORM arms it under the Item->Workflow locks — for a substrate-required
    outcome only after signed receipt revalidation, and for a non-substrate outcome
    unconditionally so ordinary writes still pass under enforce."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Nullable so a BigAutoField Item INSERT can pre-arm the receipt before its pk
    # exists; the AFTER INSERT guard atomically backfills item_id = NEW.id + consumes.
    item = models.ForeignKey('Item', on_delete=models.PROTECT, null=True, blank=True,
                             related_name='substrate_write_receipts')
    organization_id = models.PositiveIntegerField()
    project_id = models.PositiveBigIntegerField(null=True, blank=True)
    assignment_group = models.CharField(max_length=100, blank=True, default='')
    workflow_name = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=64, blank=True, default='')
    # exact NEW owner this write installs (the guard binds the post-write owner)
    subject_id = models.PositiveBigIntegerField(null=True, blank=True)
    assignee_email = models.EmailField(blank=True, default='')
    assignment_generation = models.PositiveBigIntegerField(default=0)
    # Hive accepts the frozen typed RequirementKey up to 512 bytes. The exact
    # golden rk2 is already 182 characters, so the receipt must preserve that
    # authority string without a PostgreSQL width failure before the trigger can
    # consume it.
    requirement_key = models.CharField(max_length=512, blank=True, default='')
    # sha256 over the sorted required-substrate-capability vector + the write-input
    # digest (labels/tags/metadata/title/description) the Python resolver consumes.
    write_input_digest = models.CharField(max_length=64)
    mode = models.CharField(max_length=16, default='enforce')
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_substrate_write_receipt'
        indexes = [models.Index(fields=['item', 'consumed_at', 'expires_at'],
                                name='substrate_write_consume_idx')]


class RoutingAuthorityQuarantine(models.Model):
    """Durable poison-event evidence; retries cannot erase the HOLD."""
    consumer = models.CharField(max_length=100)
    authority_kind = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    raw_event = models.JSONField(default=dict)
    reason = models.CharField(max_length=100)
    dedupe_key = models.CharField(max_length=64, unique=True)
    recovered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_authority_quarantine'


class RoutingAuthorityHoldSignal(models.Model):
    """Deduplicated observable HOLD plus distributed reconcile/backoff state."""
    authority_id = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    checkpoint_generation = models.PositiveBigIntegerField(default=0)
    reason = models.CharField(max_length=100)
    owner_group = models.CharField(max_length=64, default='devops')
    dedupe_key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    in_flight = models.BooleanField(default=False, db_index=True)
    in_flight_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    notified_at = models.DateTimeField(null=True, blank=True)
    notification_recipient = models.EmailField(blank=True, default='')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_routing_authority_hold_signal'
        indexes = [models.Index(
            fields=['resolved_at', 'in_flight', 'next_attempt_at'],
            name='routing_hold_ready_idx')]


class RoutingAuthorityCommand(models.Model):
    """Payload-bound maker/checker command envelope and idempotency ledger."""
    STATE_PROPOSED = 'proposed'
    STATE_APPLIED = 'applied'
    STATE_REJECTED = 'rejected'
    STATE_CHOICES = [(STATE_PROPOSED, 'Proposed'), (STATE_APPLIED, 'Applied'),
                     (STATE_REJECTED, 'Rejected')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command_type = models.CharField(max_length=64)
    organization_id = models.PositiveIntegerField(db_index=True)
    aggregate_key = models.CharField(max_length=255)
    expected_version = models.PositiveBigIntegerField(default=0)
    normalized_payload = models.JSONField(default=dict)
    payload_digest = models.CharField(max_length=64)
    idempotency_key = models.UUIDField(unique=True)
    maker_id = models.PositiveBigIntegerField()
    maker_email = models.EmailField()
    maker_authority_digest = models.CharField(max_length=64)
    checker_id = models.PositiveBigIntegerField(null=True, blank=True)
    checker_email = models.EmailField(blank=True, default='')
    checker_authority_digest = models.CharField(max_length=64, blank=True, default='')
    reason = models.TextField()
    approval_expires_at = models.DateTimeField()
    authority_generation = models.PositiveBigIntegerField(default=1)
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_PROPOSED)
    result = models.JSONField(default=dict, blank=True)
    result_digest = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'tasks_routing_authority_command'


class RoutingAuthorityOutbox(models.Model):
    """Durable at-least-once domain outbox."""
    origin_event_id = models.UUIDField(unique=True)
    authority_kind = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    version = models.PositiveBigIntegerField()
    digest = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    tombstone = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    in_flight = models.BooleanField(default=False, db_index=True)
    in_flight_until = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    quarantined_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_authority_outbox'
        constraints = [models.UniqueConstraint(
            fields=['authority_kind', 'aggregate_key', 'version'],
            name='uniq_routing_outbox_version')]
        indexes = [models.Index(fields=['published_at', 'quarantined_at'],
                                name='routing_outbox_pending_idx')]


class ItemStateEventOutbox(models.Model):
    """PLS-467 append-only occurrence for one Item state generation.

    Mutation + generation bump + this row share one database transaction.  A
    unique ``(item, state_generation)`` key is the cardinality floor, while the
    stable event UUID survives relay retries and later Connect delivery.
    """

    DISPOSITION_CHOICES = [
        ('pending', 'Pending'),
        ('applied', 'Applied'),
        ('stale', 'Stale'),
        ('quarantined', 'Quarantined'),
        # PLS-1310: the owning item was hard-deleted. The row is detached
        # (item_id NULL) and kept purely as forensic record — never relayed,
        # never drained, never counted as live stream state.
        ('orphaned', 'Orphaned'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PLS-1310: SET_NULL (not CASCADE) so the tombstoned outbox stream survives
    # the item's deletion as forensic record. Rows are tombstoned to
    # disposition='orphaned' by the pre_delete receiver in tasks.signals BEFORE
    # the item row goes; the FK then detaches them. The raw/SQL delete path can
    # still orphan rows (it bypasses the ORM entirely) — those are caught by the
    # tombstone_orphaned_outbox management command / deploy backfill.
    item = models.ForeignKey(
        'Item', on_delete=models.SET_NULL, null=True, related_name='state_event_outbox'
    )
    mutation_token = models.UUIDField(unique=True)
    state_generation = models.PositiveBigIntegerField()
    changed_kinds = models.JSONField(default=list)
    envelope = models.JSONField(default=dict)
    writer_kind = models.CharField(max_length=32, default='typed')
    disposition = models.CharField(
        max_length=16, choices=DISPOSITION_CHOICES, default='pending', db_index=True
    )
    stale_reason = models.CharField(max_length=64, blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_item_state_event_outbox'
        constraints = [models.UniqueConstraint(
            fields=['item', 'state_generation'],
            name='uniq_item_state_event_generation',
        )]
        indexes = [models.Index(
            fields=['disposition', 'next_attempt_at'],
            name='item_state_event_ready_idx',
        )]


class ItemStateMutationMarker(models.Model):
    """Transaction-local typed-writer proof checked by a deferred trigger.

    The row is inserted before the marked Item UPDATE and deleted by the
    commit-time constraint trigger only after the exact typed outbox row exists.
    It therefore never survives a successful transaction.
    """

    mutation_token = models.UUIDField(primary_key=True, editable=False)
    item = models.ForeignKey('Item', on_delete=models.CASCADE)
    state_generation = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_item_state_mutation_marker'


# =============================================================================
# PLS-733 / ADR-0056 + ADR-0064 — external wait authority and bindings
# =============================================================================

class ExternalWaitAuthority(models.Model):
    """Status-level proof that one exact Item generation may be parked.

    The row is deliberately separate from editable Item metadata.  PostgreSQL
    and the model writer both require a matching armed row before an Item may
    enter or leave ``waiting_external``; workflow editors and generic PATCHes
    therefore cannot manufacture the non-actionable state.
    """

    BRANCH_HUMAN = 'human_escalation_v1'
    BRANCH_CROSS_ORG = 'cross_org_task_v1'
    # PLS-962 / ADR-0064 v2: bounded external-party response window. This is
    # the third member of the closed waiting_external admission union; it is
    # deliberately not a new workflow status.
    BRANCH_EXTERNAL_RESPONSE = 'external_response_hold_v1'
    # PLS-842 / BKSD-14 §7.2: migrated org-isolation capsule whose evidence is
    # not producible inside the local organization.
    BRANCH_MIGRATION_CAPSULE = 'migration_capsule_v1'
    BRANCH_CHOICES = [
        (BRANCH_HUMAN, 'Human escalation v1'),
        (BRANCH_CROSS_ORG, 'Cross-org task v1'),
        (BRANCH_EXTERNAL_RESPONSE, 'External response hold v1'),
        (BRANCH_MIGRATION_CAPSULE, 'Migration capsule v1'),
    ]
    STATE_ENTER_ARMED = 'enter_armed'
    STATE_WAITING = 'waiting'
    STATE_EXIT_ARMED = 'exit_armed'
    STATE_RELEASED = 'released'
    STATE_REVOKED = 'revoked'
    STATE_CHOICES = [
        (STATE_ENTER_ARMED, 'Entry armed'),
        (STATE_WAITING, 'Waiting'),
        (STATE_EXIT_ARMED, 'Exit armed'),
        (STATE_RELEASED, 'Released'),
        (STATE_REVOKED, 'Revoked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='external_wait_authorities'
    )
    organization_id = models.PositiveIntegerField(db_index=True)
    wait_generation = models.PositiveBigIntegerField()
    branch = models.CharField(max_length=40, choices=BRANCH_CHOICES)
    state = models.CharField(max_length=20, choices=STATE_CHOICES)
    receipt_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pre_status = models.CharField(max_length=50)
    pre_workflow_status = models.CharField(max_length=100, blank=True, default='')
    pre_assignee_id = models.IntegerField(null=True, blank=True)
    pre_assignee_email = models.EmailField(blank=True, default='')
    pre_assignment_group = models.CharField(max_length=100, blank=True, default='')
    evidence = models.JSONField(default=dict, blank=True)
    checkpoint_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_evidence_digest = models.CharField(max_length=64, blank=True, default='')
    display_reference = models.JSONField(default=dict, blank=True)
    requested_by_id = models.PositiveBigIntegerField(null=True, blank=True)
    requested_by_email = models.EmailField(blank=True, default='')
    revalidate_after = models.DateTimeField(null=True, blank=True, db_index=True)
    last_revalidated_at = models.DateTimeField(null=True, blank=True)
    entered_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_external_wait_authority'
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'wait_generation'],
                name='uniq_external_wait_generation',
            ),
        ]
        indexes = [models.Index(
            fields=['state', 'revalidate_after'], name='ext_wait_revalidate_idx'
        )]


class ExternalResponseHoldWake(models.Model):
    """Durable at-least-once wake envelope for ADR-0064 branch 3.

    A wake is committed before Celery is touched. The reducer locks the
    authority and carrier, fences on wait_generation, and records the terminal
    disposition here, so duplicate broker deliveries are harmless.
    """

    KIND_EVIDENCE = 'external_evidence_v1'
    KIND_CHECKPOINT = 'checkpoint_due_v1'
    KIND_REVOKE = 'revoke_v1'
    KIND_CHOICES = [
        (KIND_EVIDENCE, 'External evidence'),
        (KIND_CHECKPOINT, 'Checkpoint due'),
        (KIND_REVOKE, 'Revoke'),
    ]
    STATE_PENDING = 'pending'
    STATE_APPLIED = 'applied'
    STATE_STALE = 'stale'
    STATE_REFUSED = 'refused'
    STATE_CONFLICT = 'conflict'
    STATE_CHOICES = [
        (STATE_PENDING, 'Pending'),
        (STATE_APPLIED, 'Applied'),
        (STATE_STALE, 'Stale'),
        (STATE_REFUSED, 'Refused'),
        (STATE_CONFLICT, 'Conflict'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    authority = models.ForeignKey(
        ExternalWaitAuthority, on_delete=models.PROTECT,
        related_name='external_response_wakes',
    )
    organization_id = models.PositiveIntegerField(db_index=True)
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='external_response_wakes'
    )
    wait_generation = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    correlation_key = models.CharField(max_length=128)
    actor_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor_email = models.EmailField(blank=True, default='')
    service_identity = models.CharField(max_length=128, blank=True, default='')
    evidence_digest = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=STATE_PENDING, db_index=True,
    )
    reason_code = models.CharField(max_length=80, blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True, default='')
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_external_response_hold_wake'
        constraints = [models.UniqueConstraint(
            fields=['authority', 'correlation_key'],
            name='uniq_ext_response_wake_correlation',
        )]
        indexes = [models.Index(
            fields=['state', 'next_attempt_at'], name='ext_resp_wake_ready_idx',
        )]


class ExternalResponseHoldAudit(models.Model):
    """Operational, redacted audit ledger for branch-3 decisions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    authority = models.ForeignKey(
        ExternalWaitAuthority, on_delete=models.PROTECT,
        related_name='external_response_audit',
    )
    organization_id = models.PositiveIntegerField(db_index=True)
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='external_response_audit'
    )
    wait_generation = models.PositiveBigIntegerField()
    event_type = models.CharField(max_length=32)
    correlation_key = models.CharField(max_length=128)
    actor_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor_email = models.EmailField(blank=True, default='')
    service_identity = models.CharField(max_length=128, blank=True, default='')
    evidence_digest = models.CharField(max_length=64, blank=True, default='')
    before_state = models.JSONField(default=dict)
    after_state = models.JSONField(default=dict)
    reason_code = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = 'tasks_external_response_hold_audit'
        constraints = [models.UniqueConstraint(
            fields=['authority', 'event_type', 'correlation_key'],
            name='uniq_ext_response_audit_event',
        )]
        indexes = [models.Index(
            fields=['organization_id', 'item', 'created_at'],
            name='ext_resp_audit_reader_idx',
        )]


class ExternalResponseHoldAuditArchive(models.Model):
    """Seven-year logical archive copy of one redacted operational audit row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_audit_id = models.UUIDField(unique=True)
    organization_id = models.PositiveIntegerField(db_index=True)
    item_id_snapshot = models.PositiveBigIntegerField()
    envelope = models.JSONField(default=dict)
    source_created_at = models.DateTimeField()
    archived_at = models.DateTimeField(auto_now_add=True)
    retain_until = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'tasks_external_response_hold_audit_archive'

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError('external_response_audit_archive_is_append_only')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('external_response_audit_archive_is_append_only')


class ExternalDependencyBinding(models.Model):
    """Opaque bilateral authority for one cross-organization dependency."""

    STATE_PREPARED = 'prepared'
    STATE_ACTIVE = 'active'
    STATE_RELEASED = 'released'
    STATE_REVOKED = 'revoked'
    STATE_STALE = 'stale'
    STATE_CHOICES = [
        (STATE_PREPARED, 'Prepared'),
        (STATE_ACTIVE, 'Active'),
        (STATE_RELEASED, 'Released'),
        (STATE_REVOKED, 'Revoked'),
        (STATE_STALE, 'Stale'),
    ]
    POLICY_TERMINAL = 'provider_terminal'
    POLICY_TERMINAL_OR_READY = 'provider_terminal_or_explicit_ready'
    POLICY_CHOICES = [
        (POLICY_TERMINAL, 'Provider terminal'),
        (POLICY_TERMINAL_OR_READY, 'Provider terminal or explicit ready'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    authority = models.ForeignKey(
        ExternalWaitAuthority, null=True, blank=True, on_delete=models.PROTECT,
        related_name='bindings',
    )
    consumer_item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='external_dependency_bindings'
    )
    consumer_organization_id = models.PositiveIntegerField(db_index=True)
    wait_generation = models.PositiveBigIntegerField()
    consumer_state_generation = models.PositiveBigIntegerField()
    token_hash = models.CharField(max_length=64, unique=True)
    token_expires_at = models.DateTimeField(db_index=True)
    token_consumed_at = models.DateTimeField(null=True, blank=True)
    provider_item = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.PROTECT,
        related_name='outbound_external_dependency_bindings',
    )
    provider_organization_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    release_policy = models.CharField(max_length=50, choices=POLICY_CHOICES)
    state = models.CharField(
        max_length=20, choices=STATE_CHOICES, default=STATE_PREPARED, db_index=True
    )
    local_display_reference = models.CharField(max_length=200, blank=True, default='')
    prepared_by_id = models.PositiveBigIntegerField(null=True, blank=True)
    prepared_by_email = models.EmailField(blank=True, default='')
    accepted_by_id = models.PositiveBigIntegerField(null=True, blank=True)
    accepted_by_email = models.EmailField(blank=True, default='')
    provider_release_generation = models.PositiveBigIntegerField(default=0)
    release_recorded_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_external_dependency_binding'
        constraints = [models.UniqueConstraint(
            fields=['consumer_item', 'wait_generation', 'provider_item'],
            condition=models.Q(provider_item__isnull=False),
            name='uniq_external_wait_provider',
        )]
        indexes = [
            models.Index(fields=['state', 'token_expires_at'], name='ext_binding_ready_idx'),
            models.Index(fields=['provider_item', 'state'], name='ext_binding_provider_idx'),
        ]


class ExternalDependencyReleaseEvent(models.Model):
    """Durable at-least-once provider release occurrence."""

    KIND_TERMINAL = 'provider_terminal'
    KIND_EXPLICIT = 'explicit_ready'
    KIND_CHOICES = [(KIND_TERMINAL, 'Provider terminal'), (KIND_EXPLICIT, 'Explicit ready')]
    STATE_PENDING = 'pending'
    STATE_APPLIED = 'applied'
    STATE_STALE = 'stale'
    STATE_CHOICES = [
        (STATE_PENDING, 'Pending'), (STATE_APPLIED, 'Applied'), (STATE_STALE, 'Stale')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    binding = models.ForeignKey(
        ExternalDependencyBinding, on_delete=models.PROTECT, related_name='release_events'
    )
    release_generation = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=STATE_PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_external_dependency_release'
        constraints = [models.UniqueConstraint(
            fields=['binding', 'release_generation', 'kind'],
            name='uniq_external_release_event',
        )]
        indexes = [models.Index(
            fields=['state', 'next_attempt_at'], name='ext_release_ready_idx'
        )]


# =============================================================================
# PLAT-4609 — typed escalation routing, immutable receipts and delivery outbox
# =============================================================================

class EscalationRoutingNotificationOutbox(models.Model):
    """Durable owner-attention delivery for one escalation disposition.

    The row is deliberately independent of the mutable Item fields.  It is
    written in the same transaction as the routing disposition and claimed by
    a bounded, idempotent Celery relay after commit.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='escalation_notification_outbox'
    )
    disposition_digest = models.CharField(max_length=64)
    recipients = models.JSONField(default=list)
    payload = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    in_flight = models.BooleanField(default=False, db_index=True)
    in_flight_until = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_escalation_notification_outbox'
        constraints = [models.UniqueConstraint(
            fields=['item', 'disposition_digest'],
            name='uniq_escalation_notice_disposition',
        )]
        indexes = [models.Index(
            fields=['delivered_at', 'failed_at', 'next_attempt_at'],
            name='escalation_notice_ready_idx',
        )]


# =============================================================================
# PLS-482 — crash-safe publication binding saga
# =============================================================================

class PublicationConsumedRound(models.Model):
    """PLS-482 P1 #2 / HLD v23 §17.4.5 — a review round retired by a stale hold.

    A ``stale_publication_hold`` atomically consumes its review-round ID; no
    later publication attempt (even under a fresh operation key) may reuse a
    consumed ``(item, review_round_id)`` pair. Fresh Wiki versions/hashes require
    a fresh review round and a new attempt.

    Per Sora's binding PLS-482 P1 #1 ruling (HLD v23 §17), PLS-482 does NOT hold
    a local artifact-integrity record: PLAT-4595 is a separate, unimplemented
    provider. Wiki-only bindings carry ``artifact_authority_ref = null`` and
    artifact-bearing paths fail closed until that provider exists — so the
    forbidden ``PublicationArtifactRecord`` parallel store is removed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.CASCADE, related_name='publication_consumed_rounds'
    )
    review_round_id = models.CharField(max_length=128)
    consumed_by_attempt = models.UUIDField()
    reason = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_publication_consumed_round'
        constraints = [models.UniqueConstraint(
            fields=['item', 'review_round_id'], name='uniq_pub_consumed_round'
        )]


class PublicationAttempt(models.Model):
    # HLD v23 §17.3 state machine:
    #   PREPARED -> ARTIFACT_NOT_REQUIRED | ARTIFACT_PREPARED
    #            -> SNAPSHOT_COMMITTED -> PUBLISHED -> DELIVERED
    # Terminal holds never rewrite PUBLISHED/DELIVERED (monotonic; P1 #3).
    STATE_CHOICES = [
        ('prepared', 'Prepared'),
        ('artifact_not_required', 'Artifact Not Required'),
        ('artifact_prepared', 'Artifact Prepared'),
        ('snapshot_committed', 'Snapshot Committed'),
        ('published', 'Published'),
        ('delivered', 'Delivered'),
        ('artifact_authority_unavailable_hold', 'Artifact Authority Unavailable Hold'),
        ('artifact_hold', 'Artifact Hold'),
        ('stale_publication_hold', 'Stale Publication Hold'),
        ('carrier_conflict_hold', 'Carrier Conflict Hold'),
        ('aborted', 'Aborted'),
    ]

    # Server-classified artifact policy (§17.1): the caller cannot choose it and
    # never supplies artifact authority through transition screen data.
    ARTIFACT_POLICY_CHOICES = [
        ('not_required', 'Wiki-only (no runtime artifact)'),
        ('bearing', 'Artifact-bearing (requires PLAT-4595 provider)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation_key = models.CharField(max_length=128, unique=True)
    request_digest = models.CharField(max_length=64)
    item = models.ForeignKey('Item', on_delete=models.PROTECT, related_name='publication_attempts')
    transition_id = models.UUIDField()
    expected_item_updated_at = models.DateTimeField()
    expected_status = models.CharField(max_length=50)
    review_round_id = models.CharField(max_length=128)
    publication_result = models.CharField(max_length=100)
    artifact_policy = models.CharField(max_length=20, choices=ARTIFACT_POLICY_CHOICES)
    # Opaque canonical PLAT-4595 provider reference (descriptor/intent/prepared-
    # receipt identity + immutable version/digest). Explicit null for a
    # server-classified Wiki-only publication. PLS-482 stores/fences this
    # reference only — it never copies or re-derives the provider's proof.
    artifact_authority_ref = models.JSONField(null=True, blank=True)
    expected_pages = models.JSONField(default=list)
    state = models.CharField(max_length=40, choices=STATE_CHOICES, default='prepared', db_index=True)
    wiki_snapshot_id = models.UUIDField(null=True, blank=True)
    snapshot_digest = models.CharField(max_length=64, blank=True, default='')
    snapshot_pages = models.JSONField(default=list, blank=True)
    failure_code = models.CharField(max_length=100, blank=True, default='')
    failure_evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_publication_attempt'
        indexes = [models.Index(fields=['item', 'state'], name='pub_attempt_item_state_idx')]


class PublicationBinding(models.Model):
    """Immutable published tuple; one terminal binding per attempt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt = models.OneToOneField(
        PublicationAttempt, on_delete=models.PROTECT, related_name='binding'
    )
    item = models.ForeignKey('Item', on_delete=models.PROTECT, related_name='publication_bindings')
    publication_snapshot_id = models.UUIDField()
    binding_digest = models.CharField(max_length=64, unique=True)
    binding_tuple = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_publication_binding'
        constraints = [models.UniqueConstraint(
            fields=['item', 'publication_snapshot_id'], name='uniq_pub_item_snapshot'
        )]


class PublicationOutbox(models.Model):
    """One logical downstream notification committed with a binding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    binding = models.OneToOneField(
        PublicationBinding, on_delete=models.PROTECT, related_name='outbox'
    )
    logical_event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    recipient_user_id = models.PositiveBigIntegerField()
    payload = models.JSONField(default=dict)
    notification = models.OneToOneField(
        'Notification', null=True, blank=True, on_delete=models.PROTECT,
        related_name='publication_outbox',
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_publication_outbox'
        indexes = [models.Index(
            fields=['delivered_at', 'next_attempt_at'], name='pub_outbox_ready_idx'
        )]


class HeldPlanTarget(models.Model):
    """PLS-481 / PLS-634: indexed per-team cursor for event-driven activation.

    One row per (item, target_team_id) that has at least one eligible slice
    remaining. Created by Accept — Capacity-Held; advanced atomically on each
    activation event; deleted when all slices are terminal. The
    ``next_eligible_slice`` cursor ensures a capacity event handles only the
    scoped, eligible held items — never a disguised full scan.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(
        'Item', on_delete=models.CASCADE, related_name='held_plan_targets'
    )
    target_team_id = models.CharField(max_length=100)
    next_eligible_slice = models.PositiveIntegerField(default=0)
    plan_generation = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_held_plan_target'
        constraints = [models.UniqueConstraint(
            fields=['item', 'target_team_id', 'plan_generation'],
            name='uniq_held_plan_target'
        )]


class EscalationRoutingDisposition(models.Model):
    """Append-only authoritative mutation result for typed escalation routing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.PositiveIntegerField(db_index=True)
    project = models.ForeignKey(
        'Project', on_delete=models.PROTECT, related_name='escalation_dispositions'
    )
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='escalation_dispositions'
    )
    transition_id = models.UUIDField()
    source_generation = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    disposition_digest = models.CharField(max_length=64)
    schema_version = models.CharField(max_length=80)
    classification = models.CharField(max_length=80)
    outcome = models.CharField(max_length=40)
    reason_code = models.CharField(max_length=100)
    blocker_dependency_key = models.CharField(max_length=64, blank=True, default='')
    blocker_result = models.CharField(max_length=20, blank=True, default='', db_index=True)
    normalized_evidence = models.JSONField(default=dict)
    pre_state = models.JSONField(default=dict)
    final_state = models.JSONField(default=dict)
    child_item = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.PROTECT,
        related_name='originating_escalation_dispositions',
    )
    link_ids = models.JSONField(default=list, blank=True)
    catalog_service_ref = models.CharField(max_length=200, blank=True, default='')
    catalog_generation = models.PositiveBigIntegerField(null=True, blank=True)
    notification_outbox_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_escalation_routing_disposition'
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'source_generation', 'disposition_digest'],
                name='uniq_escalation_disposition_input',
            ),
            models.UniqueConstraint(
                fields=['item', 'generation'],
                name='uniq_escalation_disposition_gen',
            ),
            models.CheckConstraint(
                check=models.Q(generation__gt=0),
                name='escalation_disposition_gen_gt_0',
            ),
        ]


class EscalationBlockerDependency(models.Model):
    """Org-scoped serialization point for one active operator dependency ask."""

    organization_id = models.PositiveIntegerField(db_index=True)
    dependency_key = models.CharField(max_length=64)
    target_team = models.CharField(max_length=100)
    blocker = models.ForeignKey(
        'Item', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='canonical_escalation_dependencies',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_escalation_blocker_dependency'
        constraints = [models.UniqueConstraint(
            fields=['organization_id', 'dependency_key'],
            name='uniq_escalation_blocker_dependency',
        )]


class EscalationRoutingReceipt(models.Model):
    """Append-only actor-attributed assertion receipt.

    A disposition may converge concurrent assertions from different actors,
    but each actor receives only their own receipt and provenance.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.PositiveIntegerField(db_index=True)
    project = models.ForeignKey(
        'Project', on_delete=models.PROTECT, related_name='escalation_receipts'
    )
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='escalation_receipts'
    )
    transition_id = models.UUIDField()
    actor_subject_id = models.CharField(max_length=255)
    actor_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor_email = models.EmailField(blank=True, default='')
    actor_assertion_digest = models.CharField(max_length=64)
    primary_request_id = models.UUIDField()
    source_generation = models.PositiveBigIntegerField()
    disposition = models.ForeignKey(
        EscalationRoutingDisposition, on_delete=models.PROTECT,
        related_name='actor_receipts',
    )
    classification = models.CharField(max_length=80)
    outcome = models.CharField(max_length=40)
    reason_code = models.CharField(max_length=100)
    final_state = models.JSONField(default=dict)
    replayed_disposition = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_escalation_routing_receipt'
        constraints = [models.UniqueConstraint(
            fields=[
                'item', 'actor_subject_id', 'source_generation',
                'actor_assertion_digest',
            ],
            name='uniq_escalation_actor_assertion',
        )]


class EscalationRoutingRequest(models.Model):
    """Immutable payload-bound transport request id ledger."""

    id = models.BigAutoField(primary_key=True)
    item = models.ForeignKey(
        'Item', on_delete=models.PROTECT, related_name='escalation_requests'
    )
    request_id = models.UUIDField()
    actor_assertion_digest = models.CharField(max_length=64)
    receipt = models.ForeignKey(
        EscalationRoutingReceipt, on_delete=models.PROTECT,
        related_name='request_bindings',
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_escalation_routing_request'
        constraints = [models.UniqueConstraint(
            fields=['item', 'request_id'], name='uniq_escalation_request_id'
        )]


class RoutingAuthorityConsumerState(models.Model):
    """Atomic consumer cursor/high-water for replay, gap and tombstone safety."""
    consumer = models.CharField(max_length=100)
    authority_kind = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    applied_version = models.PositiveBigIntegerField(default=0)
    applied_digest = models.CharField(max_length=64, blank=True, default='')
    tombstone_high_water = models.PositiveBigIntegerField(default=0)
    pending_gap_version = models.PositiveBigIntegerField(null=True, blank=True)
    hold_reason = models.CharField(max_length=100, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_routing_authority_consumer_state'
        constraints = [models.UniqueConstraint(
            fields=['consumer', 'authority_kind', 'aggregate_key'],
            name='uniq_routing_consumer_cursor')]


class RoutingAuthorityConsumerReceipt(models.Model):
    """Immutable per-version consumer evidence used for old duplicate checks."""
    consumer = models.CharField(max_length=100)
    authority_kind = models.CharField(max_length=64)
    aggregate_key = models.CharField(max_length=255)
    version = models.PositiveBigIntegerField()
    digest = models.CharField(max_length=64)
    tombstone = models.BooleanField(default=False)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_authority_consumer_receipt'
        constraints = [models.UniqueConstraint(
            fields=['consumer', 'authority_kind', 'aggregate_key', 'version'],
            name='uniq_routing_consumer_receipt')]


class RoutingDenialAudit(models.Model):
    """Content-free, access-controlled assignment denial breadcrumb."""
    item_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    organization_id = models.PositiveIntegerField(null=True, blank=True)
    routing_team_id = models.UUIDField(null=True, blank=True)
    subject_id = models.PositiveBigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=64, default='receive_assignment')
    reason = models.CharField(max_length=100)
    evidence = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tasks_routing_denial_audit'


class SubstrateShadowMetric(models.Model):
    """Durable bounded aggregate for off→shadow→enforce bake evidence.

    One row exists per sanitized (outcome, reason, mismatch) label tuple; task,
    subject and requirement-key detail stays in the deduplicated audit breadcrumb
    and never becomes metric cardinality. Atomic increments converge across all
    replicas and survive process restarts.
    """
    outcome = models.CharField(max_length=32)
    reason = models.CharField(max_length=100)
    mismatch = models.BooleanField(default=False)
    observations = models.PositiveBigIntegerField(default=0)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_substrate_shadow_metric'
        constraints = [models.UniqueConstraint(
            fields=['outcome', 'reason', 'mismatch'],
            name='uq_substrate_shadow_metric_labels')]


class TeamCapabilityProfile(models.Model):
    """Pulse SoT for a TEAM's conferred capability TAGS.

    Operator directive 2026-07-11: *"capabilities are fine up to Pulse … but the
    mapping for capability → skill / MCP server should be Hive's."* So the layers
    split cleanly:

    * **Pulse owns team → abstract capability TAGS** (``kubeconfig``,
      ``credential-broker``, ``github:merge`` …). This model is the queryable,
      DB-backed SoT for that policy — the durable replacement for the hardcoded
      ``WorkflowService._TEAM_CONFERRED_CAPABILITIES`` dict (which carried a
      "keep this in sync with the Hive baseline" manual-sync smell).
    * **Hive owns tag → concrete resources** (skills, MCP servers, credential
      scopes). Hive reads this table via ``/api/internal/team-capabilities/``
      and maps each tag → resources on its end; it must NOT re-declare which
      teams are privileged.

    Keyed by canonical ``team_name`` (globally unique, matching TeamRoster) and
    org-agnostic: a tag confers the same policy for a team name in every org
    (e.g. ``devops`` always confers ``kubeconfig``). Team membership stays in
    TeamRoster; this holds only the team's capability policy.
    """
    team_name = models.CharField(max_length=64, unique=True, db_index=True)
    capabilities = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=128, blank=True, default='')

    class Meta:
        db_table = 'tasks_team_capability_profile'
        ordering = ['team_name']
        verbose_name = 'Team Capability Profile'
        verbose_name_plural = 'Team Capability Profiles'

    def __str__(self):
        return f'{self.team_name}: {self.capabilities}'


class AgentAvailability(models.Model):
    """Durable availability *signals* for Pulse routing — not fleet identity SoT.

    **Hive** is the source of truth for what a fleet agent *is* (model, execution
    method, enabled, pod identity). This table records:
      - **operator** force ON/OFF / on-leave (always honored for critical routing)
      - **self:** / **health-sweep** capacity latches (provider exhaustion, empty
        turns) which are *ignored* for assignment when live exporter health says
        the seat is UP again (stale self-latches must not create split-brain).

    Unlike ``pulse:agent_disabled`` (ephemeral Redis rewritten by a celery beat
    sweep), rows here persist across restarts. Operator OFF still drains
    in-flight work via ``_disabled_agent_emails``.

    Set via ``set_agent_availability`` (operator), bridge self-PATCH, or the
    health sweep. No row == no override == fall through to live health/env.
    """
    # PLS-742: single source of truth for the `updated_by` ownership sentinels.
    # Every code path that reads `updated_by` to decide who owns a row MUST
    # compare through ONE normalisation rule (strip + lower) instead of a
    # hand-typed literal, so a row can never look operator-owned to one path
    # and health-sweep-owned to another. Writers and readers reference these
    # constants, never re-type the string.
    ACTOR_OPERATOR = 'operator'
    ACTOR_HEALTH_SWEEP = 'health-sweep'
    ACTOR_SELF_PREFIX = 'self:'
    ACTOR_SELF_PROVIDER = 'self:provider'
    ACTOR_SELF_RUNTIME = 'self:runtime'

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('on_leave', 'On Leave'),
        ('disabled', 'Disabled'),
    ]
    email = models.EmailField(unique=True, db_index=True)
    active = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    reason = models.TextField(blank=True, default='')
    updated_by = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        super().save(*args, **kwargs)

    # -- PLS-742: ONE normalisation rule for `updated_by` ownership comparisons --
    # Every code path that reads `updated_by` to classify row ownership (drain
    # set, routing escape, reactivation loop) MUST go through these helpers so a
    # row stamped `Health-Sweep`, `HEALTH-SWEEP ` or `health-sweep` is owned by
    # the SAME actor on every path. Rule: strip surrounding whitespace + fold
    # case, both in Python and (via iregex) at the ORM boundary.

    @classmethod
    def norm_actor(cls, value):
        """Single normalised form of an `updated_by` value (strip + lower)."""
        return (value or '').strip().lower()

    @classmethod
    def is_actor(cls, value, *actors):
        """Python-side: does `value` equal any actor under the PLS-742 rule?
        Supports exact sentinels and the `self:` prefix family."""
        norm = cls.norm_actor(value)
        for a in actors:
            a = cls.norm_actor(a)
            if a.endswith(':'):
                if norm == a.rstrip(':') or norm.startswith(a):
                    return True
            elif norm == a:
                return True
        return False

    @classmethod
    def actor_q(cls, *actors):
        """ORM-side Q filter matching `updated_by` to any actor under the same
        rule (case-insensitive + surrounding-whitespace-insensitive)."""
        q = Q()
        for a in actors:
            a = cls.norm_actor(a)
            if a.endswith(':'):
                q |= Q(updated_by__iregex=r'^\s*' + re.escape(a.rstrip(':')) + r'\s*$') | \
                     Q(updated_by__iregex=r'^\s*' + re.escape(a) + r'.*$')
            else:
                q |= Q(updated_by__iregex=r'^\s*' + re.escape(a) + r'\s*$')
        return q

    class Meta:
        db_table = 'tasks_agent_availability'
        verbose_name = 'Agent Availability'
        verbose_name_plural = 'Agent Availability'

    def __str__(self):
        return f'{self.email}: {"active" if self.active else self.status}'


class UserLifecycleEventReceipt(models.Model):
    """Idempotency ledger for ID -> Pulse user lifecycle cleanup events."""

    EVENT_DEACTIVATED = 'user.deactivated'
    EVENT_DELETED = 'user.deleted'
    EVENT_REACTIVATED = 'user.reactivated'
    EVENT_CHOICES = [
        (EVENT_DEACTIVATED, 'User deactivated'),
        (EVENT_DELETED, 'User deleted'),
        (EVENT_REACTIVATED, 'User reactivated'),
    ]

    event_id = models.UUIDField(unique=True, db_index=True)
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES, db_index=True)
    user_id = models.IntegerField(db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    items_unassigned = models.PositiveIntegerField(default=0)
    processed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-processed_at']
        indexes = [
            models.Index(fields=['event_type', 'user_id'], name='tasks_userl_event_t_9f9209_idx'),
        ]

    def __str__(self):
        return f'{self.event_type} user={self.user_id} event={self.event_id}'


# =============================================================================
# PLS-171 — live dashboard/task updates via backend-pushed deltas (Slice 1)
# =============================================================================
class TaskEvent(models.Model):
    """Append-only delta log for live Pulse frontend updates (PLS-171).

    Every Item / Comment / ItemLink mutation appends one row here, written from
    the same post_save/post_delete signals that already fire. The frontend polls
    ``GET /api/events?since=<seq>`` and patches its loaded dashboard / list /
    detail cache from the deltas instead of forcing a full refresh; ``seq`` (the
    pk) is the monotonic cursor — a client tracks its last-seen seq and asks for
    everything after it (Kafka-style, mirroring the daemon EventLog), so there is
    zero per-client state on the server and reconnect is a bounded
    ``?since=<last seq>`` resync.

    Slice 1 is a deployment-safe cursor-delta log (the Pulse backend is gunicorn
    WSGI-sync, so a true server-push transport — SSE/WS — is Slice 2; this log is
    the substrate that transport will reuse). Access is enforced at READ time by
    scoping to the projects the caller can see; ``org_id`` / ``project_id`` are
    denormalized so a delete event stays access-scopable after its Item row is
    gone.
    """
    KIND_ITEM_CREATED = 'item_created'
    KIND_ITEM_UPDATED = 'item_updated'
    KIND_ITEM_DELETED = 'item_deleted'
    KIND_COMMENT_ADDED = 'comment_added'
    KIND_LINK_CHANGED = 'link_changed'
    KIND_CHOICES = [
        (KIND_ITEM_CREATED, 'Item created'),
        (KIND_ITEM_UPDATED, 'Item updated'),
        (KIND_ITEM_DELETED, 'Item deleted'),
        (KIND_COMMENT_ADDED, 'Comment added'),
        (KIND_LINK_CHANGED, 'Link changed'),
    ]

    seq = models.BigAutoField(primary_key=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    # Denormalized scope keys — retained even after the Item is deleted so a
    # delete event is still access-scopable. project_id NULL = personal item.
    org_id = models.IntegerField(null=True, blank=True, db_index=True)
    project_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    item_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    item_key = models.CharField(max_length=64, blank=True, default='')
    actor_user_id = models.IntegerField(null=True, blank=True)
    # Compact delta the frontend applies (e.g. {'status': 'done'} / changed fields).
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'tasks_task_event'
        ordering = ['seq']
        indexes = [
            models.Index(fields=['project_id', 'seq']),
            models.Index(fields=['org_id', 'seq']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f'TaskEvent#{self.seq} {self.kind} item={self.item_key or self.item_id}'


# =============================================================================
# PLS-238 — CVQA-L7 QA Target Map-driven rotation cursor
# =============================================================================
class QaCadenceRotation(models.Model):
    """Persists the F1/F2 rotation cursor for CVQA-L7 target-map checklists.

    Each (project_key, tier) pair stores an integer cursor that advances by
    the slice size after every cadence run, wrapping at the total row count.
    This gives a deterministic rotating schedule without any per-task state.
    """
    project_key = models.CharField(max_length=50, db_index=True)
    tier = models.CharField(max_length=3)  # "F1" or "F2"
    cursor = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("project_key", "tier")]

    def __str__(self):
        return f'QaCadenceRotation {self.project_key}/{self.tier} cursor={self.cursor}'


class TaskDraftProposal(models.Model):
    """
    AI-generated task draft proposal (PLS-306 / PLS-270 Slice C).

    Stores a natural-language → structured task draft before confirmation.
    The pipeline: parse prompt → retrieve context → search duplicates →
    propose fields → validate → confirm/reject lifecycle.

    On confirm, creates a real Item and links it via confirmed_item.
    On reject, marks rejected_at and keeps the draft for audit.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Who requested it
    org_id = models.IntegerField(db_index=True)
    requester_id = models.IntegerField(null=True, blank=True)
    requester_email = models.EmailField(null=True, blank=True)
    source = models.CharField(
        max_length=50,
        default='mcp',
        help_text="Source of the request: 'mcp', 'api', 'ui'"
    )

    # Raw input
    raw_prompt = models.TextField(help_text="Original natural-language prompt")

    # Proposed fields (LLM output before validation)
    proposed_project_id = models.IntegerField(null=True, blank=True)
    proposed_workflow_slug = models.CharField(max_length=100, blank=True)
    proposed_title = models.CharField(max_length=500, blank=True)
    proposed_description = models.TextField(blank=True)
    proposed_priority = models.CharField(max_length=20, blank=True)
    proposed_assignment_group = models.CharField(max_length=100, blank=True)
    proposed_labels = models.JSONField(default=list, blank=True)
    proposed_tags = models.JSONField(default=list, blank=True)
    proposed_parent_item_id = models.CharField(max_length=50, blank=True)

    # Duplicate search results
    duplicate_candidates = models.JSONField(
        default=list, blank=True,
        help_text="List of {task_key, title, status, score} for likely duplicates"
    )

    # Validation
    validation_status = models.CharField(
        max_length=30,
        default='pending',
        choices=[
            ('pending', 'Pending'),
            ('valid', 'Valid'),
            ('invalid', 'Invalid - missing required fields'),
            ('duplicate', 'Duplicate found - not created'),
        ]
    )
    validation_errors = models.JSONField(default=list, blank=True)

    # Provenance
    prompt_hash = models.CharField(max_length=64, blank=True, help_text="SHA-256 of raw_prompt")
    model_used = models.CharField(max_length=100, blank=True)
    pipeline_version = models.CharField(max_length=20, default='1.0')

    # Lifecycle
    confirmed_item_id = models.CharField(
        max_length=50, null=True, blank=True,
        help_text="Item key (e.g., PROJ-123) of the created task, if confirmed"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_taskdraftproposal'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['org_id', 'validation_status'], name='taskdraft_org_valstatus_idx'),
            models.Index(fields=['requester_id'], name='taskdraft_requester_idx'),
        ]

    def __str__(self):
        return f'TaskDraftProposal {self.id} ({self.validation_status})'


class FocusedQAGeneration(models.Model):
    """Immutable, append-only focused-QA generation source of truth (PLS-819 HLD §2.1).

    One row per deployment/evidence generation. The focused-QA task is a
    protected one-to-one projection; the generation carries the immutable target
    snapshot and is the durable idempotency/audit boundary. Rows are appended
    only: once committed, neither update nor delete is permitted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Protected one-to-one reference to the focused-QA task (HLD §2.1).
    focused_qa_task = models.OneToOneField(
        Item, on_delete=models.PROTECT, related_name='focused_qa_generation',
    )
    originating_task = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name='focused_qa_generations',
    )
    owning_project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT,
        related_name='focused_qa_generations',
    )
    organization = models.CharField(max_length=64, blank=True, default='')
    environment = models.CharField(max_length=80, blank=True, default='')
    observed_deployment_event = models.ForeignKey(
        DeploymentEvent, null=True, blank=True, on_delete=models.PROTECT,
        related_name='focused_qa_generations',
    )
    observed_build = models.CharField(max_length=512, blank=True, default='')
    trigger_identity = models.CharField(max_length=512, blank=True, default='')
    generation_key = models.CharField(max_length=512, db_index=True)
    predecessor = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='successors',
    )
    origin_surface_schema = models.CharField(max_length=255, blank=True, default='')
    origin_surface_key = models.CharField(max_length=1024, blank=True, default='')
    # Hash of the immutable target snapshot; a reused generation_key with a
    # different target snapshot is a conflict (HLD §2.1).
    target_snapshot_hash = models.CharField(max_length=64, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = 'tasks_focused_qa_generation'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'originating_task', 'generation_key'],
                name='uniq_focused_qa_generation_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['originating_task', 'environment', 'created_at'],
                name='focused_qa_gen_origin_env_idx',
            ),
            models.Index(
                fields=['observed_deployment_event'],
                name='focused_qa_gen_deploy_idx',
            ),
        ]

    def __str__(self):
        return f'FocusedQAGeneration {self.generation_key} (@{self.focused_qa_task_id})'

    def delete(self, *args, **kwargs):
        raise TypeError('FocusedQAGeneration is append-only (HLD §2.2); delete is forbidden.')


class FocusedQABugEquivalenceKey(models.Model):
    """Canonical cross-generation bug-equivalence reservation (HLD §2.3).

    One row per full versioned equivalence tuple; the UNIQUE constraint over the
    tuple is the database serialization boundary for FAIL lookup-or-create.
    Append-only rows; delete forbidden.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.CharField(max_length=64, blank=True, default='')
    owning_project = models.CharField(max_length=64, blank=True, default='')
    origin_surface_schema = models.CharField(max_length=255, blank=True, default='')
    origin_surface_key = models.CharField(max_length=1024, blank=True, default='')
    failure_fingerprint_schema = models.CharField(max_length=255, blank=True, default='')
    canonical_failure_fingerprint_value = models.CharField(max_length=512, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = 'tasks_focused_qa_bug_equivalence_key'
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'organization', 'owning_project', 'origin_surface_schema',
                    'origin_surface_key', 'failure_fingerprint_schema',
                    'canonical_failure_fingerprint_value',
                ],
                name='uniq_focused_qa_bug_equivalence',
            ),
        ]

    def __str__(self):
        return f'FocusedQABugEquivalenceKey {self.organization}/{self.origin_surface_key}'

    def delete(self, *args, **kwargs):
        raise TypeError('FocusedQABugEquivalenceKey is append-only (HLD §2.3); delete is forbidden.')


class FocusedQADispositionReceipt(models.Model):
    """Append-only immutable disposition receipt (HLD §2.2).

    Written in the same transaction as the disposition transition. At most one
    terminal disposition (PASS/FAIL/SUBSUMED) per generation; correction receipts
    may be multiple and remain historical. Unique on (focused_qa_task,
    idempotency_key) arbitrates concurrent identical requests.
    """

    DISPOSITION_CHOICES = [
        ('pass', 'Pass QA Evidence'),
        ('fail', 'Confirm Product Failure'),
        ('correct', 'Return Evidence for Correction'),
        ('subsumed', 'Close Duplicate / Subsumed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    generation = models.ForeignKey(
        FocusedQAGeneration, on_delete=models.PROTECT, related_name='disposition_receipts',
    )
    focused_qa_task = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name='focused_qa_receipts',
    )
    disposition = models.CharField(max_length=24, choices=DISPOSITION_CHOICES)
    transition_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=512)
    canonical_payload_hash = models.CharField(max_length=64)
    target_snapshot_hash = models.CharField(max_length=64, blank=True, default='')
    resulting_status = models.CharField(max_length=64, blank=True, default='')
    resulting_resolution = models.CharField(max_length=64, blank=True, default='')
    activity_id = models.UUIDField(null=True, blank=True)
    product_bug = models.ForeignKey(
        Item, null=True, blank=True, on_delete=models.PROTECT,
        related_name='focused_qa_fail_receipts',
    )
    equivalence_key = models.ForeignKey(
        FocusedQABugEquivalenceKey, null=True, blank=True, on_delete=models.PROTECT,
        related_name='disposition_receipts',
    )
    replay_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = 'tasks_focused_qa_disposition_receipt'
        constraints = [
            models.UniqueConstraint(
                fields=['focused_qa_task', 'idempotency_key'],
                name='uniq_focused_qa_receipt_task_key',
            ),
            # Conditional one-terminal-receipt constraint (HLD §2.2): at most one
            # PASS/FAIL/SUBSUMED per generation.
            models.UniqueConstraint(
                fields=['generation'],
                condition=models.Q(disposition__in=['pass', 'fail', 'subsumed']),
                name='uniq_focused_qa_one_terminal_per_generation',
            ),
        ]
        indexes = [
            models.Index(
                fields=['generation', 'created_at'], name='focused_qa_receipt_gen_idx',
            ),
            models.Index(
                fields=['equivalence_key'], name='focused_qa_receipt_equiv_idx',
            ),
        ]

    def __str__(self):
        return f'FocusedQADispositionReceipt {self.disposition} for {self.focused_qa_task_id}'

    def delete(self, *args, **kwargs):
        raise TypeError('FocusedQADispositionReceipt is append-only (HLD §2.2); delete is forbidden.')


class MutationReceipt(models.Model):
    """Durable mutation idempotency receipt (PLAT-5343 HLD v12 §6.1).

    Uniquely keyed ``(principal_fingerprint, operation_name_and_version,
    idempotency_key)``. Stores the receipt UUID, authenticated issuer+subject
    fingerprint, operation, tenant/target references, caller key (≤128 B),
    SHA-256 request digest (RFC 8785 JCS over the validated semantic request
    after defaults + method/tool name + operation version + canonical target
    identifiers; transport-only fields, request ID, deadline, and the key
    itself excluded), state, HTTP/JSON-RPC result class, allowlisted redacted
    response (≤64 KiB) or durable result reference, timestamps, expiry, and
    an optional outbox operation ID.

    No token, assertion, raw authorization header, attachment body, or
    unbounded response is ever stored (§6.1 must-never).
    """

    class States(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMMITTED = 'committed', 'Committed'
        FENCING_UNPROVEN = 'fencing_unproven', 'Fencing Unproven'
        ABORTED_PROVEN = 'aborted_proven', 'Aborted Proven'

    receipt_uuid = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    principal_fingerprint = models.CharField(max_length=64, db_index=True)
    operation_name = models.CharField(max_length=128)
    operation_version = models.CharField(max_length=32, default='1')
    idempotency_key = models.CharField(max_length=128)
    tenant_ref = models.CharField(max_length=128, blank=True)
    target_ref = models.CharField(max_length=128, blank=True)
    caller_key = models.CharField(max_length=128, blank=True)
    request_digest = models.CharField(max_length=64)
    state = models.CharField(
        max_length=24, choices=States.choices, default=States.ACTIVE, db_index=True,
    )
    result_class = models.CharField(max_length=32, blank=True)
    redacted_response = models.TextField(blank=True)
    result_reference = models.CharField(max_length=256, blank=True)
    outbox_operation_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['principal_fingerprint', 'operation_name', 'operation_version',
                        'idempotency_key'],
                name='uniq_mutation_receipt_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['principal_fingerprint', 'state'],
                name='mutation_receipt_principal_idx',
            ),
            models.Index(fields=['expires_at'], name='mutation_receipt_expiry_idx'),
        ]

    def __str__(self):
        return f'MutationReceipt {self.receipt_uuid} {self.operation_name} {self.state}'

    def delete(self, *args, **kwargs):
        raise TypeError('MutationReceipt is append-only (HLD §6.1); delete is forbidden.')


class OperationOutbox(models.Model):
    """Transactional outbox row for effects outside the Pulse DB transaction
    (PLAT-5343 HLD v12 §6.3).

    The request atomically commits the MutationReceipt + this row and returns
    202/operation ID; the event-driven worker claims with a generation-fenced
    lease and sends a provider idempotency key derived from the receipt ID.
    Success/failure is recorded before a terminal operation result. Providers
    lacking idempotency keys/read-after-timeout reconciliation are
    ``async_only`` — never eligible for automated synchronous retry. The
    design never promises exactly-once from a local receipt alone.
    """

    class States(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CLAIMED = 'claimed', 'Claimed'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        ABANDONED = 'abandoned', 'Abandoned'

    operation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(
        MutationReceipt, on_delete=models.PROTECT, related_name='outbox_rows',
    )
    provider = models.CharField(max_length=64)
    provider_idempotency_key = models.CharField(max_length=128)
    payload_digest = models.CharField(max_length=64)
    state = models.CharField(
        max_length=16, choices=States.choices, default=States.PENDING, db_index=True,
    )
    result_class = models.CharField(max_length=32, blank=True)
    lease_generation = models.IntegerField(default=0)
    lease_owner = models.CharField(max_length=128, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    terminal_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['state', 'created_at'], name='operation_outbox_claim_idx'),
            models.Index(fields=['receipt'], name='operation_outbox_receipt_idx'),
        ]

    def __str__(self):
        return f'OperationOutbox {self.operation_id} {self.provider} {self.state}'

    def delete(self, *args, **kwargs):
        raise TypeError('OperationOutbox is append-only (HLD §6.3); delete is forbidden.')

class DeferredWorkflowCommand(models.Model):
    """Server-owned intent; task mutation and terminal receipt commit together.

    Pending commands retain the original active blocker. They contain no bearer
    token or cached grants. The worker re-reads identity and permissions before
    applying the complete graph transaction.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, null=True, on_delete=models.SET_NULL,
                             related_name='deferred_workflow_commands')
    organization_id = models.IntegerField(null=True)
    actor_id = models.IntegerField()
    actor_email = models.EmailField()
    external_token = models.ForeignKey(ExternalMcpToken, null=True, blank=True,
                                      on_delete=models.SET_NULL)
    kind = models.CharField(max_length=16)
    payload = models.JSONField(default=dict)
    source_generation = models.PositiveBigIntegerField()
    graph_digest = models.CharField(max_length=64)
    transition_digest = models.CharField(max_length=64, blank=True)
    request_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default='pending', db_index=True,
                             choices=[('pending', 'Pending'), ('succeeded', 'Succeeded'),
                                      ('failed', 'Failed'), ('stale', 'Stale')])
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['item', 'request_digest'], condition=models.Q(state='pending'),
            name='uniq_pending_workflow_command',
        )]


class SecurityGateState(models.Model):
    """PLS-458: server-owned durable record that a parent's STRIDE gate was
    satisfied by a successor topology rather than a direct Security sign-off.

    Created atomically during the transition that passes the STRIDE gate via
    the successor chain.  Guards against race deletion/reassignment of the
    load-bearing links after the parent terminalizes.

    ``status`` lifecycle::

        active → released (successor terminalized)
        active → failed   (gate violated — link deleted, successor reassigned)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent_item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='security_gate_states',
        help_text="The trust-boundary design item that passed the STRIDE gate via successor",
    )
    successor_item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='+',
        help_text="The Security-assigned successor that performs the fresh review",
    )
    anchor_item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='+', null=True, blank=True,
        help_text="The shared release/design anchor that both parent and successor block",
    )
    status = models.CharField(
        max_length=20, default='active',
        choices=[
            ('active', 'Active — gate is live'),
            ('released', 'Released — successor terminalized, gate satisfied'),
            ('failed', 'Failed — gate violated'),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_security_gate_state'
        indexes = [
            models.Index(fields=['parent_item', 'status'], name='secgate_parent_status_idx'),
            models.Index(fields=['successor_item', 'status'], name='secgate_successor_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['parent_item'],
                condition=models.Q(status='active'),
                name='uq_secgate_active_parent',
            ),
        ]

    def __str__(self):
        return (f'SecurityGateState({self.parent_item_id} → '
                f'{self.successor_item_id}, status={self.status})')


class ProviderWaitMarker(models.Model):
    """PLAT-10083: durable provider_wait queue-lane classification.

    Proof-derived like OperatorWaitMarker (PLS-527/529): the marker is set by
    the runtime writer at terminal-stop time keyed on the landed
    ``[stop_signature: <class>]`` bounded label set, and only the two provider
    classes (``upstream_unavailable``, ``quota_exhausted``) produce cluster-
    manager ``provider_wait`` visibility. Reversibility is explicit in both
    directions: auto-clear on the seat's next SUCCESSFUL heartbeat/turn
    (never on a failed attempt), or an explicit manager-action clear
    (take_over / redispatch / acknowledge). Visibility only — no actuation.
    """

    STATE_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]
    SIGNATURE_CHOICES = [
        ('upstream_unavailable', 'Upstream unavailable'),
        ('quota_exhausted', 'Quota exhausted'),
    ]
    CLEAR_ACTION_CHOICES = [
        ('take_over', 'Take over'),
        ('redispatch', 'Redispatch'),
        ('acknowledge', 'Acknowledge'),
    ]

    item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name='provider_wait_marker',
    )
    signature_class = models.CharField(max_length=40)
    stop_message_id = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default='active')
    generation = models.PositiveIntegerField(default=1)
    activated_at = models.DateTimeField(auto_now_add=True)
    last_set_at = models.DateTimeField(auto_now=True)
    auto_cleared_at = models.DateTimeField(null=True, blank=True)
    manager_cleared_at = models.DateTimeField(null=True, blank=True)
    manager_cleared_by = models.CharField(max_length=254, blank=True, default='')
    manager_cleared_action = models.CharField(
        max_length=20, choices=CLEAR_ACTION_CHOICES, blank=True, default='',
    )
    # PLAT-9836 legibility: the failover sweep reads (and records, not sweeps
    # around) active markers via provider_wait_visible_qs().
    sweep_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tasks_provider_wait_marker'
        indexes = [
            models.Index(
                fields=['state', 'signature_class'],
                name='tasks_provw_state_9d1c44_idx',
            ),
        ]

    def __str__(self):
        return (f'ProviderWaitMarker({self.item_id}, {self.signature_class}, '
                f'{self.state})')
