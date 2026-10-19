from tasks.services.service_signing import configured_service_signing_key
from django.shortcuts import render, get_object_or_404
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import (
    PermissionDenied as DjangoPermissionDenied,
    ValidationError as DjangoValidationError,
)
from django.utils.http import content_disposition_header
from rest_framework import viewsets, status, permissions, serializers as drf_serializers
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.db.models import Q, Count, Case, When, Value, IntegerField, F, Max, OuterRef, Subquery
from django.db.models.functions import Coalesce, JSONObject
from django.db.models.fields.json import KeyTextTransform
from django.db import IntegrityError, OperationalError, transaction
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
import requests

from .fleet_read_authentication import HiveFleetReadAuthentication, is_hive_fleet_reader
from .workflow_articles import normalize_knowledge_articles
from .label_utils import InvalidLabelFilter, item_labels_match_q, parse_label_filter
from .models import (
    TaskEvent,  # PLS-171: live-update delta log
    CustomerRequest, CustomerRequestVote, CustomerRequestComment,
    DeploymentEvent, VerificationRequirementManifest, DeploymentEvidence,
    Objective, KeyResult,
    Item, Task,  # Task is alias for Item
    ApprovalRule, SilenceRule, EscalationRule, InhibitionRule,
    Subscription, FingerprintConfig, UserNotificationChannel,
    Notification,
    Project, ProjectMembership, AgentProjectMembership, Sprint,
    ProjectTeamLead,
    Comment, WebhookKey,
    Attachment, ItemActivity, ItemLink, ItemWatcher, CommentReaction,
    # Phase 3 models
    Component, Version, WorkLog,
    ProjectWorkflow, WorkflowTransition,
    BoardColumn, SavedFilter,
    # Workflow models
    Status, Resolution, Workflow, WorkflowStatus,
    WorkflowScheme, WorkflowSchemeMapping,
    # Issue types, screens, permissions, SLA models
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
    TeamRoster, RoutingAuthorityCommand, UserLifecycleEventReceipt,
    ContainmentDecision,
    ExternalMcpClient, ExternalMcpToken, ExternalMcpCallAudit,
    TaskDraftProposal,
    AiGenerationRun,
)
from .serializers import (
    ItemSerializer, ItemCreateSerializer, ItemUpdateSerializer,
    ObjectiveSerializer, KeyResultSerializer,
    DeploymentEventSerializer, DeploymentEventIngestSerializer,
    VerificationRequirementManifestSerializer, VerificationRequirementManifestCreateSerializer,
    DeploymentEvidenceSerializer, DeploymentEvidenceCreateSerializer,
    DeploymentEvidenceInvalidateSerializer,
    ItemListSerializer, RecurringTaskSerializer,
    TaskSerializer, TaskCreateSerializer, TaskUpdateSerializer, TaskListSerializer,
    AlertSerializer, AlertSubmitSerializer, AlertAcknowledgeSerializer,
    AlertResolveSerializer, AlertSilenceSerializer,
    ActionSerializer, ActionSubmitSerializer, ActionApproveSerializer, ActionRejectSerializer,
    ApprovalRuleSerializer, SilenceRuleSerializer, SilenceRuleCreateSerializer,
    EscalationRuleSerializer, InhibitionRuleSerializer,
    SubscriptionSerializer, FingerprintConfigSerializer,
    UserNotificationChannelSerializer, UserNotificationChannelCreateSerializer,
    TestNotificationSerializer,
    DailySummarySerializer, StatisticsSerializer,
    ProjectSerializer, ProjectCreateSerializer, ProjectMembershipSerializer,
    ProjectMemberAddSerializer,
    SprintSerializer, SprintCreateSerializer, SprintListSerializer,
    CommentSerializer, CommentCreateSerializer, CommentWithReactionsSerializer,
    AttachmentSerializer, AttachmentUploadSerializer,
    validate_attachment_serve_token,
    ItemActivitySerializer,
    ItemLinkSerializer, ItemLinkCreateSerializer,
    ItemWatcherSerializer,
    CommentReactionSerializer, CommentReactionCreateSerializer,
    NotificationSerializer,
    TaskDraftProposalSerializer, TaskDraftCreateSerializer,
    TaskDraftConfirmSerializer, TaskDraftRejectSerializer,
    AiGenerationRunSerializer,
    SavedFilterSerializer, SavedFilterCreateSerializer, SavedFilterUpdateSerializer,
)


from .services.ai_recaps import (
    DEFAULT_MODEL,
    MAX_WINDOW_DAYS,
    PROMPT_TEMPLATE_VERSION,
    RecapPermissionDenied,
    append_recap_feedback,
    create_recap_run,
)
from .services.actionable_items import (
    NON_ACTIONABLE_STATUS_SLUGS,
    TERMINAL_STATUS_SLUGS,
    WAITING_QUEUE_STATUS_SLUGS,
    annotate_owner_queue_projection,
    filter_active_queue_items,
    future_recurring_owner_waiting_q,
    owner_awareness_exists,
)
def _release_assignment_wip_reservation(view_method):
    """Make one ``/assign/`` call the owner of its exact WIP reservation."""
    @wraps(view_method)
    def wrapped(self, request, *args, **kwargs):
        try:
            return view_method(self, request, *args, **kwargs)
        finally:
            item = getattr(request, '_pulse_assignment_wip_item', None)
            if item is not None:
                from .services.workflow_service import WorkflowService
                WorkflowService._release_unconsumed_wip_reservation(item)
                delattr(request, '_pulse_assignment_wip_item')
    return wrapped


def _describe_item_identifier(identifier):
    """Return (normalized, type, suggestion) for task identifiers agents use.

    Pulse has three common task shapes in the wild: project item keys
    (``PLAT-123``), UUID primary keys, and legacy/numeric-only identifiers that
    are displayed to humans as ``#6559``.  Keep the normalization in one place so
    normal item reads, link helpers, and workflow migration all agree.
    """
    raw = '' if identifier is None else str(identifier).strip()
    if raw.startswith('#') and raw[1:].isdigit():
        return raw[1:], 'display_key', raw[1:]
    try:
        uuid.UUID(raw)
        return raw, 'uuid', raw
    except (TypeError, ValueError, AttributeError):
        pass
    if raw.isdigit():
        return raw, 'numeric', f"#{raw}"
    return raw, 'item_key', raw


def _resolve_item_identifier(identifier, *, mode='task'):
    """Resolve project key, UUID, numeric-only key, or #display key to Item."""
    normalized, _kind, _suggestion = _describe_item_identifier(identifier)
    if not normalized:
        return None

    qs = Item.objects.all()
    if mode:
        qs = qs.filter(mode=mode)

    # Project keys and numeric-only task keys live in item_key.  Prefer that
    # stable public key before trying the database primary key.
    item = qs.filter(item_key__iexact=normalized).first()
    if item:
        return item

    # Alerts created off the Item serializer historically skipped
    # get_next_item_key, so the Alerts UI falls back to ``{project.key}-{pk}``.
    # Agents then call pulse_get_task('PLAT-33301') and 404'd even though the
    # row existed. Honor that display form when no minted key matches.
    display = re.fullmatch(r'([A-Za-z][A-Za-z0-9]*)-(\d+)$', normalized)
    if display:
        item = qs.filter(
            project__key__iexact=display.group(1),
            pk=int(display.group(2)),
            item_key__isnull=True,
        ).first()
        if item:
            return item

    try:
        return qs.get(pk=normalized)
    except (Item.DoesNotExist, ValueError, TypeError):
        return None


def _is_exact_item_key_search(value):
    """Return whether a free-text search is exactly one public item key.

    ``search=PLAT-123`` is a very common compatibility spelling used by older
    clients.  Treat it like the indexed ``item_key=PLAT-123`` filter instead of
    running the broad title/description/comment search plan.  The trailing
    numeric component is the invariant minted by ``Project.get_next_item_key``;
    the project component remains permissive enough for historical keys.
    """
    return (
        bool(value)
        and len(value) <= Item._meta.get_field('item_key').max_length
        and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*-[0-9]+', value) is not None
    )


def _explicit_org_id(request):
    """Resolve an explicit org scope from X-Organization-ID or query params.

    Selector precedence, highest first — documented because PLS-439 propagates
    this helper across the router, and 47 viewsets should not each rediscover it:

        1. ``X-Organization-ID`` header
        2. ``?organization=``
        3. ``?organization_id=``

    The header wins over a contradictory query parameter. A proxy or client that
    injects the header therefore overrides a caller's explicit query scope, with
    no signal to the caller — deliberate, but worth knowing before you debug it.

    Returns ``None`` when no selector is present (meaning "no explicit scope",
    not "org zero"). Fails CLOSED on an unparseable value: never returns an
    unnarrowed queryset because the scope could not be read.
    """
    raw = (
        request.headers.get('X-Organization-ID')
        or request.query_params.get('organization')
        or request.query_params.get('organization_id')
    )
    if raw in (None, ''):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        # PLS-670 / ADR-0054: a syntactically invalid selector is a client
        # error (400), not an authorization failure (403). 403 is reserved for
        # a well-formed numeric org the caller may not access, so a debugger
        # never has to chase memberships for an unparseable value.
        raise ValidationError('Invalid organization scope.')


def _ensure_explicit_org_membership(request):
    """Reject explicit wrong-org requests before list/create can return 2xx.

    Returns the validated org id so the caller can narrow with it. Callers MUST
    use the return value — validating without then filtering yields 403 on a
    wrong-org request and an unnarrowed 200 on a right-org one.
    """
    org_id = _explicit_org_id(request)
    if org_id is None:
        return None
    user = request.user
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return org_id
    memberships = getattr(user, 'organization_memberships', {}) or {}
    # Skip unparseable keys individually rather than discarding the whole map.
    # `_normalize_memberships` (tasks/authentication.py) already guarantees
    # {int: str} on every production auth path, so the previous all-or-nothing
    # `try: {int(k) for k in ...} except: set()` could not fire today — but it
    # meant one non-numeric key (e.g. a `personal-N` style scope) would deny a
    # user EVERY org, including ones they genuinely belong to. That made this
    # helper safe by virtue of an upstream invariant it neither stated nor
    # enforced, which is not a property to replicate across 47 viewsets.
    member_org_ids = set()
    for key in memberships:
        try:
            member_org_ids.add(int(key))
        except (TypeError, ValueError):
            continue
    if org_id not in member_org_ids:
        from .authentication import merge_live_membership
        if merge_live_membership(user, org_id):
            return org_id
        if not member_org_ids:
            # Distinguish "you have no usable membership map" from "you are not
            # in THIS org" — the old wording sent a debugger to org N's
            # membership records when the real condition was an absent map.
            raise PermissionDenied(
                'No organization memberships are available for this account; '
                f'cannot scope to organization {org_id}.'
            )
        raise PermissionDenied(f'You are not a member of organization {org_id}.')
    return org_id


def _ensure_org_default_project(org_id, user):
    """HIVE-1223: lazily get_or_create the org's canonical default project.

    Called ONLY in the empty-org first-task-create case (see ItemViewSet.create):
    ``mode=task`` + no ``project`` + the resolved org has zero projects. Binds the
    project to the RESOLVED ``org_id`` (the tenant-isolation crux — never the
    caller's default/other org) and marks it ``is_default=True``.

    Concurrency-safe: get_or_create on the ``(org_id, is_default=True)`` idempotency
    key, with the ``uniq_default_project_per_org`` partial-unique constraint as the DB
    backstop. Row-locking can't lock a not-yet-existing row, so the constraint (not
    ``select_for_update``) is what actually closes the race — racing first-task POSTs
    both miss the SELECT, one INSERT wins, the loser's INSERT raises IntegrityError and
    get_or_create re-reads the winner's row. Project keys are unique only WITHIN an org,
    so ``O{org_id}`` is collision-free in a zero-project org; the suffix loop guards the
    pathological case where a non-default project already claimed that key.
    """
    # reika P3-1: the suffix must survive the 10-char truncation, or the loop can
    # revisit a key it already rejected and spin forever (a hung request thread,
    # not a bad key). A 6-char prefix + a zero-padded 4-digit suffix is exactly 10,
    # so `[:10]` never truncates and every suffix maps to a distinct key. Note the
    # naive "shrink the prefix as the suffix grows" variant is NOT collision-free:
    # a shorter prefix with a longer tail reproduces a longer prefix with a shorter
    # one (measured 909/999 distinct for org_id=1234567). Bounded `for/else` makes
    # termination structural rather than dependent on key distinctness.
    base_key = f"O{org_id}".upper()[:10]
    key = base_key
    for suffix in range(1, 1000):
        if not Project.objects.filter(org_id=org_id, key=key).exists():
            break
        key = f"{base_key[:6]}{suffix:04d}"[:10].upper()
    else:
        # 999 collisions in one org is not a reachable state; fail toward a random
        # key rather than looping. get_or_create + uniq_default_project_per_org
        # remain the real guards.
        key = f"O{uuid.uuid4().hex[:9]}".upper()[:10]
    project, _created = Project.objects.get_or_create(
        org_id=org_id,
        is_default=True,
        defaults={
            'name': 'General',
            'key': key,
            'project_type': 'software',
            # Org-scoped: all members of THIS org. Tenancy is enforced by org_id;
            # no access_level is cross-org visible, so 'open' means "all members of
            # this org", not "public". Never cross-org.
            'access_level': 'open',
            'lead_id': getattr(user, 'id', None),
            'lead_email': getattr(user, 'email', None) or None,
            'created_by_id': getattr(user, 'id', None),
        },
    )
    return project


def _format_item_identifier_error(identifier):
    normalized, kind, suggestion = _describe_item_identifier(identifier)
    if kind == 'display_key':
        return (f"task not found for display key {identifier!r} "
                f"(normalized to numeric key/ID {normalized!r}; try {suggestion!r})")
    if kind == 'numeric':
        return (f"task not found for numeric identifier {identifier!r} "
                f"(also tried numeric-only item_key {normalized!r}; display form {suggestion!r})")
    if kind == 'uuid':
        return f"task not found for UUID {identifier!r}"
    return f"task not found for item_key {identifier!r}"
from .user_service import user_service
from .services.deduplication import AlertDeduplicationService, AlertIdentityError
from .services.silencing import SilencingService
from .services.escalation import EscalationService
from .services.inhibition import InhibitionService
from .permissions import (
    IsOwnerOrStaffForItems, IsOwnerOrStaffForChannels,
    ProjectPermission, ItemProjectPermission, can_comment_on_item,
    is_item_participant, GlobalConfigurationPermission,
    RelatedProjectPermission, DashboardOwnerPermission,
)
from .services.permissions import PermissionService
from .pagination import CommentPagination

logger = logging.getLogger(__name__)


def _accessible_projects_for_user(user):
    """Return the canonical request-principal project scope (never client scope)."""
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return Project.objects.all()
    return PermissionService.get_user_projects(user=user)


def _require_accessible_project(user, project_id, *, permission='edit'):
    """Resolve a project inside the caller's visibility/write authority."""
    project = _accessible_projects_for_user(user).filter(id=project_id).first()
    if project is None:
        raise NotFound('Project not found')
    if permission != 'view' and not (
        getattr(user, 'is_staff', False)
        or getattr(user, 'is_superuser', False)
        or PermissionService.can_access_project(
            user.id,
            project,
            permission=permission,
            user_email=getattr(user, 'email', ''),
            user=user,
        )
    ):
        raise PermissionDenied('You do not have permission to modify this project.')
    return project


def _user_organization_ids(user):
    """Verified JWT/admin membership keys; never a request header/body claim."""
    memberships = getattr(user, 'organization_memberships', {}) or {}
    result = set()
    for value in memberships:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _accessible_dashboards_for_user(user):
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return Dashboard.objects.all()
    return Dashboard.objects.filter(
        Q(owner_id=user.id) | Q(visibility='public')
    )

# AI User ID - consistent across the system
AI_ID = int(os.getenv("AI_ID", "2"))
MAX_SOURCE_URL_LENGTH = Item._meta.get_field('source_url').max_length or 2048
CEO_EMAIL = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()
COS_EMAIL = os.environ.get('PULSE_COS_EMAIL', '').strip().lower()
PROJECT_LIST_DB_ERROR_MARKER = 'PLS358_PROJECTS_DB_OPERATIONAL_ERROR'
PROJECT_LIST_UNEXPECTED_ERROR_MARKER = 'PLS358_PROJECTS_UNEXPECTED_ERROR'
# PLS-605 (aoi, PLS-439 port spec): PLS-358's fail-loud wrapper is applied
# per-viewset, so detection coverage is opt-in — and it is ANTI-correlated with
# protection. A route that got the wrapper is both better instrumented and more
# likely to have been looked at; a route with neither is invisible AND unguarded.
# `objectives` was that case: the same crash as `projects`, but surfacing as an
# anonymous 500 no error-code search could ever find, which is why it went
# unnoticed while the labelled twin was reported within hours. Guard and wrapper
# therefore travel together — porting the guard alone fixes this crash class and
# leaves the NEXT class equally invisible on the same route.
OBJECTIVE_LIST_DB_ERROR_MARKER = 'PLS358_OBJECTIVES_DB_OPERATIONAL_ERROR'
OBJECTIVE_LIST_UNEXPECTED_ERROR_MARKER = 'PLS358_OBJECTIVES_UNEXPECTED_ERROR'
# PLS-523: a workflow transition that already COMMITTED must never become a
# false-negative 500 because non-critical display enrichment failed afterward.
TRANSITION_RECEIPT_DEGRADED_MARKER = 'PLS523_TRANSITION_RECEIPT_DEGRADED'


SERVICE_AUTH_MAX_AGE_SECONDS = 300

_DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT = 0
_DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS = None

KNOWN_SHARED_SERVICE_IDENTITIES = {'codex@shizuha.com'}


def _normalize_audit_email(email):
    return (str(email or '').strip().lower())


def _active_roster_identity_counts():
    """Return email -> roster occurrence count for HIVE-412-style service identity hints."""
    counts = {}
    for roster in TeamRoster.objects.filter(is_active=True).only('tiers'):
        for tier in roster.tiers or []:
            if not isinstance(tier, list):
                continue
            for email in tier:
                normalized = _normalize_audit_email(email)
                if normalized:
                    counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def _is_shared_service_identity(email, identity_counts=None):
    """Backend mirror of Hive HIVE-412 serviceIdentity.js."""
    normalized = _normalize_audit_email(email)
    if not normalized:
        return False
    if normalized in KNOWN_SHARED_SERVICE_IDENTITIES:
        return True
    return (identity_counts or {}).get(normalized, 0) > 1


def _internal_lifecycle_secret():
    return (
        getattr(settings, 'PULSE_USER_LIFECYCLE_CONSUMER_SECRET', '')
        or os.environ.get('PULSE_USER_LIFECYCLE_CONSUMER_SECRET', '')
        or os.environ.get('ID_TO_PULSE_LIFECYCLE_SECRET', '')
    ).strip()


def _verify_id_lifecycle_signature(request):
    """Verify the signed ID -> Pulse lifecycle consumer request."""
    secret = _internal_lifecycle_secret()
    if not secret:
        return False, Response(
            {'error': 'Pulse lifecycle consumer secret is not configured'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if request.headers.get('X-Shizuha-Service') != 'shizuha-id':
        return False, Response({'error': 'Missing service identity'}, status=status.HTTP_401_UNAUTHORIZED)
    timestamp = request.headers.get('X-Shizuha-Timestamp', '')
    signature = request.headers.get('X-Shizuha-Signature', '')
    if not timestamp or not signature.startswith('sha256='):
        return False, Response({'error': 'Missing lifecycle signature'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        ts = int(timestamp)
    except ValueError:
        return False, Response({'error': 'Malformed lifecycle timestamp'}, status=status.HTTP_401_UNAUTHORIZED)
    if abs(int(time.time()) - ts) > int(getattr(settings, 'SERVICE_AUTH_MAX_AGE_SECONDS', SERVICE_AUTH_MAX_AGE_SECONDS)):
        return False, Response({'error': 'Expired lifecycle signature'}, status=status.HTTP_401_UNAUTHORIZED)
    expected = hmac.new(
        secret.encode('utf-8'),
        timestamp.encode('utf-8') + b'.' + (request.body or b''),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest('sha256=' + expected, signature):
        return False, Response({'error': 'Bad lifecycle signature'}, status=status.HTTP_401_UNAUTHORIZED)
    return True, None


def resolve_assignee_email_for_operator_gate(assignee_id=None, assignee_email=None):
    """Resolve assignee_id/email consistently and fail closed on mismatches.

    PLS-16: callers must not be able to smuggle the operator through an id-only
    assignment, a mismatched id+email pair, or a transient directory failure.
    """
    target_email = (assignee_email or '').strip().lower()
    if assignee_id:
        try:
            info = user_service.get_user_by_id(int(assignee_id))
        except Exception as exc:
            raise drf_serializers.ValidationError({
                'assignee_id': f'Unable to resolve assignee_id={assignee_id}; failing closed.'
            }) from exc
        id_email = (info or {}).get('email', '').strip().lower()
        if not id_email:
            raise drf_serializers.ValidationError({
                'assignee_id': f'Unable to resolve assignee_id={assignee_id}; failing closed.'
            })
        if target_email and target_email != id_email:
            raise drf_serializers.ValidationError({
                'assignee_id': 'assignee_id and assignee_email refer to different users.'
            })
        target_email = id_email
    return target_email


def enforce_operator_assignment_gate(actor_email, assignee_id=None, assignee_email=None, *, context='assignment'):
    target_email = resolve_assignee_email_for_operator_gate(assignee_id, assignee_email)
    actor = (actor_email or '').strip().lower()
    if CEO_EMAIL and target_email == CEO_EMAIL and (not actor or actor not in (CEO_EMAIL, COS_EMAIL)):
        raise PermissionDenied({
            'assignee_email': (
                f"Refusing operator {context} to {CEO_EMAIL} — the operator is LAST "
                f"RESORT and takes Pulse assignments only from the Chief of Staff "
                f"({COS_EMAIL}) or via operator self-assignment."
            )
        })
    return target_email


def fit_source_url(url):
    """Return a source_url value that is safe for Item.source_url storage."""
    if not url:
        return url
    return str(url)[:MAX_SOURCE_URL_LENGTH]

def _scrape_agent_health(timeout=4):
    """PLS-20: live per-agent health from the agent-health exporter (the same
    source the availability sweep reads) -> {short_name: {status, auth_errors,
    last_activity_s}}. Best-effort: any failure returns {} so the org-chart still
    renders task data. Cached ~20s to avoid a scrape per request."""
    import os, re, time as _time
    now = _time.time()
    cached = globals().get('_AGENT_HEALTH_CACHE')
    if cached and cached[0] > now:
        return cached[1]
    url = os.environ.get('PULSE_HEALTH_EXPORTER_URL', 'http://host.docker.internal:9888/metrics').strip()
    out = {}
    if url and url.lower() not in {'disabled', 'off', 'none'}:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            text = resp.text

            def _vals(metric):
                rx = re.compile(r'^' + metric + r'\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
                return {m.group(1): float(m.group(2)) for m in rx.finditer(text)}

            up = _vals('shizuha_agent_process_up')
            wedge = _vals('shizuha_agent_wedge_errors')
            prov = _vals('shizuha_agent_provider_healthy')
            enabled = _vals('shizuha_agent_enabled')
            last = _vals('shizuha_agent_last_activity_seconds')
            auth = _vals('shizuha_agent_auth_errors')
            wedge_th = float(os.environ.get('PULSE_WEDGE_THRESHOLD', '10'))
            for agent in set(up) | set(enabled) | set(last):
                if enabled.get(agent, 1) < 1:
                    status = 'disabled'
                elif up.get(agent, 0) < 1:
                    status = 'absent'
                elif wedge.get(agent, 0) >= wedge_th:
                    status = 'wedged'
                elif prov.get(agent, 1) < 1:
                    status = 'provider_down'
                else:
                    status = 'active'
                out[agent] = {
                    'status': status,
                    'auth_errors': int(auth.get(agent, 0)),
                    'last_activity_s': int(last[agent]) if agent in last else None,
                }
        except Exception:
            out = {}
    globals()['_AGENT_HEALTH_CACHE'] = (now + 20, out)
    return out


# =============================================================================
# Customer Request Intake Portal (PLS-273)
# =============================================================================

class CustomerRequestPublicSerializer(drf_serializers.ModelSerializer):
    """Public shape: never expose requester/commenter email addresses."""

    project_key = drf_serializers.CharField(source='project.key', read_only=True)
    created_item_key = drf_serializers.CharField(source='created_item.item_key', read_only=True, default=None)

    class Meta:
        model = CustomerRequest
        fields = [
            'id', 'project_key', 'title', 'description', 'category', 'requester_name',
            'status', 'upvote_count', 'created_item_key', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class CustomerRequestCreateSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = CustomerRequest
        fields = ['title', 'description', 'category', 'requester_name', 'requester_email', 'requester_external_id']
        extra_kwargs = {
            'title': {'required': True, 'allow_blank': False, 'max_length': 240},
            'description': {'required': True, 'allow_blank': False},
            'category': {'required': False, 'allow_blank': True, 'max_length': 80},
            'requester_name': {'required': False, 'allow_blank': True, 'max_length': 120},
            'requester_email': {'required': False, 'allow_blank': True},
            'requester_external_id': {'required': False, 'allow_blank': True, 'max_length': 200},
        }

    def validate_description(self, value):
        if len(value.strip()) < 10:
            raise drf_serializers.ValidationError('Description must be at least 10 characters.')
        return value.strip()

    def validate_title(self, value):
        return value.strip()


class CustomerRequestCommentPublicSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = CustomerRequestComment
        fields = ['id', 'author_name', 'body', 'created_at']
        read_only_fields = fields


class CustomerRequestCommentCreateSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = CustomerRequestComment
        fields = ['author_name', 'author_email', 'body']
        extra_kwargs = {
            'author_name': {'required': False, 'allow_blank': True, 'max_length': 120},
            'author_email': {'required': False, 'allow_blank': True},
            'body': {'required': True, 'allow_blank': False},
        }

    def validate_body(self, value):
        if len(value.strip()) < 2:
            raise drf_serializers.ValidationError('Comment body must not be blank.')
        return value.strip()


class CustomerRequestVoteSerializer(drf_serializers.Serializer):
    voter_email = drf_serializers.EmailField(required=False, allow_blank=True)


class CustomerRequestApproveSerializer(drf_serializers.Serializer):
    title = drf_serializers.CharField(required=False, allow_blank=True, max_length=400)
    description = drf_serializers.CharField(required=False, allow_blank=True)
    priority = drf_serializers.ChoiceField(choices=[c[0] for c in Item.PRIORITY_CHOICES], required=False, default='normal')


def _resolve_customer_portal_project(project_ref):
    """
    Resolve the public customer portal by an explicit, non-enumerable opt-in ref.

    Public routes must never expose arbitrary active projects by DB id or by the
    internal project key. A project is externally visible only when its settings
    contain both:
      - customer_portal_enabled: true
      - customer_portal_public_ref: <unguessable slug/token>
    """
    ref = str(project_ref or '').strip()
    if not ref or ref.isdigit():
        raise NotFound('Project portal not found.')

    qs = Project.objects.filter(is_active=True, is_archived=False)
    project = qs.filter(
        settings__customer_portal_enabled=True,
        settings__customer_portal_public_ref=ref,
    ).order_by('org_id', 'id').first()
    if not project:
        raise NotFound('Project portal not found.')
    return project


def _customer_voter_key(request, email=''):
    normalized = (email or '').strip().lower()
    if normalized:
        raw = f'email:{normalized}'
    else:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        ip = forwarded or request.META.get('REMOTE_ADDR', '')
        ua = request.META.get('HTTP_USER_AGENT', '')[:160]
        raw = f'anon:{ip}:{ua}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class CustomerPortalRequestListCreateView(APIView):
    """Public customer-facing request board for a single project."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request, project_ref):
        project = _resolve_customer_portal_project(project_ref)
        qs = CustomerRequest.objects.filter(project=project).select_related('project', 'created_item')
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status__in=[s.strip() for s in status_filter.split(',') if s.strip()])
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category__iexact=category.strip())
        search = request.query_params.get('q')
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))
        serializer = CustomerRequestPublicSerializer(qs[:100], many=True)
        return Response({
            'project': {
                'key': project.key,
                'name': project.name,
                'public_ref': (project.settings or {}).get('customer_portal_public_ref'),
            },
            'results': serializer.data,
        })

    def post(self, request, project_ref):
        project = _resolve_customer_portal_project(project_ref)
        serializer = CustomerRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer_request = serializer.save(project=project, metadata={'source': 'customer-portal'})
        return Response(CustomerRequestPublicSerializer(customer_request).data, status=status.HTTP_201_CREATED)


class CustomerPortalRequestDetailView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request, project_ref, request_id):
        project = _resolve_customer_portal_project(project_ref)
        customer_request = get_object_or_404(CustomerRequest.objects.select_related('project', 'created_item'), project=project, id=request_id)
        return Response(CustomerRequestPublicSerializer(customer_request).data)


class CustomerPortalRequestVoteView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request, project_ref, request_id):
        project = _resolve_customer_portal_project(project_ref)
        customer_request = get_object_or_404(CustomerRequest, project=project, id=request_id)
        serializer = CustomerRequestVoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data.get('voter_email', '')
        voter_key = _customer_voter_key(request, email)
        with transaction.atomic():
            _, created = CustomerRequestVote.objects.get_or_create(
                request=customer_request,
                voter_key=voter_key,
                defaults={'voter_email': email.strip().lower()},
            )
            if created:
                CustomerRequest.objects.filter(pk=customer_request.pk).update(upvote_count=F('upvote_count') + 1)
                customer_request.refresh_from_db(fields=['upvote_count'])
        return Response({'id': str(customer_request.id), 'upvote_count': customer_request.upvote_count, 'created': created})


class CustomerPortalRequestCommentListCreateView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request, project_ref, request_id):
        project = _resolve_customer_portal_project(project_ref)
        customer_request = get_object_or_404(CustomerRequest, project=project, id=request_id)
        comments = customer_request.comments.all()[:100]
        return Response({'results': CustomerRequestCommentPublicSerializer(comments, many=True).data})

    def post(self, request, project_ref, request_id):
        project = _resolve_customer_portal_project(project_ref)
        customer_request = get_object_or_404(CustomerRequest, project=project, id=request_id)
        serializer = CustomerRequestCommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.save(request=customer_request)
        return Response(CustomerRequestCommentPublicSerializer(comment).data, status=status.HTTP_201_CREATED)


class CustomerPortalRequestApproveView(APIView):
    """Authenticated internal conversion of a customer request into a Pulse item."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, request_id):
        customer_request = get_object_or_404(CustomerRequest.objects.select_related('project', 'created_item'), id=request_id)
        project = customer_request.project
        user = request.user
        if not user.is_staff and not PermissionService.can_access_project(
            user.id, project, permission='create', user_email=getattr(user, 'email', None), user=user
        ):
            raise PermissionDenied('You do not have permission to create items in this project.')

        serializer = CustomerRequestApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            customer_request = (
                CustomerRequest.objects
                .select_for_update()
                .select_related('project')
                .get(id=request_id)
            )
            project = customer_request.project

            if customer_request.created_item_id:
                return Response({
                    'customer_request': CustomerRequestPublicSerializer(customer_request).data,
                    'item': ItemSerializer(customer_request.created_item).data,
                    'created': False,
                })

            title = serializer.validated_data.get('title') or customer_request.title
            description = serializer.validated_data.get('description') or (
                f"Customer request submitted via Pulse portal.\n\n"
                f"Category: {customer_request.category or 'Uncategorized'}\n"
                f"Requester: {customer_request.requester_name or 'Anonymous'}\n"
                f"Upvotes at approval: {customer_request.upvote_count}\n\n"
                f"{customer_request.description}"
            )
            workflow = 'feature-request-rfc'
            item = Item.objects.create(
                mode='task',
                item_type='task.story',
                project=project,
                item_key=project.get_next_item_key(),
                title=title,
                description=description,
                source='customer_request_portal',
                source_id=str(customer_request.id),
                priority=serializer.validated_data.get('priority') or 'normal',
                assignment_group=(project.settings or {}).get('customer_request_assignment_group') or 'product',
                workflow_name=workflow,
                status='rfc_intake',
                created_by_id=getattr(user, 'id', None),
                created_by_email=getattr(user, 'email', '') or '',
                metadata={'customer_request_id': str(customer_request.id), 'customer_request_upvotes': customer_request.upvote_count},
            )
            customer_request.created_item = item
            customer_request.status = 'under_review'
            customer_request.approved_by_id = getattr(user, 'id', None)
            customer_request.approved_by_email = getattr(user, 'email', '') or ''
            customer_request.approved_at = timezone.now()
            customer_request.save(update_fields=['created_item', 'status', 'approved_by_id', 'approved_by_email', 'approved_at', 'updated_at'])

        return Response({
            'customer_request': CustomerRequestPublicSerializer(customer_request).data,
            'item': ItemSerializer(item).data,
            'created': True,
        }, status=status.HTTP_201_CREATED)

def _decision_pin_team_for_email(email):
    try:
        from .services.workflow_service import WorkflowService
        target = (email or '').strip().lower()
        for team, members in WorkflowService.assignable_teams(include_active_task_groups=False).items():
            flat = {m.strip().lower() for m in WorkflowService._flat_roster(members)}
            if target in flat:
                return WorkflowService._canonical_team_name(team)
    except Exception:
        logger.warning("PLS-233 pinned-team resolution failed (non-fatal)", exc_info=True)
    return ''


def _clear_direct_assignment_pin(item, *, save=True) -> bool:
    md = dict(item.metadata or {}) if isinstance(item.metadata, dict) else {}
    if not ({'direct_assignment', 'explicit_merge_handoff'} & set(md)):
        return False
    md.pop('direct_assignment', None)
    md.pop('explicit_merge_handoff', None)
    item.metadata = md
    if save:
        item.save(update_fields=['metadata'])
    return True


def _normalize_validation_error_details(exc) -> dict:
    """Return a JSON-serializable field→messages map from a Django ValidationError."""
    if hasattr(exc, 'message_dict'):
        out = {}
        for field, messages in exc.message_dict.items():
            if isinstance(messages, (list, tuple)):
                out[str(field)] = [str(m) for m in messages]
            else:
                out[str(field)] = [str(messages)]
        return out
    messages = list(getattr(exc, 'messages', None) or [str(exc)])
    return {'non_field_errors': [str(m) for m in messages]}


def _format_assignment_validation_detail(details: dict) -> str:
    """PLS-859: human Detail string naming every failing field/constraint.

    Agents previously only saw the opaque code ``assignment_validation_failed``
    because MCP dropped the structured ``details`` map. Put the field map into a
    single ``detail`` string so both HTTP clients and MCP surface the constraint.
    """
    if not details:
        return 'assignment validation failed (no field detail available)'
    parts = []
    for field, messages in details.items():
        if isinstance(messages, (list, tuple)):
            msg = '; '.join(str(m) for m in messages)
        else:
            msg = str(messages)
        parts.append(f'{field}: {msg}')
    return '; '.join(parts)


def _record_direct_assignment_pin_persist_failure():
    """PLAT-3011: fail-open observable counter for PLS-233 pin writes.

    Metadata persistence is deliberately best-effort, so this in-process signal
    must also never raise into the assignment path.
    """
    global _DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT
    global _DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS
    try:
        _DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT += 1
        _DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS = timezone.now().isoformat()
    except Exception:
        logger.warning(
            "PLAT-3011 direct-assignment pin failure metric update failed (non-fatal)",
            exc_info=True,
        )


def _direct_assignment_pin_persist_failure_metrics():
    """Return the statusz-exposed PLS-233 pin persistence failure signal."""
    return {
        'count': _DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT,
        'last_failure_ts': _DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS,
    }


def _reset_direct_assignment_pin_persist_failure_metric_for_tests():
    global _DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT
    global _DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS
    _DIRECT_ASSIGNMENT_PIN_PERSIST_FAILURE_COUNT = 0
    _DIRECT_ASSIGNMENT_PIN_PERSIST_LAST_FAILURE_TS = None


def _persist_direct_assignment_pin(
    item,
    assignee_email,
    reason,
    *,
    actor_id=0,
    actor_email='',
):
    """PLS-233: record an explicit direct-assign-with-reason handoff on the item.

    A decision-gated task (e.g. a `simple`-workflow devops task needing an
    Architecture sign-off) is direct-assigned to the decision-owner with a reason.
    Without a persisted marker, the rebalance sweep + availability failover re-resolve
    the assignee from the task's `team`/`assignment_group` and pull it BACK to the
    symptom-team pool (CTX-274/278 bounced 3x each). Persisting the handoff lets
    `_is_decision_pinned` (tasks.py) exempt it from those sweeps and, if the pinned
    owner is unavailable, fail over within the pinned decision team only. Keyed to the
    pinned assignee_email so a later real reassignment auto-releases the pin.
    Best-effort: never block the assignment on a metadata write failure.
    """
    try:
        from .services.workflow_service import WorkflowService

        md = dict(item.metadata or {})
        now = timezone.now().isoformat()
        target = (assignee_email or '').strip().lower()
        actor = (actor_email or '').strip().lower()
        md['direct_assignment'] = {
            'assignee_email': target,
            'pinned_team': _decision_pin_team_for_email(assignee_email),
            'reason': (reason or '')[:500],
            'at': now,
        }
        lane = WorkflowService._status_lane_assignment_group(getattr(item, 'status', None))
        group = WorkflowService._canonical_team_name(getattr(item, 'assignment_group', '') or '')
        is_merge_handoff = lane == 'merge' or group == 'merge'
        handoff_payload = None
        if is_merge_handoff:
            handoff_payload = {
                'assignee_email': target,
                'reason': (reason or '')[:500],
                'assigned_by_email': actor,
                'at': now,
            }
        else:
            md.pop('explicit_merge_handoff', None)
        with transaction.atomic():
            if handoff_payload is not None:
                from django.core import signing
                activity = ItemActivity.objects.create(
                    item=item,
                    action='updated',
                    field_name='Explicit Merge Handoff',
                    new_value=target,
                    user_id=actor_id or 0,
                    user_email=actor,
                )
                handoff_payload['audit_activity_id'] = str(activity.id)
                handoff_payload['attestation'] = signing.dumps(
                    {
                        'assignee_email': target,
                        'assigned_by_email': actor,
                        'reason': (reason or '')[:500],
                        'at': now,
                    },
                    salt='pulse.explicit_merge_handoff',
                    compress=True,
                )
                md['explicit_merge_handoff'] = handoff_payload
            item.metadata = md
            item.save(update_fields=['metadata'])
    except Exception:
        _record_direct_assignment_pin_persist_failure()
        logger.warning("PLS-233 direct-assignment pin persist failed (non-fatal)", exc_info=True)


# =============================================================================
# PLAT-4871 / PLS-539: ONE shared direct-handoff mutation contract
# -----------------------------------------------------------------------------
# create, /assign, and PATCH all route direct-vs-team assignment through the
# primitives below so the reason gate, the stale-group intent, the audit comment,
# and the decision pin are byte-identical across every entry point, and the audit
# is atomic with the assignee write (a comment failure rolls the assignee back).
#
# The single source of truth is normalize_direct_handoff(): it resolves the raw
# request payload (assignee_id / assignee_email / group / reason) into ONE canonical
# command BEFORE any gate or write, so equivalent handoffs expressed differently
# (email-only vs id-only, with or without a co-supplied group) can no longer diverge
# between endpoints (revi #47 P1). There is exactly ONE precedence, consumed UNCHANGED
# at create / /assign / PATCH — no endpoint-local reclassification:
#   * TEAM route == a group is named AND NO direct_assignment_reason accompanies it
#     -> Pulse resolves the live member; any concrete assignee in the payload is a
#     stale hint (e.g. a client-sent team lead) and is DROPPED, never pinned. This
#     preserves the long-standing tolerance in test_plat1089_* / test_assignment.
#   * DIRECT == a concrete assignee is named AND it is not a team route (a reason is
#     present, or no group is co-supplied) -> requires a reason (unless the ACTOR is
#     the operator), is audited + pinned, and a co-supplied group is RETAINED and its
#     exact (org, team) membership enforced at the persistence fence; a LEFTOVER group
#     (none re-supplied) is cleared so the pinned owner is not re-validated through a
#     team it does not belong to.
# The reason is the direct-intent signal when a group co-exists. The id->email
# resolution closes the id-only gap: the gate + writer see the SAME resolved assignee
# and the SAME is_direct no matter the representation.
# =============================================================================


class DirectHandoffCommand:
    """The canonical, endpoint-independent representation of an assignment handoff.

    Built once per request by normalize_direct_handoff() and consumed identically by
    direct_handoff_reason_denied() (the reason gate) and record_direct_handoff() (the
    audit/pin writer). ``assignee_email`` is resolved (id->email) and lowercased;
    ``is_team_route``/``is_direct`` encode the ONE precedence, so every endpoint drops
    the stale assignee on a team route and gates/audits the same direct writes.
    """
    __slots__ = ('assignee_id', 'assignee_email', 'group', 'reason',
                 'is_direct', 'is_team_route')

    def __init__(self, assignee_id, assignee_email, group, reason,
                 is_direct, is_team_route):
        self.assignee_id = assignee_id
        self.assignee_email = assignee_email
        self.group = group
        self.reason = reason
        self.is_direct = is_direct
        self.is_team_route = is_team_route


def normalize_direct_handoff(*, assignee_id=None, assignee_email=None,
                             assignment_group=None, reason=None, resolve_email=True):
    """Resolve a raw handoff payload into ONE canonical DirectHandoffCommand.

    Called at the top of create / /assign / PATCH so the reason gate + audit writer
    see identical intent no matter how the caller expressed the target:
      * id-only requests are resolved to an email (via the shared identity source) so
        an id-only direct handoff can never skip the reason gate the way an email-only
        one is caught (revi #47 P1, counterexample a);
      * intent is derived ONCE with a single precedence, consumed unchanged at every
        entry point (revi #47 P1.1): a group with NO reason is a TEAM route (a
        co-supplied assignee is a stale hint, dropped); otherwise a concrete assignee
        is a DIRECT override (a group+assignee+reason payload is DIRECT everywhere,
        counterexample b).
    """
    email = (assignee_email or '').strip().lower()
    aid = assignee_id if assignee_id not in (None, '') else None
    if resolve_email and aid and not email:
        try:
            from .user_service import user_service as _us
            info = _us.get_user_by_id(int(aid))
            email = (info.get('email') or '').strip().lower() if info else ''
        except Exception:
            logger.warning("PLS-539 normalize_direct_handoff: id->email resolve failed for %r",
                           aid, exc_info=True)
            email = ''
    group = (assignment_group or '').strip() or None
    reason = (reason or '').strip()
    assignee_present = bool(email or aid)
    # A TEAM route is a group with NO concrete assignee (revi #47 P1, round 4): a
    # `direct_assignment_reason` is meaningless without an assignee, so group+reason+
    # no-assignee is a team route too — NOT a third fall-through state (which let
    # /assign clear the owner instead of routing the group). A concrete assignee makes
    # it DIRECT only when it is NOT a team route, i.e. a reason accompanies it (or no
    # group is supplied); group+assignee+no-reason stays team-wins (stale hint dropped).
    is_team_route = bool(group) and not (assignee_present and reason)
    is_direct = assignee_present and not is_team_route
    return DirectHandoffCommand(
        assignee_id=aid, assignee_email=email, group=group, reason=reason,
        is_direct=is_direct, is_team_route=is_team_route)


def direct_handoff_reason_denied(actor_email, assignee_email, reason, *, is_direct=True):
    """The ONE reason gate for a DIRECT person assignment (PLAT-4871/PLS-539).

    A DIRECT override (``is_direct``) requires a ``direct_assignment_reason``, EXCEPT
    when the ACTOR is the operator (CEO) — the operator assigning directly needs no
    reason. Returns True when the call MUST be rejected. Deliberately identical for
    create, /assign and PATCH: the previously divergent PATCH exemption for the
    CEO-as-TARGET is removed (a handoff TO the operator still documents its reason),
    matching create + /assign.

    ``is_direct`` is authoritative — it comes from normalize_direct_handoff() (a
    concrete assignee, id OR email, resolved once up front), so the gate no longer
    keys on ``assignee_email`` being non-empty: an id-only direct whose email failed
    to resolve is still gated rather than silently skipped (revi #47 P1). The caller
    raises its endpoint-idiomatic exception so status codes are preserved.
    """
    if not is_direct:
        return False
    if (reason or '').strip():
        return False
    return not CEO_EMAIL or (actor_email or '').strip().lower() != CEO_EMAIL


_OVERRIDE_AUDIT_COMMENT_PREFIX = 'Direct assignee override used during task'
# Written by ``_assign_critical_unassigned`` — that path never calls
# ``record_direct_handoff``, so the watchdog must count it explicitly
# (PLAT-10519 architecture review on pulse#759).
_FORCE_ASSIGN_COMMENT_PREFIX = '🚨 **Critical unassigned override**'
_OVERRIDE_AUDIT_PREFIXES = (
    _OVERRIDE_AUDIT_COMMENT_PREFIX,
    _FORCE_ASSIGN_COMMENT_PREFIX,
)
_OVERRIDE_WATCHDOG_SOURCE = 'pulse.override_watchdog'


def _watch_override_refires(item, *, actor_email=None):
    """PLAT-10519 watchdog: same-key force-assign / direct-override re-fires.

    A task key receiving repeated assignee overrides within a day is the
    signature of an assignment loop (rebalance reclaim vs. override tug-of-war,
    misrouting, or a stuck coordinator seat re-issuing the same handoff). Two
    durable records count in the same window:

    * audit comments written by ``record_direct_handoff``
    * ``Critical unassigned override`` notices written by
      ``_assign_critical_unassigned`` (that path does not call
      ``record_direct_handoff``)

    Counting them in a trailing window is a stateless-machine-detectable
    canary — no new bookkeeping row, no migration.

    Fires a dedup'd alert when MORE than ``OVERRIDE_WATCHDOG_THRESHOLD``
    (default 2) of those comments exist on the item within the trailing
    ``OVERRIDE_WATCHDOG_WINDOW_HOURS`` (default 24). The count includes the
    comment the caller has just written, so "fire on the 3rd same-day
    force-assign" is the default contract. The alert fingerprint carries the
    item key, so each task key gets its own dedup identity and re-fires refresh
    one alert instead of paging per override.

    Best-effort by contract: any failure here is logged and swallowed — the
    override itself and its audit trail must never be blocked by the watchdog
    (same principle as the PLS-233 pin write).
    """
    try:
        threshold = int(os.environ.get('OVERRIDE_WATCHDOG_THRESHOLD', '2'))
        window_hours = float(os.environ.get('OVERRIDE_WATCHDOG_WINDOW_HOURS', '24'))
        window_start = timezone.now() - timedelta(hours=window_hours)
        prefix_q = Q()
        for prefix in _OVERRIDE_AUDIT_PREFIXES:
            prefix_q |= Q(content__startswith=prefix)
        override_count = Comment.objects.filter(
            prefix_q,
            item=item,
            created_at__gte=window_start,
        ).count()
        if override_count <= threshold:
            return None

        from .services.deduplication import AlertDeduplicationService
        item_key = (getattr(item, 'item_key', '') or f'#{item.pk}')
        description = (
            f'Force-assign/direct-override notices re-fired {override_count}x on '
            f'**{item_key}** within the last {window_hours:g}h '
            f'(threshold: >{threshold}/day). This is the repeated-override '
            're-fire signature: an assignment loop between reclaim/override '
            'paths, a misrouting, or a seat re-issuing the same handoff. '
            'Inspect the override audit comments on the carrier for the '
            'actors and reasons before assigning again.'
        )
        _alert, _is_new = AlertDeduplicationService.submit_alert({
            'title': f'Repeated direct-override re-fires on {item_key}',
            'description': description,
            'source': _OVERRIDE_WATCHDOG_SOURCE,
            'item_type': 'alert.warning',
            'severity': 'warning',
            'priority': 'high',
            'assignment_group': 'engineering',
            'correlation_id': f'override-watchdog-{item_key}',
            'labels': {
                'item_key': item_key,
                'override_count': str(override_count),
                'window_hours': str(window_hours),
            },
        })
        logger.warning(
            'override_watchdog: %s direct-override re-fires on %s within %.1fh '
            '(actor=%s) — alert %s',
            override_count, item_key, window_hours, actor_email or 'unknown',
            'fired' if _is_new else 'dedup-updated',
        )
        return _alert
    except Exception:
        logger.warning('override_watchdog: evaluation failed; override preserved',
                       exc_info=True)
        return None


def record_direct_handoff(item, *, assignee_email, reason, actor_id, actor_email,
                          is_direct, context, prev_assignee):
    """The ONE transactional side-effect writer for a direct/team handoff.

    MUST be called inside the caller's ``transaction.atomic()`` AFTER the assignee
    is persisted, so the reason audit comment is rolled back together with the
    assignee on any failure — closing the previously non-atomic /assign + PATCH
    windows that could persist a partial state. On a direct override it writes the
    reason audit comment AND the decision pin; on a non-direct reassignment (the
    owner changed without a direct reason) it releases an obsolete pin. The pin
    write stays best-effort (PLS-233: never block the assignment on metadata), but
    the comment propagates so audit and assignee are all-or-nothing. ``context`` is
    a human audit label only ('creation' / 'assignment' / 'update').
    """
    target = (assignee_email or '').strip().lower()
    reason = (reason or '').strip()
    if is_direct and target and reason:
        Comment.objects.create(
            item=item,
            author_id=actor_id or 0,
            author_email=(actor_email or 'system@pulse'),
            content=(f'Direct assignee override used during task {context}. '
                     f'Reason: {reason}'),
        )
        _persist_direct_assignment_pin(
            item, target, reason,
            actor_id=actor_id or 0, actor_email=actor_email or '')
        # PLAT-10519: same-key override re-fire watchdog. Runs after the audit
        # write so the just-written comment is counted; best-effort (never
        # blocks the override).
        _watch_override_refires(item, actor_email=actor_email)
    elif (item.assignee_email or '').strip().lower() != (prev_assignee or '').strip().lower():
        _clear_direct_assignment_pin(item, save=True)


# =============================================================================
# Unified Item ViewSet (backward compatible with TaskViewSet)
# =============================================================================

class ObjectiveViewSet(viewsets.ModelViewSet):
    """OKR/objective endpoint for linking strategic goals to Pulse work."""

    serializer_class = ObjectiveSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Objective.objects.all().prefetch_related('key_results', 'items', 'projects').annotate(
            _key_result_count=Count('key_results', distinct=True),
            _linked_item_count=Count('items', distinct=True),
            _linked_project_count=Count('projects', distinct=True),
        )
        if not (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
            memberships = getattr(user, 'organization_memberships', {}) or {}
            try:
                org_ids = [int(k) for k in memberships.keys()]
            except (TypeError, ValueError):
                org_ids = []
            qs = qs.filter(org_id__in=org_ids)

        # PLS-605 (second instance): identical unguarded construct to the one
        # reported on ProjectViewSet — a raw selector string handed to
        # `.filter(org_id=...)`. Measured on main@d1acd14: `?organization=abc`
        # raised `ValueError: Field 'org_id' expected a number but got 'abc'`
        # straight out of get_queryset. It fails WORSE than projects: there is no
        # `projects_unexpected_error`-style wrapper on this class, so it surfaces
        # as an UNLABELLED 500 that no log search by error code will ever find.
        # The membership filter above means it is not fail-open on the selector-
        # name axis the way projects was, but the crash exposure is the greater
        # of the two. Same one-line helper adoption closes it.
        org_id = _explicit_org_id(self.request)
        # `is not None`, NOT truthiness: the helper documents None as "no explicit
        # scope" and returns int(raw), so `?organization=0` normalizes to 0 and a
        # truthiness test would silently drop the selector and return the caller's
        # whole membership-scoped set. Before the helper the raw string "0" was
        # truthy and narrowed to zero rows, so truthiness here is a widening
        # regression, not a preserved behaviour (revi P1).
        if org_id is not None:
            qs = qs.filter(org_id=org_id)
        project_id = self.request.query_params.get('project') or self.request.query_params.get('project_id')
        if project_id:
            qs = qs.filter(Q(project_id=project_id) | Q(projects__id=project_id)).distinct()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        period = self.request.query_params.get('time_period')
        if period:
            qs = qs.filter(time_period=period)
        return qs

    def list(self, request, *args, **kwargs):
        """List objectives with the same fail-loud markers `projects` carries.

        PLS-605: this route had the identical unguarded selector construct AND no
        wrapper, so its 500 surfaced as a generic `server_error` with no code.
        That combination is why it stayed invisible while the labelled twin was
        reported the same day. `except APIException: raise` keeps the guard's
        403 intact — only genuinely unexpected failures are relabelled.
        """
        try:
            return super().list(request, *args, **kwargs)
        except OperationalError:
            logger.exception(
                '%s layer=db/query endpoint=/api/objectives/ action=list',
                OBJECTIVE_LIST_DB_ERROR_MARKER,
            )
            return Response(
                {
                    'detail': 'Pulse objective list failed at the database/query layer.',
                    'code': 'objectives_db_operational_error',
                    'marker': OBJECTIVE_LIST_DB_ERROR_MARKER,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except APIException:
            raise
        except Exception:
            logger.exception(
                '%s layer=unexpected endpoint=/api/objectives/ action=list',
                OBJECTIVE_LIST_UNEXPECTED_ERROR_MARKER,
            )
            return Response(
                {
                    'detail': 'Pulse objective list failed unexpectedly.',
                    'code': 'objectives_unexpected_error',
                    'marker': OBJECTIVE_LIST_UNEXPECTED_ERROR_MARKER,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(
            created_by_id=getattr(user, 'id', None),
            created_by_email=getattr(user, 'email', ''),
        )

    @action(detail=False, methods=['get'], url_path='dashboard')
    def dashboard(self, request):
        qs = self.filter_queryset(self.get_queryset())
        data = []
        for objective in qs:
            data.append({
                'id': objective.id,
                'title': objective.title,
                'org_id': objective.org_id,
                'owner_email': objective.owner_email,
                'time_period': objective.time_period,
                'status': objective.status,
                'progress_percent': objective.progress_percent,
                'key_result_count': objective.key_results.count(),
                'linked_item_count': objective.items.count(),
                'linked_project_count': objective.projects.count(),
            })
        return Response({'count': len(data), 'objectives': data})


class KeyResultViewSet(viewsets.ModelViewSet):
    serializer_class = KeyResultSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = KeyResult.objects.select_related('objective')
        if not (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
            memberships = getattr(user, 'organization_memberships', {}) or {}
            try:
                org_ids = [int(k) for k in memberships.keys()]
            except (TypeError, ValueError):
                org_ids = []
            qs = qs.filter(objective__org_id__in=org_ids)
        objective_id = self.request.query_params.get('objective') or self.request.query_params.get('objective_id')
        if objective_id:
            qs = qs.filter(objective_id=objective_id)
        return qs


class DeploymentEventViewSet(viewsets.ModelViewSet):
    """Deployment-event ledger API for CI ingestion and QA queries (PLS-183)."""
    queryset = DeploymentEvent.objects.select_related('project', 'failure_task', 'qa_task')
    serializer_class = DeploymentEventSerializer

    # PLS-906: repo + environment + deployed image tag/digest + commit SHA form
    # one coherent immutable identity. A later QA/state update may advance state
    # (state, smoke_result, timestamps, links, metadata, ...) but must never
    # replace commit provenance with an unrelated SHA while keeping the deployed
    # image fields. The idempotent ingest path keys on these fields; a bare
    # ModelViewSet PATCH/PUT previously let a writer rewrite commit_sha on an
    # existing row, producing a ledger row whose commit_sha disagreed with its
    # image/digest (live event 8b0c4433...). Reject identity mutation on update.
    _IMMUTABLE_IDENTITY_FIELDS = frozenset({
        'repo', 'service', 'commit_sha', 'environment', 'cluster',
        'image', 'image_tag', 'image_digest', 'manifest_ref',
        'pipeline_system', 'pipeline_run_id',
    })

    def _reject_identity_mutation(self, request, instance):
        attempted = {
            field for field in self._IMMUTABLE_IDENTITY_FIELDS
            if field in request.data and request.data[field] != getattr(instance, field)
        }
        if attempted:
            raise drf_serializers.ValidationError({
                field: f"DeploymentEvent identity field '{field}' is immutable after creation; "
                       f"open a new event via POST /deployment-events/ingest/ for a different "
                       f"commit/image instead of mutating this row."
                for field in sorted(attempted)
            })

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        self._reject_identity_mutation(request, instance)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        self._reject_identity_mutation(request, instance)
        return super().partial_update(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for field in ('repo', 'service', 'environment', 'cluster', 'state', 'smoke_result'):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        project_key = params.get('project_key')
        if project_key:
            qs = qs.filter(project__key__iexact=project_key)
        commit_sha = params.get('commit_sha')
        if commit_sha:
            qs = qs.filter(commit_sha__istartswith=commit_sha)
        failure_only = str(params.get('failure_only', '')).lower() in {'1', 'true', 'yes'}
        if failure_only:
            qs = qs.filter(Q(state__in=list(DeploymentEvent.FAILURE_STATES)) | Q(smoke_result='failed'))
        if getattr(self, 'action', None) == 'list':
            try:
                limit = min(max(int(params.get('limit', '100')), 1), 500)
            except ValueError:
                limit = 100
            return qs[:limit]
        return qs

    def _ingest(self, request):
        serializer = DeploymentEventIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from .services.deployment_events import upsert_deployment_event
        event, created, devops_task = upsert_deployment_event(serializer.validated_data)
        response = DeploymentEventSerializer(event, context={'request': request}).data
        response['created'] = created
        response['devops_task_key'] = getattr(devops_task, 'item_key', None) if devops_task else None
        response['devops_task_id'] = getattr(devops_task, 'id', None) if devops_task else None
        return Response(response, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        return self._ingest(request)

    @action(detail=False, methods=['post'], url_path='ingest')
    def ingest(self, request, *args, **kwargs):
        """Alias endpoint for CI jobs: POST /deployment-events/ingest/."""
        return self._ingest(request)


class VerificationRequirementManifestViewSet(viewsets.ReadOnlyModelViewSet):
    """Immutable Security-reviewed requirement generations (PLS-521)."""

    queryset = VerificationRequirementManifest.objects.select_related('item')
    serializer_class = VerificationRequirementManifestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            visible = PermissionService.get_user_item_queryset(self.request.user).values('pk')
            qs = qs.filter(item_id__in=visible)
        item_id = self.request.query_params.get('item')
        if item_id:
            qs = qs.filter(item_id=item_id)
        current = self.request.query_params.get('current')
        if str(current).lower() in {'1', 'true', 'yes'}:
            qs = qs.filter(is_current=True)
        return qs

    def create(self, request, *args, **kwargs):
        incoming = VerificationRequirementManifestCreateSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        item = incoming.validated_data['item']
        if not (request.user.is_staff or request.user.is_superuser) and not (
            PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists()
        ):
            raise NotFound('Item not found')
        from .services.workflow_service import WorkflowService
        email = (getattr(request.user, 'email', '') or '').strip().lower()
        # PLS-521 P1 #2: freezing a Security-reviewed requirement manifest is a
        # Security-team authority. `is_staff` must NOT bypass it — a staff user who
        # is not a current Security member could otherwise freeze a manifest they
        # aren't authorized to review. Only an EXPLICIT superuser break-glass is
        # allowed, and it is audited.
        if not WorkflowService._is_team_member(email, 'security', item=item):
            if request.user.is_superuser:
                logger.warning(
                    'PLS-521 break-glass: superuser %s froze a requirement manifest for '
                    'item %s without Security-team membership',
                    email or getattr(request.user, 'id', None), item.pk,
                )
            else:
                raise PermissionDenied('A current Security-team reviewer must freeze this manifest.')
        from .services.deployment_evidence import create_requirement_manifest
        values = dict(incoming.validated_data)
        values.pop('item')
        # PLS-1375 (PLAT-7689/7324): the service raises Django ValidationError
        # for every fail-closed contract violation (derivation, SHA, attestation,
        # live_required placement fields). Those previously escaped DRF and
        # surfaced as middleware 500s. Convert to a typed 400 naming the field;
        # fail-closed classification and Security freeze authority are untouched.
        try:
            manifest = create_requirement_manifest(
                item=item,
                reviewer_id=getattr(request.user, 'id', None),
                reviewer_email=email,
                **values,
            )
        except DjangoValidationError as exc:
            raise ValidationError(_normalize_validation_error_details(exc))
        # PLAT-7290 (PLAT-7066 follow-up): a frozen manifest resolves the
        # requirement_manifest_invalid hold that set the
        # merge_event_requirement_manifest_pending marker (workflow_service).
        # Clear it and re-queue the merge-event retry so the item advances
        # awaiting_merge -> verification automatically — no manual marker clear,
        # no re-firing PLS-521 alerts. Best-effort: never fail the freeze.
        try:
            from .tasks import _attempt_merge_event_retry
            meta = dict(getattr(item, 'metadata', None) or {})
            if meta.pop('merge_event_requirement_manifest_pending', None):
                # item was already object-authorized above via
                # PermissionService.get_user_item_queryset. Persist on that
                # instance — an unscoped Item.objects.filter(pk=...) is
                # DRF-AUTHZ001 even when the pk is already authorized.
                item.metadata = meta
                item.save(update_fields=['metadata'])
                try:
                    _attempt_merge_event_retry(item)
                except Exception:
                    logger.exception(
                        'PLAT-7290 merge-event retry after manifest freeze failed '
                        'for %s', getattr(item, 'item_key', item.pk),
                    )
        except Exception:
            logger.exception(
                'PLAT-7290 manifest-freeze marker clear failed for %s',
                getattr(item, 'item_key', item.pk),
            )
        return Response(
            VerificationRequirementManifestSerializer(manifest).data,
            status=status.HTTP_201_CREATED,
        )


class DeploymentEvidenceViewSet(viewsets.ReadOnlyModelViewSet):
    """Immutable CI/controller evidence snapshots and invalidation events."""

    queryset = DeploymentEvidence.objects.select_related('item')
    serializer_class = DeploymentEvidenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            visible = PermissionService.get_user_item_queryset(self.request.user).values('pk')
            qs = qs.filter(item_id__in=visible)
        item_id = self.request.query_params.get('item')
        if item_id:
            qs = qs.filter(item_id=item_id)
        return qs

    @staticmethod
    def _require_producer(request, item, *, action='write evidence'):
        from .services.workflow_service import WorkflowService
        email = (getattr(request.user, 'email', '') or '').strip().lower()
        # Deployment proof is produced by rollout/build/monitoring owners, not
        # by the implementer or the Security verifier who consumes the handoff.
        trusted_teams = ('devops', 'build-pipeline', 'monitoring')
        if any(WorkflowService._is_team_member(email, team, item=item) for team in trusted_teams):
            return
        # PLS-521 P1 #2: producer-team membership is the authority; `is_staff` must
        # NOT bypass it (a staff non-producer could otherwise forge deployment
        # proof). Only an EXPLICIT, audited superuser break-glass is allowed.
        if request.user.is_superuser:
            logger.warning(
                'PLS-521 break-glass: superuser %s used "%s" on item %s without '
                'producer-team (devops/build-pipeline/monitoring) membership',
                email or getattr(request.user, 'id', None), action, item.pk,
            )
            return
        raise PermissionDenied('Only an identified deployment/build/monitoring producer may write evidence.')

    def create(self, request, *args, **kwargs):
        incoming = DeploymentEvidenceCreateSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        item = incoming.validated_data['item']
        if not (request.user.is_staff or request.user.is_superuser) and not (
            PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists()
        ):
            raise NotFound('Item not found')
        self._require_producer(request, item)
        from .services.deployment_evidence import create_evidence_snapshot
        values = dict(incoming.validated_data)
        values.pop('item')
        # PLS-1281: the service layer raises django.core.exceptions.ValidationError
        # for deterministic by-design rejections (no current manifest for the item
        # class, source-only findings, provenance binds) — uncaught, DRF surfaced
        # them as 500, masking "wrong item class" as a server fault. Translate to
        # a typed 400.
        try:
            evidence, created = create_evidence_snapshot(
                item=item,
                producer_id=getattr(request.user, 'id', None),
                producer_identity=(getattr(request.user, 'email', '') or '').strip().lower(),
                **values,
            )
        except DjangoValidationError as exc:
            raise ValidationError(_normalize_validation_error_details(exc))
        return Response(
            {**DeploymentEvidenceSerializer(evidence).data, 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=['post'], url_path='invalidate')
    def invalidate(self, request, *args, **kwargs):
        incoming = DeploymentEvidenceInvalidateSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        item = incoming.validated_data['item']
        if not (request.user.is_staff or request.user.is_superuser) and not (
            PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists()
        ):
            raise NotFound('Item not found')
        self._require_producer(request, item, action='invalidate evidence')
        from .services.deployment_evidence import invalidate_evidence
        # PLS-1281: same django-ValidationError → typed-400 translation as create().
        try:
            cursor = invalidate_evidence(
                item=item,
                reason=incoming.validated_data['reason'],
                actor_email=(getattr(request.user, 'email', '') or '').strip().lower(),
            )
        except DjangoValidationError as exc:
            raise ValidationError(_normalize_validation_error_details(exc))
        return Response({
            'item': item.pk,
            'item_key': item.item_key,
            'requirement_generation': cursor.requirement_generation,
            'evidence_generation': cursor.current_evidence_generation,
            'reason': cursor.invalidation_reason,
        })


def _heartbeat_preflight_team_bindings(email: str) -> set[tuple[int, str]]:
    """Organization/team pairs the heartbeat principal belongs to.

    Includes junior_emails. Empty personal queues still have team unstarted
    work Hive granted a Cortex lease for (sara/revi 2026-08-22).
    """
    requested = (email or '').strip().lower()
    if not requested:
        return set()
    aliases = {requested}
    if requested.endswith('@shizuha.com'):
        aliases.add(requested[: -len('@shizuha.com')] + '@agents.shizuha.io')
    elif requested.endswith('@agents.shizuha.io'):
        aliases.add(requested[: -len('@agents.shizuha.io')] + '@shizuha.com')
    bindings = set()
    for org_id, team_name, tiers, juniors in TeamRoster.objects.filter(
        is_active=True,
    ).values_list('organization_id', 'team_name', 'tiers', 'junior_emails'):
        members = TeamRoster._flat_member_emails(tiers, juniors)
        if aliases & members:
            slug = str(team_name or '').strip().lower()
            if slug:
                bindings.add((org_id, slug))
    return bindings


class ItemViewSet(viewsets.ModelViewSet):
    """
    Unified ViewSet for Item management (tasks, actions, alerts).
    Backward compatible with existing TaskViewSet API.
    Requires authentication for all operations.

    Supports lookup by both numeric ID and item_key (e.g., AT-37).
    """
    queryset = Item.objects.all()
    serializer_class = ItemSerializer

    @staticmethod
    def _attach_list_comment_metadata(items):
        """Attach list comment metadata in one query for the selected page."""
        item_ids = [item.pk for item in items]
        if not item_ids:
            return
        # PLS-1294: attention metadata excludes system lifecycle/audit
        # comments — same classing as the my_tasks annotation and the
        # serializer fallbacks (Comment.SUBSTANTIVE_ORIGIN_KINDS).
        metadata = {
            row['item_id']: row
            for row in Comment.objects.filter(
                item_id__in=item_ids,
                origin_kind__in=Comment.SUBSTANTIVE_ORIGIN_KINDS,
            )
            .values('item_id')
            .annotate(comment_count=Count('id'), last_comment_at=Max('created_at'))
        }
        for item in items:
            row = metadata.get(item.pk)
            item._comment_count = row['comment_count'] if row else 0
            item._last_comment_at = row['last_comment_at'] if row else None

    def list(self, request, *args, **kwargs):
        """
        List items with a `permission_filtered` hint in the response so
        callers (especially AI agents) can detect when the returned set
        may be incomplete due to tenant scoping.

        This is the "hidden_by_permission_count" pattern adapted from
        multi-tenant authorization best practices (WorkOS / Auth0 guidance).
        Without this signal, a caller who legitimately has reduced
        visibility cannot distinguish "nothing exists" from "nothing
        visible to you", which is how agents confidently hallucinate
        "Nori has 0 tasks assigned" when permissions are filtering.

        We opt into the hint only when permission scoping COULD have
        dropped rows — staff/superusers trivially see everything, and
        their responses carry `permission_filtered: false`. Non-privileged
        users get `permission_filtered: true` plus, when cheap, a
        `hidden_by_permission` count indicating how many items the same
        filters would have matched with no permission scoping.
        """
        active_queue_probe = (
            request.query_params.get('mode', '').lower() == 'task'
            and bool(request.query_params.get('assignee_email'))
            and request.query_params.get('queue_order', '').lower() == 'true'
            and request.query_params.get('is_active', '').lower() == 'true'
        )
        active_queue_started = time.monotonic() if active_queue_probe else None
        permission_hint_started = None

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._attach_list_comment_metadata(page)
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
        else:
            items = list(queryset)
            self._attach_list_comment_metadata(items)
            serializer = self.get_serializer(items, many=True)
            response = Response(serializer.data)

        user = request.user
        # PLS-568: bind focus-change diagnostics to the latest *unfiltered*
        # owner queue the scheduler actually observed.  If a return transition
        # commits a new root after this response, the next rejected progress
        # write can say FOCUS_ROOT_CHANGED + refresh instead of making the
        # bounded scheduler appear inconsistent.
        queue_snapshot_params = {
            'mode', 'assignee_email', 'page_size', 'queue_order', 'is_active',
            'permission_hint',
        }
        if (
            request.query_params.get('queue_order', '').lower() == 'true'
            and request.query_params.get('assignee_email')
            and set(request.query_params).issubset(queue_snapshot_params)
            and request.query_params.get('mode', '').lower() == 'task'
            and request.query_params.get('is_active', '').lower() == 'true'
        ):
            try:
                from .services.workflow_service import WorkflowService
                requested_owner = request.query_params.get('assignee_email')
                if not WorkflowService._emails_same_agent_identity(
                    getattr(user, 'email', None),
                    requested_owner,
                ):
                    raise ValueError(
                        'queue snapshot owner must match the authenticated caller'
                    )
                payload = response.data
                rows = (
                    payload.get('results', [])
                    if isinstance(payload, dict)
                    else payload
                )
                item_ids = [
                    row.get('id')
                    for row in (rows or [])
                    if isinstance(row, dict) and row.get('id') is not None
                ]
                WorkflowService.record_owner_queue_snapshot(
                    requested_owner,
                    item_ids,
                )
            except ValueError:
                logger.info(
                    'PLS-568 ignored cross-owner queue snapshot attempt'
                )
            except Exception:
                logger.warning(
                    'PLS-568 queue-snapshot diagnostic record failed',
                    exc_info=True,
                )
        is_privileged = getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)
        hint = {
            'permission_filtered': not is_privileged,
        }

        # Compute hidden count only for non-privileged users with bounded
        # queries (has some narrowing filter), otherwise the unfiltered
        # count query can walk the full table.
        # PLS-698: this second unscoped COUNT deliberately repeats all narrowing
        # predicates, including the expensive active-queue contract.  It is
        # useful to interactive callers that display the permission hint, but
        # the fleet MCP discards both `hidden_by_permission` and its response
        # header.  Let such callers explicitly opt out rather than doubling the
        # control-plane query on every synchronized heartbeat.  Opt-out can only
        # remove diagnostic information; it never widens data visibility.
        include_permission_hint = (
            request.query_params.get('permission_hint', 'true').lower()
            not in {'false', '0', 'no'}
        )
        if not is_privileged and include_permission_hint:
            narrowing_params = {'assignee_email', 'assignee_id', 'reporter_email',
                                'reporter_id', 'created_by_id', 'project', 'sprint_id',
                                'component_id', 'item_key', 'fingerprint', 'correlation_id',
                                'status', 'workflow_name', 'workflow', 'assignment_group', 'team'}
            has_narrowing = any(k in request.query_params for k in narrowing_params)
            if has_narrowing:
                try:
                    if active_queue_probe:
                        permission_hint_started = time.monotonic()
                    # Build the "same filters without permission scope" count.
                    # Reuse get_queryset logic but start from Item.objects.all()
                    # with the same query_params by temporarily bypassing the
                    # user scope. Cheap: 28 rows today, scales with real filters.
                    unscoped_qs = self._apply_narrowing_filters(Item.objects.all())
                    total_matching = unscoped_qs.count()
                    # response.data may be paginated OR raw list depending on config
                    visible_count = response.data.get('count') if isinstance(response.data, dict) else len(response.data or [])
                    hidden = max(0, total_matching - (visible_count or 0))
                    if hidden > 0:
                        hint['hidden_by_permission'] = hidden
                except Exception as exc:
                    # Don't break the response if the hint calc fails
                    logger.debug(f"Hidden-count hint failed: {exc}")

        # Inject hint into the response envelope
        if isinstance(response.data, dict):
            response.data.update(hint)
        # For non-paginated raw lists we skip — caller reads headers instead:
        response['X-Permission-Filtered'] = '1' if hint['permission_filtered'] else '0'
        if 'hidden_by_permission' in hint:
            response['X-Hidden-By-Permission'] = str(hint['hidden_by_permission'])
        if not include_permission_hint:
            response['X-Permission-Hint-Skipped'] = '1'

        if active_queue_started is not None:
            elapsed_ms = (time.monotonic() - active_queue_started) * 1000
            hint_ms = (
                (time.monotonic() - permission_hint_started) * 1000
                if permission_hint_started is not None else 0.0
            )
            response['Server-Timing'] = (
                f'pulse-active-queue;dur={elapsed_ms:.1f}, '
                f'pulse-permission-hint;dur={hint_ms:.1f}'
            )
            if elapsed_ms >= 5000:
                # Fixed marker + bounded numeric/context fields: queriable in
                # service logs without leaking the assignee identity.
                logger.warning(
                    'PULSE_ACTIVE_QUEUE_SLOW duration_ms=%.1f '
                    'permission_hint_ms=%.1f page_size=%s hint_enabled=%s',
                    elapsed_ms,
                    hint_ms,
                    request.query_params.get('page_size', ''),
                    include_permission_hint,
                )
        return response

    def _exclude_inactive_statuses(self, queryset):
        """Apply the shared `is_active` / `is_actionable` exclusion.

        Shared by the visible queryset (`get_queryset`) and the
        hidden-by-permission count (`_apply_narrowing_filters`) so both use
        IDENTICAL active semantics. PLS-226: the count previously omitted this
        exclusion, so terminal rows became a false `hidden_by_permission` delta.
        Keep this helper in sync with the default active queue semantics:
        terminal/done, scheduled, backlog, and non-actionable waiting states
        with active implementation links are not active queue work.
        """
        return filter_active_queue_items(queryset)

    def _apply_narrowing_filters(self, queryset):
        """
        Re-apply the request's narrowing filters (assignee, project, etc.) to
        an unscoped queryset. Used to compute the hidden-by-permission count.
        Intentionally ignores the permission-scoping filters from
        `_get_user_scoped_queryset` — that's the whole point — but DOES re-apply
        the is_active/is_actionable terminal exclusion (PLS-226) so the count
        matches the visible queryset's active semantics.
        """
        p = self.request.query_params
        if self._is_task_endpoint():
            queryset = queryset.filter(mode='task')
        elif p.get('mode'):
            queryset = queryset.filter(mode=p['mode'])
        if p.get('assignee_email'):
            emails = [e.strip() for e in p['assignee_email'].split(',') if e.strip()]
            # PLAT-533: expand to both @shizuha.com and @agents.shizuha.io aliases
            expanded = set(emails)
            for e in list(emails):
                if e.endswith('@shizuha.com'):
                    expanded.add(e[:-len('@shizuha.com')] + '@agents.shizuha.io')
                elif e.endswith('@agents.shizuha.io'):
                    expanded.add(e[:-len('@agents.shizuha.io')] + '@shizuha.com')
            queryset = queryset.filter(assignee_email__in=expanded)
        if p.get('assignee_id'):
            ids = [int(x) for x in p['assignee_id'].split(',') if x.strip().isdigit()]
            if ids:
                queryset = queryset.filter(assignee_id__in=ids)
        if p.get('reporter_email'):
            emails = [e.strip() for e in p['reporter_email'].split(',') if e.strip()]
            queryset = queryset.filter(created_by_email__in=emails)
        if p.get('project'):
            pids = [int(x) for x in str(p['project']).split(',') if x.strip().isdigit()]
            if pids:
                queryset = queryset.filter(project_id__in=pids)
        if p.get('status'):
            statuses = [s.strip() for s in p['status'].split(',') if s.strip()]
            queryset = queryset.filter(status__in=statuses)
        if p.get('item_key'):
            queryset = queryset.filter(item_key=p['item_key'])
        wf = p.get('workflow_name') or p.get('workflow')
        if wf:
            queryset = queryset.filter(workflow_name__iexact=wf)
        ag = p.get('assignment_group') or p.get('team')
        if ag:
            queryset = queryset.filter(assignment_group=ag)
        # PLS-226: match the visible queryset's active semantics so terminal
        # rows are not counted as hidden-by-permission.
        is_active = p.get('is_active', '').lower() == 'true'
        is_actionable = p.get('is_actionable', '').lower() == 'true'
        if is_active or is_actionable:
            queryset = self._exclude_inactive_statuses(queryset)
        return queryset

    def get_object(self):
        """Support lookup by item_key, UUID, numeric-only key, and #display key."""
        lookup = self.kwargs.get(self.lookup_field, '')
        obj = _resolve_item_identifier(lookup, mode=None)
        if obj:
            self.check_object_permissions(self.request, obj)
            return obj
        from rest_framework.exceptions import NotFound
        raise NotFound(_format_item_identifier_error(lookup))
    # IsAuthenticated is inherited from settings
    # ItemProjectPermission handles both personal items and project-based permissions
    permission_classes = [permissions.IsAuthenticated, ItemProjectPermission]

    def get_serializer_class(self):
        """Return appropriate serializer based on action and mode."""
        if self.action == 'create':
            return ItemCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return ItemUpdateSerializer
        elif self.action == 'list':
            return ItemListSerializer
        elif self.action == 'recurring_tasks':
            return RecurringTaskSerializer
        return ItemSerializer

    def _is_task_endpoint(self):
        """True for the legacy /tasks alias, which must never include alerts/actions."""
        if getattr(self, 'basename', None) == 'task':
            return True
        match = getattr(self.request, 'resolver_match', None)
        name = getattr(match, 'url_name', '') or ''
        return name.startswith('task-')

    def _get_user_scoped_queryset(self):
        """
        Get base queryset scoped to the current user.

        Access rules:
        - Personal items (no project): Owner (assignee or creator) only
        - Project items: Based on project membership and access level
        - Staff users: Full access to everything

        Uses PermissionService for project-aware filtering.
        """
        user = self.request.user

        # Get project and organization filters from request.  If the caller
        # explicitly scopes to an org, it must be one of their orgs; silently
        # returning an empty/non-leaking 200 for a wrong-org request violates the
        # HIVE-327 tenant-isolation probe contract.
        project_id = self.request.query_params.get('project')
        organization_id = _ensure_explicit_org_membership(self.request)
        include_personal = self.request.query_params.get('include_personal', 'true').lower() == 'true'

        return PermissionService.get_user_item_queryset(
            user=user,
            include_personal=include_personal,
            project_id=project_id,
            organization_id=organization_id
        ).select_related('project', 'parent_item', 'containment_decision')

    def get_queryset(self):
        """Filter queryset based on query parameters and user scope."""
        # Start with user-scoped queryset
        queryset = self._get_user_scoped_queryset()

        # Archive handling.
        #   - Default: exclude archived items from all queries (they're
        #     auto-archived Done-category tasks older than N days, surfaced
        #     only via the /archive view).
        #   - include_archived=true → don't filter (needed for explicit
        #     "show everything" queries like export tools or admin debugging).
        #   - archived_only=true → show ONLY archived items (the /archive view).
        archived_param = self.request.query_params.get('archived', '').lower()
        include_archived = self.request.query_params.get('include_archived', '').lower() == 'true'
        if archived_param == 'only' or self.request.query_params.get('archived_only', '').lower() == 'true':
            queryset = queryset.filter(is_archived=True)
        elif not include_archived:
            queryset = queryset.filter(is_archived=False)
            # PLS-7 #6: also hide items belonging to a SOFT-archived (deleted)
            # project from the default scope. The legacy DELETE / bulk-delete
            # paths now soft-archive the Project instead of permanently deleting
            # it, so without this its tasks would keep surfacing in /api/items and
            # dashboards. Personal (null-project) items are unaffected;
            # include_archived / archived_only still expose them.
            queryset = queryset.exclude(project__is_archived=True)

        # Filter by mode
        mode = self.request.query_params.get('mode')
        if self._is_task_endpoint():
            queryset = queryset.filter(mode='task')
        elif mode:
            queryset = queryset.filter(mode=mode)

        # Filter: assigned_to_me
        # PLS-23: email is the canonical cross-service identity key. Using
        # assignee_id alone can collide with drifted historical records where
        # Django local pk=N mapped to a different person than shizuha-id=N.
        # Primary filter: email match (always available for SSO/FederatedUser).
        # Fallback: id match only for tasks where assignee_email is NULL (e.g.
        # very old pre-email-schema records) — the NULL guard prevents pulling in
        # cross-contaminated rows whose email field identifies a different user.
        if self.request.query_params.get('assigned_to_me', '').lower() == 'true':
            user = self.request.user
            if getattr(user, 'email', None):
                # PLAT-533: match both @shizuha.com and @agents.shizuha.io forms.
                # Some assignment paths (failover sweep, availability resolver) write
                # the @agents.shizuha.io alias while the JWT identity is @shizuha.com.
                # Pattern mirrors the throughput watchdog (tasks.py lines 2295-2316).
                _ue = user.email.lower()
                _email_q = Q(assignee_email__iexact=_ue)
                if _ue.endswith('@shizuha.com'):
                    _email_q |= Q(assignee_email__iexact=_ue[:-len('@shizuha.com')] + '@agents.shizuha.io')
                elif _ue.endswith('@agents.shizuha.io'):
                    _email_q |= Q(assignee_email__iexact=_ue[:-len('@agents.shizuha.io')] + '@shizuha.com')
                user_filter = (
                    _email_q
                    | (Q(assignee_id=user.id) & (Q(assignee_email__isnull=True) | Q(assignee_email='')))
                )
            else:
                user_filter = Q(assignee_id=user.id)
            queryset = queryset.filter(user_filter)

        # Filter by assignee — supports comma-separated IDs for multi-select
        # Rules:
        # - Superusers: can filter by ANY user across all orgs
        # - Staff: can filter by any user
        # - Org members: can filter by assignee if they share a project (excludes personal items)
        assignee_id = self.request.query_params.get('assignee_id')
        if assignee_id:
            assignee_ids = []
            for part in str(assignee_id).split(','):
                try:
                    assignee_ids.append(int(part.strip()))
                except (ValueError, TypeError):
                    continue

            if assignee_ids:
                if self.request.user.is_superuser or self.request.user.is_staff:
                    queryset = queryset.filter(assignee_id__in=assignee_ids)
                elif len(assignee_ids) == 1 and assignee_ids[0] == self.request.user.id:
                    queryset = queryset.filter(assignee_id__in=assignee_ids)
                else:
                    # Org member filtering by other members — only project items
                    queryset = queryset.filter(
                        assignee_id__in=assignee_ids,
                        project__isnull=False
                    )

        # Filter by assignee email — supports comma-separated for multi-select
        assignee_email = self.request.query_params.get('assignee_email')
        if assignee_email:
            emails = [e.strip() for e in str(assignee_email).split(',') if e.strip()]
            if emails:
                # PLAT-533: expand each email to its alternate alias so tasks
                # assigned via the @agents.shizuha.io form appear when queried
                # with @shizuha.com (and vice-versa).
                expanded = set(emails)
                for e in list(emails):
                    if e.endswith('@shizuha.com'):
                        expanded.add(e[:-len('@shizuha.com')] + '@agents.shizuha.io')
                    elif e.endswith('@agents.shizuha.io'):
                        expanded.add(e[:-len('@agents.shizuha.io')] + '@shizuha.com')
                queryset = queryset.filter(assignee_email__in=expanded)

        # Filter by reporter email — supports comma-separated for multi-select
        reporter_email = self.request.query_params.get('reporter_email')
        if reporter_email:
            emails = [e.strip() for e in str(reporter_email).split(',') if e.strip()]
            if emails:
                queryset = queryset.filter(created_by_email__in=emails)

        # Filter by reporter (created_by) — supports comma-separated IDs for multi-select
        reporter_id = self.request.query_params.get('reporter_id') or self.request.query_params.get('created_by_id')
        if reporter_id:
            reporter_ids = []
            for part in str(reporter_id).split(','):
                try:
                    reporter_ids.append(int(part.strip()))
                except (ValueError, TypeError):
                    continue

            if reporter_ids:
                if self.request.user.is_superuser or self.request.user.is_staff:
                    queryset = queryset.filter(created_by_id__in=reporter_ids)
                elif len(reporter_ids) == 1 and reporter_ids[0] == self.request.user.id:
                    queryset = queryset.filter(created_by_id__in=reporter_ids)
                else:
                    queryset = queryset.filter(
                        created_by_id__in=reporter_ids,
                        project__isnull=False
                    )

        # Direct item_key lookup (e.g. ?item_key=PLAT-123) — the home cockpit's
        # task-peek fetches one item by its human key through the same
        # permission-scoped list path (HIVE-602).
        item_key_filter = (self.request.query_params.get('item_key') or '').strip()
        if item_key_filter:
            queryset = queryset.filter(item_key__iexact=item_key_filter)

        # Filter by status (supports comma-separated values for multiple statuses)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            if ',' in status_filter:
                status_list = [s.strip() for s in status_filter.split(',')]
                queryset = queryset.filter(status__in=status_list)
            else:
                queryset = queryset.filter(status=status_filter)

        # Filter by status_category (comma-separated — todo / in_progress / done).
        # This is the Jira-style "Status Category" filter — matches against the
        # Status.category column via the status slug. Used as the day-to-day
        # "progress" filter for the 3-bucket mental model (Pending / InProgress / Done);
        # the `status` filter above remains for precise workflow-slug drill-down.
        # Both can be combined (AND) — e.g. status_category=in_progress AND
        # status=awaiting_merge narrows to only that specific slug.
        status_category_filter = self.request.query_params.get('status_category')
        if status_category_filter:
            cats = [c.strip() for c in status_category_filter.split(',') if c.strip()]
            from .models import Status
            slugs_in_cats = list(
                Status.objects.filter(category__in=cats).values_list('slug', flat=True)
            )
            if slugs_in_cats:
                queryset = queryset.filter(status__in=slugs_in_cats)
            else:
                # No statuses match the requested categories — return empty
                queryset = queryset.none()

        # Filter: is_active=true / is_actionable=true → exclude terminal/done
        # statuses across all workflows. Both params are now equivalent; the
        # old `is_actionable` additionally hid recurring tasks scheduled >24h
        # out, but that behavior was non-standard (Jira / Linear / Asana /
        # GitHub Projects all show every non-terminal task regardless of due
        # date) and produced surprises like AT-40 disappearing because its
        # next weekly scan was 80h away. Users can sort/filter by due date
        # explicitly if they want "due today" or similar narrower scopes.
        is_active = self.request.query_params.get('is_active', '').lower() == 'true'
        is_actionable = self.request.query_params.get('is_actionable', '').lower() == 'true'
        if is_active or is_actionable:
            # Shared with the hidden-by-permission count so both paths use
            # identical active semantics (PLS-226), including backlog and
            # non-actionable linked-implementation waiting states from upstream.
            queryset = self._exclude_inactive_statuses(queryset)

        # Board-view window on Done tasks: limit done-category items to those
        # resolved within the last N days. Matches Jira Kanban's "Hide
        # completed issues older than" default (2 weeks). Prevents the Done
        # column from ballooning into thousands of historical items.
        # Only applies when the caller explicitly asks — list view doesn't
        # send this so a user filtering for "Done" in list view still sees
        # everything.
        done_recent_days = self.request.query_params.get('done_recent_days')
        if done_recent_days:
            try:
                days = max(1, int(done_recent_days))
            except ValueError:
                days = 14
            from .models import Status
            done_slugs = list(
                Status.objects.filter(category='done').values_list('slug', flat=True)
            )
            if done_slugs:
                cutoff = timezone.now() - timedelta(days=days)
                # Keep everything NOT in done-category, OR done-category items
                # updated within the window (resolution_date preferred, falls
                # back to updated_at for items that never set resolution_date).
                queryset = queryset.filter(
                    ~Q(status__in=done_slugs)
                    | Q(resolution_date__gte=cutoff)
                    | Q(resolution_date__isnull=True, updated_at__gte=cutoff)
                )

        # Filter by item_type (supports prefix matching)
        item_type = self.request.query_params.get('item_type')
        if item_type:
            if item_type.endswith('*'):
                queryset = queryset.filter(item_type__startswith=item_type[:-1])
            else:
                queryset = queryset.filter(item_type=item_type)

        # Legacy: Filter by task_type
        task_type = self.request.query_params.get('task_type')
        if task_type:
            queryset = queryset.filter(item_type__contains=task_type)

        # Filter by priority (supports comma-separated values for multi-select)
        priority = self.request.query_params.get('priority')
        if priority:
            if ',' in priority:
                plist = [p.strip() for p in priority.split(',') if p.strip()]
                queryset = queryset.filter(priority__in=plist)
            else:
                queryset = queryset.filter(priority=priority)

        # Filter by severity (for alerts)
        severity = self.request.query_params.get('severity')
        if severity:
            queryset = queryset.filter(severity=severity)

        # Filter by source
        source = self.request.query_params.get('source')
        if source:
            queryset = queryset.filter(source=source)

        # Filter by source_id — exact match on the external stable identity.
        # Backed by the (source, source_id) index (migration 0005). Lets an
        # integration (e.g. security-ci-to-pulse) reliably look up an existing
        # item by its stable source_id across ALL statuses without depending on
        # the list serializer surfacing description/source_id (it does NOT),
        # which is what made repeated security-ci runs flood duplicate findings
        # (PLAT-2688). Combine with source= for an index-covered exact match.
        source_id = self.request.query_params.get('source_id')
        if source_id:
            queryset = queryset.filter(source_id=source_id)

        # Filter by due date range (use __date to compare date portion of DateTimeField)
        due_after = self.request.query_params.get('due_after')
        due_before = self.request.query_params.get('due_before')
        if due_after:
            queryset = queryset.filter(due_date__date__gte=due_after)
        if due_before:
            queryset = queryset.filter(due_date__date__lte=due_before)

        # Search by item_key/title/description/comments. Keep exact substring
        # matching for item pickers, then add token-AND matching so long alert
        # phrases still find prior decisions with slightly different wording
        # (e.g. PLAT-1057 vs deferred PLAT-666).
        search = self.request.query_params.get('search', '').strip()
        if search:
            if _is_exact_item_key_search(search):
                # Exact public keys are indexed and globally unique.  Running
                # the compatibility spelling through the broad search below
                # needlessly scans item text plus the comments semi-join: live
                # CTX-479 reads took 13-20s and eventually hit PostgreSQL's 15s
                # statement_timeout.  Keep permission/mode scoping from the
                # base queryset, but make the search predicate a keyed read.
                queryset = queryset.filter(item_key=search.upper())
            else:

                def _item_fields_contain(needle, prefix=''):
                    return (
                        Q(**{f'{prefix}item_key__icontains': needle})
                        | Q(**{f'{prefix}title__icontains': needle})
                        | Q(**{f'{prefix}description__icontains': needle})
                    )

                def _matches_a_comment(needle):
                    """Comment match as a semi-join, never a related-field lookup.

                `Q(comments__content__icontains=x)` is the obvious spelling and it
                is the one that took Pulse down on 2026-07-26. Django compiles it
                to a join onto tasks_comment — one join, not one per token, but
                that single join is enough. It fans 10,591 items out to 192,126
                rows, and every fanned row carries the ENTIRE item: the planner
                reports `width=3818` across ~95 columns, so the join materialises
                roughly 730MB. Postgres then evaluates the search predicates
                AFTER the fan-out, so an 8-token search runs up to 32 ILIKEs per
                fanned row rather than per item, and the trailing .distinct()
                has to Sort+Unique that whole wide set on a 95-column sort key.

                Measured on the live database: 80-93s per query, wait_event
                IPC/ExecuteGather, no lock waits, against a table of 10,591 rows.
                EXPLAIN shows `Parallel Hash Right Join` -> `Sort` -> `Unique`
                at width=3818.

                Then it became an outage rather than a slow page. gunicorn serves
                :8002 from 5 sync workers, so a few of these fill a pod; the
                readiness probe queues behind them and fails; the pod leaves the
                Service; the survivors absorb more load. Agents whose search
                timed out retried, and search-before-create with 3-6 specific
                words is exactly what pulse-core and wiki-lifecycle instruct
                every agent to do — so the fleet's own discipline was the load
                generator.

                `pk__in` on a .values('item_id') subquery compiles to a narrow
                SubPlan returning item_id alone (`width=8`), while item-side
                ILIKEs and DISTINCT run over the unfanned item rows.
                """
                    return Q(pk__in=Comment.objects.filter(
                        content__icontains=needle,
                    ).values('item_id'))

                def _tokens_match_one_comment(needles):
                    """Preserve the legacy same-comment token-AND contract.

                The old related-field lookup reused one comment join alias for
                every token. Therefore comment-only tokens had to co-occur in
                one comment; splitting them across comments did not match.
                Independent per-token semi-joins widened the result set by
                allowing a different comment to witness each token.

                Keep all token predicates inside one narrow Comment subquery.
                Item-field alternatives intentionally traverse `item__` inside
                that same subquery, so mixed item/comment matches retain the
                legacy behaviour without fanning the outer item queryset.
                """
                    comments = Comment.objects.all()
                    for needle in needles:
                        comments = comments.filter(
                            _item_fields_contain(needle, prefix='item__')
                            | Q(content__icontains=needle)
                        )
                    return Q(pk__in=comments.values('item_id'))

                search_fields = (
                    _item_fields_contain(search)
                    | _matches_a_comment(search)
                )
                tokens = [
                    token for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', search)
                    if len(token) > 2
                ][:8]
                all_item_token_fields = None
                for token in tokens:
                    item_token_q = _item_fields_contain(token)
                    all_item_token_fields = (
                        item_token_q if all_item_token_fields is None
                        else all_item_token_fields & item_token_q
                    )
                if all_item_token_fields is None:
                    search_query = search_fields
                else:
                    # The top-level item-only arm is required for items with zero
                    # comments. The subquery arm preserves the old rule that every
                    # comment-witnessed token resolves against the same comment row.
                    token_fields = (
                        all_item_token_fields
                        | _tokens_match_one_comment(tokens)
                    )
                    search_query = search_fields | token_fields
                # .distinct() stays: the comment axis no longer fans out, but earlier
                # filters in this method can still join a multi-valued relation, and
                # de-duplicating ~10k unfanned rows is cheap. It was never the
                # DISTINCT that was expensive — it was what it had to de-duplicate.
                queryset = queryset.filter(search_query).distinct()

        # Filter by tags
        tags = self.request.query_params.get('tags')
        if tags:
            tag_list = [tag.strip() for tag in tags.split(',')]
            for tag in tag_list:
                queryset = queryset.filter(tags__icontains=tag)

        # PLS-712: task labels are persisted as both flat string arrays and
        # key/value objects.  The canonical predicate matches both shapes and
        # rejects malformed input instead of silently widening the result set.
        labels = self.request.query_params.get('labels')
        if labels:
            try:
                label_tokens = parse_label_filter(labels)
            except InvalidLabelFilter as exc:
                raise drf_serializers.ValidationError({'labels': str(exc)}) from exc
            queryset = queryset.filter(item_labels_match_q(label_tokens))

        # Filter by recurring status
        is_recurring = self.request.query_params.get('is_recurring')
        if is_recurring is not None:
            queryset = queryset.filter(is_recurring=is_recurring.lower() == 'true')

        # Filter by sprint
        sprint_id = self.request.query_params.get('sprint')
        if sprint_id:
            if sprint_id.lower() == 'none' or sprint_id.lower() == 'backlog':
                # Items not in any sprint (backlog)
                queryset = queryset.filter(sprint__isnull=True)
            else:
                queryset = queryset.filter(sprint_id=sprint_id)

        # Filter by parent_item (for subtasks)
        parent_item_id = self.request.query_params.get('parent_item')
        if parent_item_id:
            queryset = queryset.filter(parent_item_id=parent_item_id)

        # Phase 3: Filter by component
        component_id = self.request.query_params.get('component')
        if component_id:
            if component_id.lower() == 'none':
                queryset = queryset.filter(component__isnull=True)
            else:
                queryset = queryset.filter(component_id=component_id)

        # Filter by assignment_group (team-based assignment)
        # `team` is accepted as a friendlier alias (PLS-24).
        assignment_group = (
            self.request.query_params.get('assignment_group')
            or self.request.query_params.get('team')
        )
        if assignment_group:
            queryset = queryset.filter(assignment_group=assignment_group)

        # Filter by workflow name — supports the slug form used in pulse_create_task
        # and the display-name form used in the UI (PLS-24: agents need to scope
        # `status` queries to a specific workflow to avoid cross-workflow slug collisions).
        # Accepts `workflow_name` or the shorter `workflow` alias.
        workflow_filter = (
            self.request.query_params.get('workflow_name')
            or self.request.query_params.get('workflow')
        )
        if workflow_filter:
            queryset = queryset.filter(workflow_name__iexact=workflow_filter)

        # Phase 3: Filter by fix_version
        fix_version_id = self.request.query_params.get('fix_version')
        if fix_version_id:
            if fix_version_id.lower() == 'none':
                queryset = queryset.filter(fix_version__isnull=True)
            else:
                queryset = queryset.filter(fix_version_id=fix_version_id)

        # Phase 3: JQL query support
        jql = self.request.query_params.get('jql')
        if jql:
            try:
                from .services.jql_parser import JQLParser, JQLSyntaxError
                from rest_framework.exceptions import ValidationError as _JQLValidationError
                parser = JQLParser()
                q_object = parser.parse(jql, self.request.user)
                queryset = queryset.filter(q_object)
            except JQLSyntaxError as e:
                # PLS-831: malformed JQL must fail explicitly (4xx), never
                # silently degrade to an unfiltered query. DRF turns
                # ValidationError into a 400 so the UI and MCP surface the same
                # explicit error and no task rows leak from a rejected filter.
                logger.warning(f"JQL parse error: {e}")
                raise _JQLValidationError({'jql': f"Invalid JQL: {e}"})
            except Exception as e:
                # Any unexpected execution error — fail explicitly (400) rather
                # than silently returning an unfiltered queue (PLS-831).
                logger.warning(f"JQL execution error: {e}")
                raise _JQLValidationError({'jql': f"Invalid JQL: {e}"})

        if self.request.query_params.get('queue_order', '').lower() == 'true':
            affinity_rank = self._project_affinity_queue_rank()
            if affinity_rank is not None:
                queryset = queryset.annotate(_project_affinity_rank=affinity_rank)

            # Agent work queue policy:
            # 1. Ready work before waiting work (blocked/scheduled)
            # 2. Highest effective priority across every ready project. A
            #    runnable blocker inherits the highest priority in its active
            #    blocked descendant graph, so release work cannot sit behind
            #    unrelated lower-impact work.
            # 3. If enabled, project-affinity breaks ties within a priority:
            #    primary -> secondary -> overflow.
            # 4. Within a (ready, priority, affinity) bucket, work CLOSEST TO DONE first —
            #    advance/finish near-complete items before starting new ones
            #    ("stop starting, start finishing"). This floats a merger's
            #    awaiting_merge backlog above same-priority review/impl work, a
            #    reviewer's in_review above its impl work, a deployer's
            #    awaiting_deploy first, etc. within the same affinity bucket.
            #    Operator directive 2026-06-15.
            # 5. FIFO inside that (priority, stage) bucket
            queryset = annotate_owner_queue_projection(queryset)
            queryset = queryset.annotate(
                _queue_wait_rank=Case(
                    # Shared with focus selection: every scheduled/backlog
                    # category, explicit wait, active-upstream hold, and
                    # authored implementation handoff is awareness-only. It
                    # also owns the PLS-73/PLS-87 future recurring rule; do not
                    # add queue-only waiting predicates here or focus drifts.
                    When(_queue_is_waiting=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
                _queue_priority_rank=Case(
                    When(priority='urgent', then=Value(0)),
                    When(priority='high', then=Value(1)),
                    When(priority='normal', then=Value(2)),
                    When(priority='medium', then=Value(2)),
                    When(priority='low', then=Value(3)),
                    default=Value(4),
                    output_field=IntegerField(),
                ),
            )
            order_fields = [
                '_queue_wait_rank',
                '_queue_effective_priority_rank',
                '-_queue_is_blocker_root',
            ]
            if affinity_rank is not None:
                order_fields.append('_project_affinity_rank')
            order_fields.extend(['_queue_stage_rank', 'created_at', 'id'])
            return self._list_defer_fat_columns(queryset.order_by(*order_fields))

        return self._list_defer_fat_columns(queryset.order_by('-created_at'))

    def _list_defer_fat_columns(self, queryset):
        """Drop wide text/JSON columns from list SELECTs.

        ItemListSerializer does not need description/payload/etc. Leaving them
        in the SELECT made `SELECT DISTINCT …` (permission-scoping joins) copy
        multi-KB rows per task and turned COUNT(DISTINCT) into multi-minute
        queries under home-dashboard polling.
        """
        if getattr(self, 'action', None) != 'list':
            return queryset
        # The CI notifier stores immutable run identity in payload. Expose
        # only its small typed fields, keeping wide payloads deferred and
        # avoiding one deferred-field fetch per alert during list rendering.
        return queryset.annotate(_origin_ci_payload=JSONObject(
            run_id=KeyTextTransform('run_id', 'payload'),
            run_number=KeyTextTransform('run_number', 'payload'),
            commit_sha=KeyTextTransform('commit_sha', 'payload'),
            status=KeyTextTransform('status', 'payload'),
        )).defer(
            'description',
            'payload',
            'execution_result',
            'error_message',
            'callback_headers',
            'callback_url',
            'resolution_note',
            'silence_reason',
        )

    def _project_affinity_queue_rank(self):
        """EVOL-20 Ph1a: optional project-affinity bucket for agent queues.

        Flag off returns None so queue_order SQL/order is unchanged. Flag on uses
        active AgentProjectMembership rows for the requesting agent: primary
        project tasks, then secondary project tasks, then org-wide overflow when
        enabled. The rank is applied after ready/waiting and effective priority,
        so a blocked primary task never hides ready work and a lower-priority
        primary task never hides an urgent overflow task.
        """
        if not getattr(settings, 'PULSE_PROJECT_AFFINITY_QUEUE_ORDER_ENABLED', False):
            return None

        assignee_email = (self.request.query_params.get('assignee_email') or '').strip().lower()
        if ',' in assignee_email:
            return None
        user = getattr(self.request, 'user', None)
        if not assignee_email:
            assignee_email = (getattr(user, 'email', '') or '').strip().lower()
        if not assignee_email:
            return None

        now = timezone.now()
        membership_filter = Q(active=True, agent_user_email__iexact=assignee_email)
        user_id = getattr(user, 'id', None)
        user_email = (getattr(user, 'email', '') or '').strip().lower()
        if user_id and (
            not self.request.query_params.get('assignee_email')
            or assignee_email == user_email
        ):
            membership_filter |= Q(active=True, agent_user_id=user_id)
        memberships = AgentProjectMembership.objects.filter(membership_filter).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        primary_ids = list(memberships.filter(tier='primary').values_list('project_id', flat=True))
        secondary_ids = list(memberships.filter(tier='secondary').values_list('project_id', flat=True))
        if not primary_ids and not secondary_ids:
            return None

        if not getattr(settings, 'PULSE_PROJECT_AFFINITY_OVERFLOW_ENABLED', True):
            return Case(
                When(project_id__in=primary_ids, then=Value(0)),
                When(project_id__in=secondary_ids, then=Value(1)),
                default=Value(99),
                output_field=IntegerField(),
            )
        return Case(
            When(project_id__in=primary_ids, then=Value(0)),
            When(project_id__in=secondary_ids, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )

    def perform_destroy(self, instance):
        """Delete a task without losing a race to its assignment notification.

        Notification rows reference the task through a deferred foreign key.
        The assignment notice is written by a worker after create commits. If
        that insert commits after this request has snapshotted children and
        before it commits, PostgreSQL rejects the delete at COMMIT and the
        API returns 500. Locking the task first makes that insert wait until
        the row is gone, so the collector sees every notice that landed.
        """
        with transaction.atomic():
            locked = (
                Item.objects.select_for_update()
                .filter(pk=instance.pk)
                .first()
            )
            if locked is None:
                return
            locked.delete()

    def create(self, request, *args, **kwargs):
        """HIVE-1223: guarantee a fresh org (zero projects) can create its FIRST
        task. When ``mode=task``, no ``project`` is supplied, and the resolved org
        has no projects, lazily provision the org's canonical default project and
        attach the task to it — so EVERY caller (raw ``POST /api/tasks/`` and MCP
        agents, not only the Hive CTA) can create a first task instead of hitting
        the 'A task must belong to an organization project.' 400. The hot path
        (project supplied, or the org already has projects) is byte-unchanged: it
        falls straight through to the default ModelViewSet.create.
        """
        data = request.data
        if data.get('mode', 'task') == 'task' and not data.get('project'):
            org_id = _ensure_explicit_org_membership(request)
            org_default = None
            if org_id:
                # Attach a no-project task to the org's DEFAULT project. Provision
                # it only for a truly fresh org (zero projects); an org that already
                # has a default reuses it (so the 2nd..Nth bare task lands there too,
                # not only the first). An org with only user-created (non-default)
                # projects has NO default -> fall through to the original 400 so the
                # caller picks a project.
                org_default = Project.objects.filter(
                    org_id=org_id, is_default=True, is_active=True,
                ).first()
                if org_default is None and not Project.objects.filter(
                    org_id=org_id, is_active=True,
                ).exists():
                    org_default = _ensure_org_default_project(org_id, request.user)
            if org_default is not None:
                patch = {'project': org_default.id}
                # A bare first task (no assignee, no team) would otherwise default
                # to the platform 'engineering' team — which a fresh customer org
                # has no roster for, re-breaking first-task create at the team gate.
                # Onboarding's first task is a PERSONAL task owned by its creator:
                # self-assign it (with an audit reason, since the direct-handoff gate
                # requires one for a non-CEO direct assignment) so it routes to the
                # creator, not a non-existent team.
                if (not data.get('assignee_id') and not data.get('assignee_email')
                        and not data.get('assignment_group')):
                    patch['assignee_id'] = request.user.id
                    patch['assignee_email'] = request.user.email
                    patch['direct_assignment_reason'] = (
                        'HIVE-1223: personal task self-assigned to its creator '
                        '(org default project; no team/assignee specified)'
                    )
                # reika P3-2: this flattens multi-value keys to their last value if
                # `data` is a form-encoded QueryDict. Assumes every caller of the
                # onboarding first-task path is a JSON client (the Hive CTA and the
                # MCP/API callers are); a repeated form param would be silently
                # collapsed here. Revisit with `QueryDict.lists()` if a
                # form-encoded client is ever in scope for this path.
                merged = {**data, **patch}
                # The self-assign reason feeds perform_create's direct-handoff gate,
                # which reads it from request.data; hand it over via an instance attr
                # the gate falls back to (see perform_create) rather than mutating a
                # private DRF attr. The same viewset instance serves create() +
                # perform_create within one request, and the attr is unset on the
                # normal (non-onboarding) create path.
                self._hive1223_onboarding_reason = patch.get('direct_assignment_reason')
                serializer = self.get_serializer(data=merged)
                serializer.is_valid(raise_exception=True)
                self.perform_create(serializer)
                headers = self.get_success_headers(serializer.data)
                return Response(
                    serializer.data, status=status.HTTP_201_CREATED, headers=headers,
                )
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def perform_create(self, serializer):
        """Create item with additional processing."""
        user = self.request.user
        explicit_org_id = _ensure_explicit_org_membership(self.request)

        # Set created_by to the authenticated user
        # If no assignee specified, assign to self (for tasks)
        extra_data = {
            'created_by_id': user.id,
            'created_by_email': user.email,
        }
        direct_assignment_reason = (
            getattr(self, '_hive1223_onboarding_reason', None)
            or self.request.data.get('direct_assignment_reason') or ''
        ).strip()
        direct_assignee_email = serializer.validated_data.get('assignee_email')
        direct_assignee_id = serializer.validated_data.get('assignee_id')
        explicit_team = serializer.validated_data.get('assignment_group')
        if explicit_team:
            from .services.workflow_service import WorkflowService as _CreateTeamService
            explicit_team = _CreateTeamService._canonical_team_name(explicit_team)
            serializer.validated_data['assignment_group'] = explicit_team
            probe_item = _CreateTeamService.create_permission_probe_item(
                serializer.validated_data,
                extra_data=extra_data,
                assignment_group=explicit_team,
            )
            if not _CreateTeamService._team_registered_for_item(
                explicit_team,
                probe_item,
            ):
                raise drf_serializers.ValidationError({
                    'assignment_group': (
                        'Team is not registered for the selected project organization.'
                    ),
                })
        # PLS-539 (revi #47 P1): ONE canonical command drives the gate + the audit,
        # consumed UNCHANGED here as at /assign + PATCH. A group with NO reason is a
        # TEAM route — a co-supplied assignee is a stale hint and is DROPPED so the
        # item is team-resolved (never persisted as an unaudited direct owner);
        # otherwise a concrete assignee is a DIRECT override that needs a reason.
        _handoff = normalize_direct_handoff(
            assignee_id=direct_assignee_id,
            assignee_email=direct_assignee_email,
            assignment_group=explicit_team,
            reason=direct_assignment_reason,
        )
        if direct_handoff_reason_denied(
                user.email, _handoff.assignee_email, _handoff.reason,
                is_direct=_handoff.is_direct):
            raise drf_serializers.ValidationError({
                'assignee_email': (
                    'Direct person assignment requires direct_assignment_reason. '
                    'Use assignment_group for normal team routing.'
                )
            })
        if _handoff.is_team_route and (direct_assignee_email or direct_assignee_id):
            # drop the stale hint; the item is created group-only and team-resolved.
            serializer.validated_data['assignee_id'] = None
            serializer.validated_data['assignee_email'] = None
            extra_data.pop('assignee_id', None)
            extra_data.pop('assignee_email', None)
            direct_assignee_email = None
            direct_assignee_id = None

        # PLS-539 × PLAT-9100: carry the canonical handoff intent into the
        # serializer so ItemSerializer.create can drop the backstop's
        # AUTO-RESOLVED group on a direct handoff (the provenance-flagged
        # "leftover stale group"); co-supplied groups are retained. The flag
        # is computed AFTER the team-route drop above, so a dropped stale
        # hint (team route) never suppresses the backstop's team stamping.
        serializer._pls539_direct_create = bool(getattr(_handoff, 'is_direct', False))

        # PLS-16: the operator (CEO) is LAST RESORT — block creating a task
        # assigned directly to him unless the actor is the Chief of Staff (the
        # escalation gateway) or the operator himself. Mirrors the assign-endpoint
        # guard so the chain can't be bypassed via create-with-assignee. Check both
        # assignee_email and assignee_id so callers cannot omit the email to bypass
        # the restriction.
        _resolved_create_email = enforce_operator_assignment_gate(
            user.email,
            direct_assignee_id,
            direct_assignee_email,
            context='create assignment',
        )
        # PLS-16: when an ALLOWED actor (CoS / operator self-assign) creates by
        # assignee_id ALONE, the gate resolved the email but the serializer payload has
        # none — so without this the created task lands with assignee_id and a MISSING
        # assignee_email (notifications + membership seeding key on the email). Persist
        # the gate-resolved email via extra_data (applied at serializer.save below).
        if direct_assignee_id and _resolved_create_email and not direct_assignee_email:
            extra_data['assignee_email'] = _resolved_create_email

        # PLAT-236: create-with-explicit-assignee is NEW work — same at-cap
        # block as the assign endpoint (P2 on PR #26: this path previously
        # bypassed the cap entirely). CEO exempt; covers assignee_email AND
        # assignee_id. No in-flight exemption — a create is never in-flight.
        if serializer.validated_data.get('mode', 'task') == 'task' and \
                (not CEO_EMAIL or (user.email or '').lower() != CEO_EMAIL):
            _cap_tgt_email = (direct_assignee_email or '').strip().lower()
            _cap_tgt_id = serializer.validated_data.get('assignee_id')
            if _cap_tgt_email or _cap_tgt_id:
                try:
                    from .services.workflow_service import WorkflowService
                    from .user_service import user_service as _us
                    tuid = _cap_tgt_id if (_cap_tgt_id and str(_cap_tgt_id).isdigit()) else None
                    try:
                        if _cap_tgt_email and not tuid:
                            _info = _us.get_user_by_email(_cap_tgt_email)
                            tuid = _info.get('id') if _info else None
                        elif tuid and not _cap_tgt_email:
                            _info = _us.get_user_by_id(int(tuid))
                            _cap_tgt_email = (_info.get('email') or '').strip().lower() if _info else ''
                    except Exception:
                        pass
                    in_cap_team = _cap_tgt_email and any(
                        _cap_tgt_email in {e.strip().lower() for e in WorkflowService._flat_roster(WorkflowService._pod_roster().get(t, []))}
                        for t in WorkflowService._cap_enforced_teams()
                    )
                    _create_priority = serializer.validated_data.get('priority')
                    if (not WorkflowService._priority_ignores_wip_cap(_create_priority)
                            and in_cap_team and tuid and str(tuid).isdigit() and
                            WorkflowService._agent_at_capacity(int(tuid), user_email=_cap_tgt_email or None)):
                        raise drf_serializers.ValidationError({
                            'assignee_email': (
                                f"{_cap_tgt_email or _cap_tgt_id} is at the WIP cap "
                                f"(active cap {WorkflowService._wip_cap()}, in-flight cap {WorkflowService._inflight_cap()}). Creating new work "
                                f"directly on an at-cap engineer is blocked (PLAT-236). Use "
                                f"assignment_group — the task queues group-only and is pulled when "
                                f"capacity frees — or pick an under-cap member."
                            )
                        })
                    # P2 (PR #26 round 2): persist the RESOLVED numeric id on
                    # email-only creates. Saving assignee_email with a NULL
                    # assignee_id makes the task invisible to BOTH the per-agent
                    # heartbeat (filters by assignee_id) AND _agent_workable_load
                    # (counts by assignee_id) — so repeated email-creates would
                    # bypass the cap through the load count itself.
                    if _cap_tgt_email and not serializer.validated_data.get('assignee_id') \
                            and tuid and str(tuid).isdigit():
                        extra_data['assignee_id'] = int(tuid)
                except drf_serializers.ValidationError:
                    raise
                except Exception:
                    logger.exception("PLAT-236 create-assignee cap check failed (non-fatal, allowing create)")

        # For tasks, prefer team routing when a team is provided. The workflow
        # engine's team resolver pins assignment_group and resolves the current
        # available concrete owner; direct lead/creator fallback is only for
        # truly non-team creates.
        if serializer.validated_data.get('mode', 'task') == 'task':
            if not serializer.validated_data.get('assignee_id') and not serializer.validated_data.get('assignee_email'):
                assignment_group = serializer.validated_data.get('assignment_group')
                explicit_assignment_group = bool(assignment_group)
                if not assignment_group:
                    from .services.workflow_service import WorkflowService as _OwnerTeam
                    tags = (serializer.validated_data.get('tags') or '').lower()
                    if any(marker in tags for marker in ('escalation', 'decision-required', 'blocker')):
                        assignment_group = 'admin-ops'
                    else:
                        # PLS-933: resolve the TRUE owner from project/workflow/
                        # tags. Engineering is the fallback only when THIS org
                        # has its own engineering roster. A customer org with
                        # no local team must not borrow the platform roster
                        # (that capacity-queues the task onto Shizuha engineering
                        # and the card never gets an owner). Leave the group
                        # unset so the creator self-assign below owns it.
                        _owner_probe = _OwnerTeam.create_permission_probe_item(
                            serializer.validated_data,
                            extra_data=extra_data,
                        )
                        assignment_group = _OwnerTeam._derive_owner_team(_owner_probe)
                        if (
                            not assignment_group
                            and _OwnerTeam._org_local_team_roster(
                                'engineering', _owner_probe,
                            ) is not None
                        ):
                            assignment_group = 'engineering'
                    if assignment_group:
                        extra_data['assignment_group'] = assignment_group
                cap_queued = False
                if assignment_group:
                    try:
                        from .services.workflow_service import WorkflowService
                        assignment_group = WorkflowService._canonical_team_name(
                            assignment_group,
                        )
                        extra_data['assignment_group'] = assignment_group
                        probe_item = WorkflowService.create_permission_probe_item(
                            serializer.validated_data,
                            extra_data=extra_data,
                            assignment_group=assignment_group,
                        )
                        if not WorkflowService._team_registered_for_item(
                            assignment_group,
                            probe_item,
                        ):
                            raise drf_serializers.ValidationError({
                                'assignment_group': (
                                    'Team is not registered for the selected project organization.'
                                ),
                            })
                        # PLAT-236: creating a task is NEW work — enforce the WIP
                        # cap. All members at cap → (None, None) → the task is
                        # created group-only (the capacity queue) and pulled when
                        # a slot frees, instead of pushed past the cap.
                        uid, email = WorkflowService._resolve_team_assignee(
                            assignment_group,
                            exclude_email=user.email,
                            enforce_cap=True,
                            item=probe_item,
                        )
                        if getattr(probe_item, '_wip_reservation_token', None):
                            serializer.context['_wip_reservation_token'] = (
                                probe_item._wip_reservation_token
                            )
                        if not email and (
                            WorkflowService._wip_roster_config(assignment_group, probe_item) is not None
                            or WorkflowService._cap_applies(assignment_group)
                        ):
                            # Distinguish "queued at capacity" from "nothing
                            # resolvable": if the uncapped resolver finds someone,
                            # the team exists and is merely full.
                            u2, e2 = WorkflowService._resolve_team_assignee(
                                assignment_group, exclude_email=user.email,
                                enforce_cap=False, item=probe_item)
                            cap_queued = bool(e2)
                    except drf_serializers.ValidationError:
                        raise
                    except Exception as e:
                        logger.warning(
                            f"Could not resolve assignment_group={assignment_group!r} "
                            f"on create ({type(e).__name__}: {e})"
                        )
                        uid, email = None, None
                    if email:
                        extra_data['assignee_email'] = email
                        if uid:
                            extra_data['assignee_id'] = uid
                project = serializer.validated_data.get('project')
                # cap_queued skips the default-assignee fallbacks below: the task
                # must stay group-only so it queues, not bypass the cap via the
                # project lead (PLAT-236).
                if (not cap_queued and not explicit_assignment_group
                        and not extra_data.get('assignee_email') and project and project.default_assignee_id):
                    # Codex P2: gate the project default by its EMAIL when one is
                    # configured, so a transient shizuha-id id-resolution outage doesn't
                    # fail-closed (400) an ordinary create whose project default is a valid
                    # NON-operator address. Only when there is no default email do we
                    # resolve+gate the id (fail-closed is acceptable there — the target
                    # can't be determined otherwise). The operator target is still caught
                    # either way (the gate blocks the operator email).
                    _gate_id = None if project.default_assignee_email else project.default_assignee_id
                    enforce_operator_assignment_gate(
                        user.email,
                        _gate_id,
                        project.default_assignee_email,
                        context='project-default create assignment',
                    )
                    # PLS-16 (#2): default_assignee_id and default_assignee_email drift
                    # independently, so the email-gate above misses an operator
                    # default_assignee_id paired with a non-operator default email — yet
                    # queues/notifications key on assignee_id. Gate the id explicitly too
                    # (direct operator-id check — no resolution-outage fail-closed), unless
                    # the actor is the CoS / operator self.
                    from .services.workflow_service import WorkflowService as _WS
                    if (_WS._is_operator_target(project.default_assignee_id, None)
                            and (not (user.email or '').strip() or (user.email or '').strip().lower() not in (_WS._COS_EMAIL, _WS._OPERATOR_EMAIL))):
                        from rest_framework.exceptions import PermissionDenied as _PD
                        raise _PD({
                            'assignee_id': (
                                f"Refusing operator project-default assignment (default_assignee_id={project.default_assignee_id}) "
                                f"— the operator is LAST RESORT; only the Chief of Staff or operator self-assignment "
                                f"may place work on {_WS._OPERATOR_EMAIL}."
                            )
                        })
                    extra_data['assignee_id'] = project.default_assignee_id
                    if project.default_assignee_email:
                        extra_data['assignee_email'] = project.default_assignee_email
                    else:
                        # Resolve email from shizuha-id for the project lead.
                        # AT-97: belt-and-suspenders try/except so a programming-
                        # error / non-RequestException from user_service can't 500
                        # the create. Service already catches RequestException and
                        # returns None; this catches everything else.
                        from .user_service import user_service
                        try:
                            lead_info = user_service.get_user_by_id(project.default_assignee_id)
                        except Exception as e:
                            logger.warning(
                                f"user_service.get_user_by_id({project.default_assignee_id}) "
                                f"raised unexpectedly ({type(e).__name__}: {e}); "
                                f"falling back to creator email"
                            )
                            lead_info = None
                        if lead_info and lead_info.get('email'):
                            extra_data['assignee_email'] = lead_info['email']
                        else:
                            extra_data['assignee_email'] = user.email
                elif (not cap_queued and not explicit_assignment_group
                      and not extra_data.get('assignee_email') and not assignment_group):
                    # PLAT-906: only self-assign the creator when the task has NO team at
                    # all. A team is essentially always present here (assignment_group is
                    # explicit or defaulted to engineering/admin-ops above), and a logical
                    # team can NEVER fail to accept work even with zero available members.
                    # So when a team is set but no live member resolved, leave the task
                    # GROUP-ONLY ("<Team> (Unassigned)") and let the rebalancer/pull assign
                    # a member when one is available — NEVER fall back to the human creator/
                    # operator, which produced the invalid "<Team> (<operator>)" state
                    # (the operator/creator isn't a team member).
                    extra_data['assignee_id'] = user.id
                    extra_data['assignee_email'] = user.email

        # PLAT-192: normalize every create-time assignee write through the same
        # id/email derivation rule as Item.assign().  Trust a provided id over a
        # stale email, and fill whichever side is missing before the row is born.
        _create_uid = extra_data.get('assignee_id', serializer.validated_data.get('assignee_id'))
        _create_email = extra_data.get('assignee_email', serializer.validated_data.get('assignee_email'))
        if _create_uid or _create_email:
            uid, email = Item._resolve_user_ref({'id': _create_uid, 'email': _create_email})
            if uid is not None or email:
                extra_data['assignee_id'] = uid
                extra_data['assignee_email'] = email

        # PLS-465: the MCP now sends an explicit direct owner in this initial
        # create transaction instead of creating a team-resolved owner and
        # overriding it through /assign/ afterwards.  Preserve the cross-org
        # guard that the old follow-up endpoint supplied before the row exists.
        if direct_assignee_email and extra_data.get('assignee_email'):
            from .services.workflow_service import WorkflowService as _WSorg
            probe_item = _WSorg.create_permission_probe_item(
                serializer.validated_data,
                extra_data=extra_data,
                assignment_group=serializer.validated_data.get('assignment_group'),
            )
            _mismatch, _roster_org, _task_org = _WSorg.assignment_org_mismatch(
                probe_item,
                extra_data['assignee_email'],
                serializer.validated_data.get('assignment_group'),
                user_id=extra_data.get('assignee_id'),
            )
            if _mismatch:
                raise drf_serializers.ValidationError({'assignee_email': (
                    f'Cross-org assignment blocked (PLAT-4213): '
                    + _WSorg.assignment_denial_reason(
                        extra_data["assignee_email"], _roster_org, _task_org)
                )})

        # Handle project assignment and generate item_key
        project = serializer.validated_data.get('project')
        if project:
            if explicit_org_id is not None and int(project.org_id) != int(explicit_org_id):
                raise PermissionDenied(
                    f'Project belongs to organization {project.org_id}, not requested organization {explicit_org_id}.'
                )
            # PLS-7: a SOFT-archived (deleted) project must not accept new items —
            # they would be immediately hidden by the archived-project filter, so a
            # client/integration with a cached project id silently piles invisible
            # tasks into a deleted project. Reject before key allocation.
            if project.is_archived:
                raise drf_serializers.ValidationError({
                    'project': 'This project is archived and no longer accepts new items.'
                })
            # Verify user can create items in this project
            can_create = PermissionService.can_access_project(
                user.id, project, permission='create', user_email=user.email, user=user
            )
            if not can_create and not user.is_staff:
                raise drf_serializers.ValidationError({
                    'project': 'You do not have permission to create items in this project'
                })

            # Generate item_key for project items
            extra_data['item_key'] = project.get_next_item_key()

        item = serializer.save(**extra_data)
        if item.assignment_group:
            try:
                from .services.workflow_service import WorkflowService
                WorkflowService._maybe_file_zero_member_team_alert(
                    item, item.assignment_group, trigger='create',
                )
            except Exception:
                logger.warning("PLS-197 zero-member-team create hook failed", exc_info=True)
        if serializer.validated_data.get('mode', 'task') == 'task' and not item.assignee_id and item.assignment_group:
            try:
                from .services.workflow_service import WorkflowService
                if (WorkflowService._wip_roster_config(item.assignment_group, item) is not None
                        or WorkflowService._cap_applies(item.assignment_group)):
                    Comment.objects.create(
                        item=item, author_id=0, author_email='system@pulse',
                        content=(f"Queued in the **{item.assignment_group}** capacity queue — all team "
                                 f"members are at the configured WIP cap. The task is "
                                 f"pulled automatically (highest priority first) when a member frees a slot "
                                 f"or the rebalance sweep finds headroom. [PLAT-236]"))
            except Exception:
                logger.exception("PLAT-236 queue breadcrumb failed (non-fatal)")
        # PLAT-4871/PLS-539: one shared direct-handoff contract. perform_create is
        # already @transaction.atomic, so the audit comment rolls back with the item.
        record_direct_handoff(
            item,
            assignee_email=_handoff.assignee_email,
            reason=_handoff.reason,
            actor_id=user.id,
            actor_email=user.email,
            is_direct=_handoff.is_direct,
            context='creation',
            prev_assignee='',
        )

        # Mode-specific processing
        if item.mode == 'task':
            self._process_task_create(item)
        elif item.mode == 'action':
            self._process_action_create(item)
        elif item.mode == 'alert':
            self._process_alert_create(item)


    def perform_update(self, serializer):
        """Update item with notification triggers."""
        item = self.get_object()
        old_status = item.status
        old_assignee = item.assignee_email
        old_priority = item.priority

        # An item's project is its persisted tenancy boundary. Ordinary PATCH
        # may move an item only between projects in the same organization and
        # only when the actor can create in the destination. Detaching a
        # project-backed item into personal scope, attaching a personal item to
        # an org, or crossing organizations requires an explicit transfer
        # protocol (not implemented) and therefore fails closed here.
        if 'project' in serializer.validated_data:
            target_project = serializer.validated_data.get('project')
            target_project_id = getattr(target_project, 'id', None)
            if target_project_id != item.project_id:
                same_org_move = bool(
                    item.project_id
                    and target_project is not None
                    and int(item.project.org_id) == int(target_project.org_id)
                )
                can_create_in_target = bool(
                    same_org_move
                    and PermissionService.can_access_project(
                        self.request.user.id,
                        target_project,
                        permission='create',
                        user_email=getattr(self.request.user, 'email', None),
                        user=self.request.user,
                    )
                )
                if not can_create_in_target:
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError({
                        'project': (
                            'Item project changes may not cross an organization or '
                            'personal-scope boundary, and require create access to '
                            'the destination project.'
                        ),
                    })

        # Guard: if item has a workflow and status is being changed directly,
        # reject — must use execute-transition endpoint instead.
        new_status = serializer.validated_data.get('status')
        if new_status and new_status != old_status and item.workflow_name:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({
                'status': f'This task uses the "{item.workflow_name}" workflow. '
                          f'Status changes must go through workflow transitions. '
                          f'Use POST /api/items/{item.id}/execute-transition/ instead of PATCH status directly.'
            })

        # PLS-16 operator-assignment guard on PATCH — mirror the custom
        # /assign/ endpoint and create path. Check both assignee_email and
        # assignee_id so callers cannot omit the email to bypass the CoS gate.
        # There is intentionally no awaiting_merge exception: the operator is out
        # of the routine merge loop and only takes Pulse assignments from the
        # Chief of Staff or via operator self-assignment.
        new_assignee = (serializer.validated_data.get('assignee_email') or '').strip().lower()
        new_assignee_id = serializer.validated_data.get('assignee_id')
        actor_email = (getattr(self.request.user, 'email', '') or '').lower()
        new_assignee = enforce_operator_assignment_gate(
            actor_email,
            new_assignee_id,
            new_assignee,
            context='PATCH assignment',
        )
        # PLS-16: an id-only assignment PATCH resolves the email via the gate, but a
        # PARTIAL update writes only submitted fields — so without this, serializer.save()
        # would change assignee_id while leaving assignee_email on the OLD owner (stale
        # state for notifications + membership seeding). Persist the resolved email when
        # an id was supplied without an explicit email.
        _resolved_assignee_email = (
            new_assignee
            if (new_assignee_id and new_assignee
                and not serializer.validated_data.get('assignee_email'))
            else None
        )

        # PLAT-236: PATCH is the third push path — mirror the /assign/ WIP-cap
        # guard (P2 on PR #26 round 2: assignee fields via PATCH bypassed the
        # cap). Same exemptions: CEO actor, current-owner re-assert, in-flight
        # working statuses (orphan-safety outranks the cap).
        _patch_tgt_email = new_assignee
        _patch_tgt_id = serializer.validated_data.get('assignee_id')
        actor_email = (getattr(self.request.user, 'email', '') or '').lower()
        if (_patch_tgt_email or _patch_tgt_id) and (not CEO_EMAIL or actor_email != CEO_EMAIL):
            try:
                from .services.workflow_service import WorkflowService
                from .user_service import user_service as _us
                tuid = _patch_tgt_id if (_patch_tgt_id and str(_patch_tgt_id).isdigit()) else None
                tgt = _patch_tgt_email
                try:
                    if tgt and not tuid:
                        _info = _us.get_user_by_email(tgt)
                        tuid = _info.get('id') if _info else None
                    elif tuid and not tgt:
                        _info = _us.get_user_by_id(int(tuid))
                        tgt = (_info.get('email') or '').strip().lower() if _info else ''
                except Exception:
                    pass
                in_cap_team = tgt and any(
                    tgt in {e.strip().lower() for e in WorkflowService._flat_roster(WorkflowService._pod_roster().get(t, []))}
                    for t in WorkflowService._cap_enforced_teams()
                )
                already_owner = bool(tgt) and (item.assignee_email or '').strip().lower() == tgt
                in_flight = ((new_status or item.status) or '').strip().lower() in WorkflowService._WORKING_STATUSES
                _patch_priority = serializer.validated_data.get('priority', item.priority)
                if (not WorkflowService._priority_ignores_wip_cap(_patch_priority)
                        and in_cap_team and not already_owner and not in_flight and
                        tuid and str(tuid).isdigit() and
                        WorkflowService._agent_at_capacity(int(tuid), user_email=tgt or None)):
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError({
                        'assignee_email': (
                            f"{tgt or _patch_tgt_id} is at the WIP cap "
                            f"(active cap {WorkflowService._wip_cap()}, in-flight cap {WorkflowService._inflight_cap()}). Direct pushes past the "
                            f"cap are blocked on every path incl. PATCH (PLAT-236). Use "
                            f"assignment_group team routing or pick an under-cap member."
                        )
                    })
            except Exception as _e:
                from rest_framework.exceptions import ValidationError as _VE
                if isinstance(_e, _VE):
                    raise
                logger.exception("PLAT-236 PATCH cap check failed (non-fatal, allowing update)")

        # PLS-539 (revi #47 P1): track the direct-override decision so the audit
        # comment + decision pin can be written after save (mirroring create +
        # /assign), and capture the pre-save owner to detect a NON-direct
        # reassignment that should release an obsolete pin.
        _pls539_old_assignee = (item.assignee_email or '').strip().lower()
        _pls539_patch_direct = False
        _pls539_patch_reason = ''
        _pls539_patch_assignee_email = ''

        save_overrides = {}
        if _resolved_assignee_email:
            save_overrides['assignee_email'] = _resolved_assignee_email
        if 'assignee_id' in serializer.validated_data or 'assignee_email' in serializer.validated_data:
            if serializer.validated_data.get('assignee_id') in (None, '') and \
                    serializer.validated_data.get('assignee_email') in (None, ''):
                save_overrides.update({'assignee_id': None, 'assignee_email': None})
            else:
                uid, email = Item._resolve_user_ref({
                    'id': serializer.validated_data.get('assignee_id'),
                    'email': save_overrides.get('assignee_email') or serializer.validated_data.get('assignee_email'),
                })
                if uid is not None or email:
                    save_overrides.update({'assignee_id': uid, 'assignee_email': email})

        if serializer.validated_data.get('assignment_group'):
            from .services.workflow_service import WorkflowService as _PatchTeamService
            canonical_team = _PatchTeamService._canonical_team_name(
                serializer.validated_data['assignment_group'],
            )
            serializer.validated_data['assignment_group'] = canonical_team
            if not _PatchTeamService._team_registered_for_item(
                canonical_team,
                item,
            ):
                from rest_framework.exceptions import ValidationError
                raise ValidationError({
                    'assignment_group': (
                        'Team is not registered for this task organization.'
                    ),
                })

        # PLAT-4213: PATCH is an assignment path too. Refuse a concrete
        # cross-org owner before serializer.save() can persist it.
        _patch_email = (save_overrides.get('assignee_email') or '').strip().lower()
        if _patch_email:
            from .services.workflow_service import WorkflowService as _WSorg
            # PLS-539 (P1, revi #313/#316): derive ONE direct-vs-team intent BEFORE
            # the preflight AND the save. A PATCH that sets a specific assignee
            # without changing the team is a DIRECT override.
            _patch_reason = (self.request.data.get('direct_assignment_reason') or '').strip()
            _patch_actor = (getattr(self.request.user, 'email', '') or '').strip().lower()
            _patch_team = serializer.validated_data.get('assignment_group')
            # PLS-539 (revi #47 P1.1): consume the ONE precedence UNCHANGED, as at
            # create + /assign. A group with NO reason is a TEAM route — drop the stale
            # assignee hint here so the team-change re-resolve below picks the live
            # member (preserves test_plat1089_* tolerance); otherwise a concrete
            # assignee is a DIRECT override, gated for a reason.
            _handoff = normalize_direct_handoff(
                assignee_email=_patch_email, assignment_group=_patch_team,
                reason=_patch_reason, resolve_email=False)
            _patch_direct = _handoff.is_direct
            if _handoff.is_team_route:
                # PLAT-7515: a team-route classification may only SILENTLY drop a
                # submitted assignee when the drop is load-bearing — i.e. the group
                # is CHANGING and the team-change re-resolve below picks the live
                # member (the long-standing test_plat1089_* tolerance). When the
                # submitted group is UNCHANGED, the drop is a guaranteed silent
                # no-op: the caller explicitly named an assignee, nothing re-resolves,
                # and the 200 response shows the old owner — the exact REST/MCP
                # divergence this task records. Reject with a typed 400 instead so
                # the caller learns the assignee was NOT applied (the PLAT-4213 /
                # PLS-539 guards themselves are untouched).
                _submitted_group = (self.request.data.get('assignment_group') or '').strip()
                _group_unchanged = _submitted_group.lower().replace('_', '-') == (
                    item.assignment_group or '').strip().lower().replace('_', '-')
                if _patch_email and _group_unchanged:
                    from rest_framework.exceptions import ValidationError as _VEsilent
                    raise _VEsilent({'assignee_email': (
                        'assignment_group was supplied without direct_assignment_reason, '
                        'so this PATCH was classified as a team route and the submitted '
                        'assignee was NOT applied (the group is unchanged, so no team '
                        're-resolve would run — the request could only be a silent no-op). '
                        'Re-send with direct_assignment_reason to make it a direct person '
                        'assignment, or drop assignment_group to change the assignee directly.'
                    )})
                for _drop in ('assignee_email', 'assignee_id'):
                    save_overrides.pop(_drop, None)
                    serializer.validated_data.pop(_drop, None)
            # revi #316 P1: a direct override REQUIRES an explicit audited reason.
            # Without it, reject with ZERO mutation; never silently take the widened
            # direct path. Exempt only the operator self-assign path (actor is operator).
            if not _handoff.is_team_route and direct_handoff_reason_denied(
                    _patch_actor, _handoff.assignee_email, _handoff.reason,
                    is_direct=_patch_direct):
                from rest_framework.exceptions import ValidationError as _VEreason
                raise _VEreason({'assignee_email': (
                    'Direct person assignment requires direct_assignment_reason. '
                    'Use assignment_group for normal team routing so Pulse can '
                    'choose an available team member.')})
            if _patch_direct:
                serializer.instance._pls539_direct_override = True
                # PLS-539 (hiro/ADR-0010): carry the authorized reason so Item.save's
                # direct-handoff mint records it on the crossing receipt (audit only;
                # the mint is gated on the override flag, not reason-presence).
                serializer.instance._pls539_direct_handoff_reason = _handoff.reason
                _pls539_patch_direct = True
                _pls539_patch_reason = _handoff.reason
                _pls539_patch_assignee_email = _handoff.assignee_email
                # revi #316/#47 P1 (direct-label honesty): a direct override with NO
                # requested team must not leave the prior team's stale assignment_group
                # persisted (mirrors /assign) — else the item reads as team-routed to a
                # team the direct owner does not belong to. A group co-supplied in THIS
                # request is retained (matches create): the caller explicitly asked for
                # it. So clear only the leftover stale group, never a co-supplied one.
                if not _patch_team:
                    save_overrides['assignment_group'] = None
            # PLS-539 (revi #47 P1.2): validate the assignee against the group that
            # will actually be PERSISTED — a co-supplied group is retained AND its
            # exact (org, group) membership is checked (identical to create); a
            # leftover group (none supplied, cleared above) validates the direct
            # assignee by task-org binding only (team=None). Never accept an in-org
            # non-member of a retained team. Skipped on a team route (the hint was
            # dropped; the team-change re-resolve validates the resolved member).
            if not _handoff.is_team_route:
                _mismatch, _roster_org, _task_org = _WSorg.assignment_org_mismatch(
                    item, _patch_email, _patch_team,
                    user_id=save_overrides.get('assignee_id'),
                )
                if _mismatch:
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError({'assignee_email': (
                        f'Cross-org assignment blocked (PLAT-4213): '
                        + _WSorg.assignment_denial_reason(_patch_email, _roster_org, _task_org)
                    )})

        # #9532/PLS-205: changing a task's TEAM must re-resolve the assignee.
        # PATCH was the one push path with no assignment_group handling: a
        # pulse_update_task(assignment_group='architecture') wrote the new team
        # but silently kept the OLD assignee — a non-member (the operator, on
        # #9532) stayed on the task, stranding it on their personal queue while
        # the UI rendered them as if in the new team. Mirror the /assign/
        # rule: team routing is authoritative. If the same PATCH does not name
        # an assignee explicitly, and the group changes: keep the current
        # assignee only if they belong to the new team's active tier
        # (continuity, no churn); otherwise re-resolve the least-loaded
        # under-cap member, or clear to group-queue (capacity queue) when the
        # team is full — never leave a non-member assignee behind.
        try:
            _new_group = (serializer.validated_data.get('assignment_group') or '').strip()
            _explicit_assignee = (
                'assignee_id' in serializer.validated_data
                or 'assignee_email' in serializer.validated_data
                or bool(_resolved_assignee_email)
            )
            if (_new_group
                    and _new_group.lower() != (item.assignment_group or '').strip().lower()
                    and not _explicit_assignee):
                from .services.workflow_service import WorkflowService as _WSg
                # PLS-208: membership must be checked identity-wise, not by raw
                # email string. Rosters carry @shizuha.com addresses while
                # assignees are often stored under the @agents.shizuha.io alias
                # of the SAME agent; a raw compare misreads the member as a
                # non-member and churns a needless re-resolve.
                # _email_in_current_team_active_tier is the one shared,
                # alias-folding membership helper — use it here and in any
                # future membership validation.
                _cur = (item.assignee_email or '').strip().lower()
                if not _cur or not _WSg._email_in_current_team_active_tier(_new_group, _cur, item=item):
                    _uid, _email = _WSg._resolve_team_assignee(
                        _new_group, enforce_cap=True, item=item,
                        project_key=_WSg._item_project_key(item),
                    )
                    if _email:
                        # canonicalize the pair the same way the explicit-assignee
                        # branch does — never persist an id/email that the identity
                        # source can't confirm belong together (model save rejects
                        # mismatched pairs).
                        _ruid, _remail = Item._resolve_user_ref({'id': _uid, 'email': _email})
                        if _ruid is not None or _remail:
                            save_overrides.update({'assignee_id': _ruid, 'assignee_email': _remail})
                        else:
                            save_overrides.update({'assignee_id': None, 'assignee_email': _email})
                    else:
                        # team full/unresolvable → group-queue; the capacity
                        # drain / rebalance sweep pulls it when a slot frees.
                        save_overrides.update({'assignee_id': None, 'assignee_email': None})
                    logger.info(
                        "team-change re-resolve on item %s: group %r -> %r, assignee %r -> %r",
                        item.item_key or item.pk, item.assignment_group, _new_group,
                        item.assignee_email, save_overrides.get('assignee_email'),
                    )
        except Exception:
            logger.exception("team-change assignee re-resolve failed (non-fatal) on item %s",
                             item.item_key or item.pk)

        # PLAT-4871/PLS-539: route the assignee write + audit through the ONE shared
        # direct-handoff contract, ATOMICALLY — the audit comment now rolls back with
        # the assignee (the previous PATCH path saved the assignee, then wrote the
        # comment best-effort in a separate, non-atomic step). Direct override ->
        # reason comment + decision pin; non-direct reassignment -> release an
        # obsolete pin. The pin write stays best-effort (PLS-233).
        _pls539_actor_id = getattr(self.request.user, 'id', 0) or 0
        _pls539_actor_email = getattr(self.request.user, 'email', '') or 'system@pulse'

        # PLS-518: pinned current-state handoff summary — single-writer (current
        # assignee or staff) and timestamped. A stale/former assignee must not
        # overwrite a newer pin, and the pin is a handoff aid, NOT authoritative
        # task state (the workflow/status is). We gate on the CURRENT persisted
        # assignee so a PATCH that reassigns in the same call still authorizes
        # against the pre-change owner (the new owner re-pins after handoff).
        if 'current_state' in serializer.validated_data:
            _cs_actor = (getattr(self.request.user, 'email', '') or '').strip().lower()
            _cs_owner = (item.assignee_email or '').strip().lower()
            _cs_staff = bool(getattr(self.request.user, 'is_staff', False))
            if not (_cs_staff or (_cs_actor and _cs_actor == _cs_owner)):
                from rest_framework.exceptions import PermissionDenied as _CSDenied
                raise _CSDenied(
                    'Only the current assignee (or staff) may update the pinned '
                    'current-state summary. Hand off the task first, then let the '
                    'new owner pin their state.'
                )
            # Timestamp the pin on every write (clearing it also stamps the clear).
            serializer.validated_data['current_state_updated_at'] = timezone.now()

        with transaction.atomic():
            updated_item = serializer.save(**save_overrides)
            record_direct_handoff(
                updated_item,
                assignee_email=_pls539_patch_assignee_email,
                reason=_pls539_patch_reason,
                actor_id=_pls539_actor_id,
                actor_email=_pls539_actor_email,
                is_direct=bool(_pls539_patch_direct),
                context='update',
                prev_assignee=_pls539_old_assignee,
            )

        # PLS-184: adding a high-risk label/tag should immediately create or
        # update a focused QA task. Keep it best-effort so normal PATCH still
        # succeeds and the next trigger (done/PR-merge/deployment event) can retry.
        if 'labels' in serializer.validated_data or 'tags' in serializer.validated_data:
            try:
                from .services.focused_qa import ensure_focused_qa_for_item
                ensure_focused_qa_for_item(updated_item, trigger='label_update')
            except Exception:
                logger.warning("PLS-184 focused QA generation failed after item update", exc_info=True)

        # PLS-50: do NOT call notification_service directly here.
        # serializer.save() above triggers post_save → track_item_changes →
        # _dispatch_field_notification → send_item_notification.delay(), which
        # delivers the Connect DM via the Celery worker. Calling notify_* here
        # as well produced a duplicate DM on every PATCH to status/assignee/priority.
        # The execute_transition view was already fixed the same way (see comment
        # at TransitionView ~line 7024): "we rely solely on the signal now".

    @action(detail=True, methods=['post'], url_path='containment-decision')
    def record_containment_decision(self, request, pk=None):
        """Record an evidence-backed PLS-620 containment disposition."""

        item = self.get_object()
        try:
            decision = item.containment_decision
        except ContainmentDecision.DoesNotExist:
            return Response(
                {'state': 'No urgent-risk containment requirement exists for this item.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor_email = str(getattr(request.user, 'email', '') or '').strip().lower()
        from .services.workflow_service import WorkflowService

        actor_aliases = WorkflowService._agent_email_aliases(actor_email)
        allowed_emails = {
            str(decision.owner_email or '').strip().lower(),
            str(item.assignee_email or '').strip().lower(),
            str(getattr(item.project, 'lead_email', '') or '').strip().lower(),
        }
        allowed = bool(getattr(request.user, 'is_staff', False)) or any(
            actor_aliases & WorkflowService._agent_email_aliases(email)
            for email in allowed_emails
            if email
        )
        if not allowed:
            raise PermissionDenied(
                'Only the named containment owner, current task owner, project lead, '
                'or staff may record this decision.'
            )

        from .services.urgent_risk_containment import record_decision, serialize_decision

        try:
            decision = record_decision(
                item.pk,
                state=request.data.get('state'),
                reason=request.data.get('reason', ''),
                evidence=request.data.get('evidence'),
                actor_id=getattr(request.user, 'id', None),
                actor_email=actor_email,
            )
        except DjangoValidationError as exc:
            body = getattr(exc, 'message_dict', None) or {'detail': exc.messages}
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        item.containment_decision = decision
        return Response(
            {'containment_decision': serialize_decision(item)},
            status=status.HTTP_200_OK,
        )

    def _process_task_create(self, task):
        """Process task creation with workflow engine support."""

        # Default workflow: every task gets a workflow
        if not task.workflow_name:
            task.workflow_name = 'simple'
            task.workflow_status = 'pending'
            task.save(update_fields=['workflow_name', 'workflow_status'])

        # ── Workflow Processing ──
        # PLS-465: create owns one final assignment event.  Suppress intermediate
        # assignee activity/dispatch while auto-transitions finish; the final
        # committed owner is recorded and notified exactly once below.
        original_suppressed = set(getattr(task, '_activity_suppress_fields', ()))
        task._activity_suppress_fields = original_suppressed | {
            'assignee_id', 'assignee_email',
        }
        if task.workflow_name:
            try:
                self._process_workflow_task(task)
            except Exception as e:
                logger.error(f"Workflow processing failed for task {task.id}: {e}")
        task._activity_suppress_fields = original_suppressed

        if task.assignee_id or task.assignee_email:
            actor_id = getattr(self.request.user, 'id', 0) or 0
            actor_email = getattr(self.request.user, 'email', '') or ''
            ItemActivity.objects.create(
                item=task,
                action='assigned',
                field_name='Assignee',
                old_value='',
                new_value=task.assignee_email or str(task.assignee_id),
                user_id=actor_id,
                user_email=actor_email,
            )
            item_id = str(task.pk)
            context = {
                'changed_by': actor_email or str(actor_id),
                'exclude_user_id': actor_id,
                'dispatched_assignee_id': str(task.assignee_id or ''),
                'dispatched_status': str(task.status or ''),
            }

            def _dispatch_final_create_assignment():
                try:
                    from .tasks import send_item_notification
                    send_item_notification.delay(
                        item_id, 'assignment_changed', context,
                    )
                except Exception:
                    logger.exception(
                        'PLS-465 final create-assignment dispatch failed for item %s',
                        item_id,
                    )

            transaction.on_commit(_dispatch_final_create_assignment)

    def _process_workflow_task(self, task):
        """Apply workflow engine logic: set initial status, fire auto-transitions, resolve assignment."""
        from .models import Workflow, WorkflowTransition, Status

        # Find the workflow by slug
        slug = task.workflow_name.lower()
        wf = None
        for s in [slug, slug.replace('-', '_'), slug.replace('_', '-')]:
            try:
                wf = Workflow.objects.get(slug__iexact=s)
                break
            except Workflow.DoesNotExist:
                continue

        if not wf:
            logger.warning(f"Workflow '{task.workflow_name}' not found on platform")
            return

        # Set initial workflow_status if not set
        if not task.workflow_status and wf.initial_status:
            task.workflow_status = wf.initial_status.slug
            task.save(update_fields=['workflow_status'])

        # Fire auto-transitions (loop until no more auto transitions or max 5 steps)
        for _ in range(5):
            if not task.workflow_status:
                break
            auto_transitions = WorkflowTransition.objects.filter(
                workflow=wf,
                from_status=task.workflow_status,
                name__icontains='auto',  # Convention: auto transitions have 'auto' in name
            )
            # Also check transitions with no conditions (simple auto-advance)
            if not auto_transitions.exists():
                # Check if there's exactly one transition from this status (auto-advance pattern)
                all_transitions = WorkflowTransition.objects.filter(
                    workflow=wf,
                    from_status=task.workflow_status,
                )
                if all_transitions.count() == 1:
                    t = all_transitions.first()
                    # Only auto-fire if the transition name suggests it's automatic
                    if 'auto' in (t.name or '').lower():
                        auto_transitions = all_transitions

            if not auto_transitions.exists():
                break

            transition = auto_transitions.first()
            old_status = task.workflow_status
            task.workflow_status = transition.to_status

            # Execute post-functions (assignment group, etc.)
            self._execute_workflow_post_functions(task, transition)

            task.save(update_fields=['workflow_status', 'assignment_group', 'assignee_id', 'assignee_email', 'status'])
            logger.info(f"Workflow auto-transition: {old_status} -> {task.workflow_status} for task {task.id}")

        # Sync status to workflow_status — the workflow is the source of truth.
        # The coarse category (todo/in_progress/done) is available via Status.category.
        if task.workflow_status and task.status != task.workflow_status:
            task.status = task.workflow_status
            task.save(update_fields=['status'])

    def _execute_workflow_post_functions(self, task, transition):
        """Execute post-functions from a workflow transition (assignment group, etc.)."""
        # Team-based assignment from post_functions
        post_fns = transition.post_functions or []
        for pf in post_fns:
            pf_type = pf.get('type', '')
            if pf_type == 'assign_team':
                team_name = pf.get('value') or pf.get('team') or ''
                if team_name:
                    from .services.workflow_service import WorkflowService
                    team_name = WorkflowService._canonical_team_name(team_name)
                    if not WorkflowService._team_registered_for_item(team_name, task):
                        from rest_framework.exceptions import ValidationError
                        raise ValidationError({
                            'assignment_group': (
                                'Workflow Team is not registered for this task organization.'
                            ),
                        })
                    task.assignment_group = team_name
                    # This create-time compatibility path used to query only the
                    # Shizuha Admin organization and pin that team's lead.  That
                    # made a same-named business team in another organization
                    # resolve to the wrong tenant.  Use the same item-aware,
                    # org-scoped resolver as normal workflow execution instead.
                    operator_capable = WorkflowService._is_operator_capable_team(
                        team_name,
                    )
                    uid, email = WorkflowService._resolve_team_assignee(
                        team_name,
                        exclude_email=task.created_by_email,
                        project_key=WorkflowService._item_project_key(task),
                        allow_operator=operator_capable,
                        item=task,
                    )
                    if uid or email:
                        WorkflowService._apply_resolved_assignee(
                            task,
                            uid,
                            email,
                            [],
                            operator_authorized=operator_capable,
                        )
                    else:
                        # Never retain the prior lane's concrete owner under a
                        # newly selected team.  The routing-authority helper is
                        # valid for both legacy and armed binding lanes and
                        # carries the exact destination proof consumed by save().
                        from .services.routing_authority import reroute_assignment_to_group_queue
                        reroute_assignment_to_group_queue(
                            task,
                            team_name,
                            reason='create_workflow_no_candidate',
                            save=False,
                        )
            elif pf_type == 'assign_to':
                value = pf.get('value', '')
                target = pf.get('target') or value
                if target == 'project_lead' and task.project:
                    task.assign({'id': task.project.lead_id, 'email': task.project.lead_email}, save=False)
                elif target == 'reporter':
                    task.assign({'id': task.created_by_id, 'email': task.created_by_email}, save=False)
                elif target == 'user':
                    task.assign({'id': pf.get('user_id'), 'email': pf.get('user_email') or value}, save=False)
                elif value:
                    task.assign({'email': value}, save=False)

    _admin_teams_cache = {}

    def _get_admin_teams(self, organization_id, organization_slug):
        """Fetch one explicit Admin organization's teams with an org-keyed cache.

        Both values are server-derived from an authorized Pulse project.  There
        is deliberately no default organization: losing the tenant scope must
        fail closed instead of silently querying Shizuha's team catalog.
        """
        import time
        now = time.time()
        try:
            organization_id = int(organization_id)
        except (TypeError, ValueError):
            return None
        organization_slug = str(organization_slug or '').strip().lower()
        if not organization_slug:
            return None

        cache_key = (organization_id, organization_slug)
        cached = type(self)._admin_teams_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < 300:
            return cached[1]

        try:
            import jwt as _jwt
            import requests
            jwt_secret = configured_service_signing_key()
            service_token = _jwt.encode({
                'user_id': 999, 'email': 'system@shizuha.com', 'username': 'system',
                'organization_id': organization_id, 'is_staff': True, 'is_superuser': True,
                'exp': timezone.now() + timedelta(hours=1),
                'iat': timezone.now(),
                'token_type': 'access',
                'jti': f'pulse-team-list-{organization_id}-{int(now)}',
            }, jwt_secret, algorithm='HS256')

            admin_url = os.environ.get('SHIZUHA_ADMIN_API_URL', 'http://shizuha-admin:8000/api')
            from .services.workflow_service import WorkflowService
            teams = WorkflowService._fetch_admin_team_pages(
                requests,
                admin_url.rstrip('/'),
                organization_slug,
                {'Authorization': f'Bearer {service_token}', 'Accept': 'application/json'},
            )
            type(self)._admin_teams_cache[cache_key] = (now, teams)
            return teams
        except Exception as e:
            logger.warning(
                'Error fetching Admin teams for org %s (%s): %s',
                organization_id,
                organization_slug,
                e,
            )

        return None

    def _process_action_create(self, action):
        """Process action creation."""
        # TODO: Check approval rules and auto-approve if applicable
        # TODO: Send notification for pending approval
        logger.info(f"Action created: {action.id} - requires_approval={action.requires_approval}")

    def _process_alert_create(self, alert):
        """Process alert creation."""
        # Check if silenced
        if SilencingService.is_silenced(alert):
            logger.info(f"Alert {alert.id} is silenced, skipping notification")
            return

        # Check if inhibited
        if InhibitionService.is_inhibited(alert):
            logger.info(f"Alert {alert.id} is inhibited, skipping notification")
            return

        # TODO: Send alert notification
        # TODO: Schedule escalation
        logger.info(f"Alert created: {alert.id} - severity={alert.severity}")

    # === Task-specific actions ===

    @action(detail=True, methods=['post'], url_path='prepare-external-wait')
    def prepare_external_wait(self, request, pk=None):
        """Mint a short-lived, one-use consumer capability (ADR-0064)."""
        from .services.external_wait import prepare_cross_org_wait

        item = self.get_object()
        try:
            result = prepare_cross_org_wait(
                item,
                user=request.user,
                release_policy=request.data.get('release_policy', ''),
                local_display_reference=request.data.get(
                    'local_display_reference', ''
                ),
            )
        except (DjangoValidationError, DjangoPermissionDenied) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='accept-external-wait')
    def accept_external_wait(self, request, pk=None):
        """Bind this authorized provider without disclosing consumer data."""
        from .services.external_wait import accept_cross_org_wait

        provider = self.get_object()
        try:
            result = accept_cross_org_wait(
                provider,
                user=request.user,
                handoff_token=request.data.get('handoff_token', ''),
            )
        except (DjangoValidationError, DjangoPermissionDenied) as exc:
            # ADR-0064 deliberately makes every token/binding refusal
            # tuple-free. Provider authorization has already happened through
            # get_object(); do not echo validation internals here.
            logger.info('External wait accept refused: %s', type(exc).__name__)
            return Response(
                {'error': 'external_wait_not_accepted'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='release-external-wait')
    def release_external_wait(self, request, pk=None):
        """Provider-authorized explicit-ready release (policy permitting)."""
        from .models import ExternalDependencyReleaseEvent
        from .services.external_wait import record_provider_release

        provider = self.get_object()
        try:
            count = record_provider_release(
                provider,
                kind=ExternalDependencyReleaseEvent.KIND_EXPLICIT,
                user=request.user,
            )
        except DjangoValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'accepted': True, 'release_count': count})

    @action(detail=True, methods=['post'], url_path='release-human-wait')
    def release_human_wait(self, request, pk=None):
        """Release a locally visible human-held wait through typed authority."""
        from .services.external_wait import release_human_wait
        from .services.workflow_service import WorkflowValidationError

        try:
            result = release_human_wait(self.get_object(), user=request.user)
        except WorkflowValidationError as exc:
            return Response(
                {'error': str(exc), 'code': exc.code, **exc.details},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (DjangoValidationError, DjangoPermissionDenied) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['post'], url_path='settle-human-wait')
    def settle_human_wait(self, request, pk=None):
        """Record a fulfilled blocker ask without admitting new work."""
        from .services.external_wait import settle_human_wait
        from .services.workflow_service import WorkflowValidationError

        try:
            result = settle_human_wait(
                self.get_object(), user=request.user, payload=request.data,
            )
        except WorkflowValidationError as exc:
            code = exc.code or 'human_wait_settlement_refused'
            return Response(
                {'error': str(exc), 'code': code, **exc.details},
                status=(status.HTTP_409_CONFLICT if code in {
                    'human_wait_generation_stale', 'human_wait_settlement_conflict',
                } else status.HTTP_400_BAD_REQUEST),
            )
        except DjangoPermissionDenied:
            return Response(
                {'error': 'human_wait_settlement_forbidden'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except DjangoValidationError as exc:
            return Response(
                {'error': getattr(exc, 'message_dict', None) or str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(result)

    @action(detail=True, methods=['post'], url_path='revoke-external-wait')
    def revoke_external_wait(self, request, pk=None):
        """Consumer-local cancellation; never asserts provider completion."""
        from .services.external_wait import revoke_cross_org_wait

        try:
            result = revoke_cross_org_wait(
                self.get_object(),
                user=request.user,
                reason=request.data.get('reason', ''),
            )
        except (DjangoValidationError, DjangoPermissionDenied) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['get', 'post'], url_path='provider-wait')
    def provider_wait(self, request, pk=None):
        """PLAT-10083: GET reads the provider_wait marker state for this item;
        POST is the runtime writer setting the marker at terminal-stop time.

        One action for both verbs: DRF's dynamic-route table is keyed by
        url_path, so two same-path actions collide and the later registration
        silently overwrites the earlier one (CI run 7649: GET 405). GET is the
        manager-lane read; POST accepts only provider signature classes.
        """
        from .models import ProviderWaitMarker
        from .services.provider_wait import (
            ProviderWaitError,
            set_provider_wait,
        )

        item = self.get_object()

        if request.method == 'GET':
            marker = getattr(item, 'provider_wait_marker', None)
            if marker is None:
                return Response({'item_id': item.pk, 'live': False, 'state': None})
            return Response({
                'item_id': item.pk,
                'live': marker.state == 'active',
                'state': marker.state,
                'signature_class': marker.signature_class,
                'generation': marker.generation,
                'activated_at': marker.activated_at,
                'auto_cleared_at': marker.auto_cleared_at,
                'manager_cleared_at': marker.manager_cleared_at,
                'manager_cleared_action': marker.manager_cleared_action,
            })

        try:
            marker, changed = set_provider_wait(
                item,
                signature_class=request.data.get('signature_class'),
                stop_message_id=request.data.get('stop_message_id', ''),
                source=str(request.data.get('source') or 'runtime'),
            )
        except ProviderWaitError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'item_id': item.pk,
            'changed': changed,
            'state': marker.state,
            'signature_class': marker.signature_class,
            'generation': marker.generation,
        })

    @action(detail=True, methods=['post'], url_path='provider-wait/auto-clear')
    def provider_wait_auto_clear(self, request, pk=None):
        """PLAT-10083: clear on the seat's next SUCCESSFUL turn. Success-only
        contract; refused (typed) if a terminal ANDON is fresher than the
        marker — a failed attempt can never clear."""
        from .services.provider_wait import auto_clear_provider_wait

        item = self.get_object()
        marker, cleared, refused_reason = auto_clear_provider_wait(
            item, source=str(request.data.get('source') or 'runtime-success'),
        )
        if marker is None:
            return Response({'item_id': item.pk, 'cleared': False, 'live': False,
                             'refused_reason': refused_reason})
        return Response({
            'item_id': item.pk,
            'cleared': cleared,
            'live': marker.state == 'active',
            'refused_reason': refused_reason,
        })

    @action(detail=True, methods=['post'], url_path='provider-wait/clear')
    def provider_wait_manager_clear(self, request, pk=None):
        """PLAT-10083: explicit manager-action clear
        (take_over / redispatch / acknowledge)."""
        from .services.provider_wait import (
            ProviderWaitError,
            manager_clear_provider_wait,
        )

        item = self.get_object()
        try:
            marker, cleared = manager_clear_provider_wait(
                item,
                action=request.data.get('action'),
                actor=getattr(getattr(request, 'user', None), 'email', '') or 'manager',
            )
        except ProviderWaitError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if marker is None:
            return Response({'item_id': item.pk, 'cleared': False, 'live': False})
        return Response({
            'item_id': item.pk,
            'cleared': cleared,
            'live': marker.state == 'active',
            'manager_cleared_action': marker.manager_cleared_action,
        })

    @action(detail=True, methods=['get'], url_path='operator-wait-state')
    def operator_wait_state(self, request, pk=None):
        """PLS-527: read operator_wait + evidence fingerprint before probing."""
        from .services.operator_wait import operator_wait_state_for_item

        item = self.get_object()
        state = operator_wait_state_for_item(item.pk)
        if state is None:
            return Response({
                'item_id': item.pk,
                'live': False,
                'state': None,
                'should_probe_external': True,
            })
        return Response(state)

    @action(detail=True, methods=['post'], url_path='operator-evidence-probe')
    def operator_evidence_probe(self, request, pk=None):
        """PLS-527: record/compare evidence fingerprint on the operator_wait marker.

        First complete probe parks the fingerprint; unchanged probes are no-ops;
        a changed fingerprint reactivates exactly once for this digest revision.
        """
        from .services.operator_wait import record_operator_evidence_probe

        item = self.get_object()
        try:
            result = record_operator_evidence_probe(
                item.pk,
                request.data.get('fingerprint') or request.data.get('evidence_fingerprint'),
                checklist=request.data.get('checklist') or {},
                source=request.data.get('source') or '',
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['post'], url_path='operator-evidence-event')
    def operator_evidence_event(self, request, pk=None):
        """PLS-527: canonical evidence-event path (same marker authority)."""
        from .services.operator_wait import apply_operator_evidence_event

        item = self.get_object()
        try:
            result = apply_operator_evidence_event(
                item.pk,
                request.data.get('fingerprint') or request.data.get('evidence_fingerprint'),
                source=request.data.get('source') or 'evidence_event',
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['post'], url_path='admit-external-response-hold')
    def admit_external_response_hold(self, request, pk=None):
        """Admit ADR-0064 branch 3 without caller-supplied identity fields."""
        from django.utils.dateparse import parse_datetime
        from .services.external_wait import admit_external_response_hold

        forbidden = {'organization_id', 'owner', 'owner_id', 'owner_email', 'role'}
        if forbidden & set(request.data):
            return Response(
                {'error': 'caller_supplied_identity_forbidden'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = admit_external_response_hold(
                self.get_object(),
                user=request.user,
                checkpoint_at=parse_datetime(
                    str(request.data.get('checkpoint_at') or '')
                ),
                source_evidence=request.data.get('source_evidence'),
                display_reference=request.data.get('display_reference'),
                correlation_key=request.data.get('correlation_key', ''),
            )
        except (DjangoValidationError, DjangoPermissionDenied) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='wake-external-response-hold')
    def wake_external_response_hold(self, request, pk=None):
        """Record an authorized immutable-evidence wake for branch 3."""
        from .models import ExternalResponseHoldWake
        from .services.external_wait import enqueue_external_response_wake

        forbidden = {'organization_id', 'owner', 'owner_id', 'owner_email', 'role'}
        if forbidden & set(request.data):
            return Response(
                {'error': 'caller_supplied_identity_forbidden'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = enqueue_external_response_wake(
                self.get_object(),
                generation=int(request.data.get('wait_generation')),
                kind=ExternalResponseHoldWake.KIND_EVIDENCE,
                correlation_key=request.data.get('correlation_key', ''),
                user=request.user,
                evidence=request.data.get('source_evidence'),
            )
        except (DjangoValidationError, DjangoPermissionDenied, TypeError, ValueError) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'], url_path='revoke-external-response-hold')
    def revoke_external_response_hold(self, request, pk=None):
        """Enqueue an audited generation-safe branch-3 revoke."""
        from .models import ExternalResponseHoldWake
        from .services.external_wait import enqueue_external_response_wake

        forbidden = {'organization_id', 'owner', 'owner_id', 'owner_email', 'role'}
        if forbidden & set(request.data):
            return Response(
                {'error': 'caller_supplied_identity_forbidden'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = enqueue_external_response_wake(
                self.get_object(),
                generation=int(request.data.get('wait_generation')),
                kind=ExternalResponseHoldWake.KIND_REVOKE,
                correlation_key=request.data.get('correlation_key', ''),
                user=request.user,
            )
        except (DjangoValidationError, DjangoPermissionDenied, TypeError, ValueError) as exc:
            detail = getattr(exc, 'message_dict', None) or str(exc)
            return Response({'error': detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['get'], url_path='external-response-hold-audit')
    def external_response_hold_audit(self, request, pk=None):
        """Read redacted branch-3 audit rows under current eligibility."""
        from .services.workflow_service import WorkflowService

        item = self.get_object()
        user_email = str(getattr(request.user, 'email', '') or '').strip().lower()
        owner = bool(
            user_email
            and set(WorkflowService._agent_email_aliases(user_email))
            & set(WorkflowService._agent_email_aliases(item.assignee_email or ''))
        )
        lane = bool(
            item.assignment_group
            and WorkflowService._is_team_member(
                user_email, item.assignment_group, item=item,
            )
        )
        memberships = getattr(request.user, 'organization_memberships', {}) or {}
        role = memberships.get(item.project.org_id) or memberships.get(
            str(item.project.org_id)
        )
        admin_reader = str(role).lower() in {
            'owner', 'admin', 'audit', 'auditor',
        }
        service_reader = user_email in {
            value.strip().lower()
            for value in str(
                getattr(settings, 'PULSE_EXTERNAL_RESPONSE_EVIDENCE_PRODUCERS', '')
            ).split(',')
            if value.strip()
        }
        if not (owner or lane or admin_reader or service_reader):
            return Response(status=status.HTTP_403_FORBIDDEN)
        from .models import ExternalResponseHoldAudit
        rows = ExternalResponseHoldAudit.objects.filter(
            item=item,
            organization_id=item.project.org_id,
            archived_at__isnull=True,
            created_at__gte=timezone.now() - timedelta(days=90),
        ).order_by('created_at')
        if service_reader and not (owner or lane or admin_reader):
            # Named services can inspect only actions stamped as theirs, never
            # the carrier's complete audit history.
            rows = rows.filter(
                Q(actor_email__iexact=user_email)
                | Q(service_identity__iexact=user_email)
            )
        return Response([
            {
                'event_type': row.event_type,
                'wait_generation': row.wait_generation,
                'correlation_key': row.correlation_key,
                'reason_code': row.reason_code,
                'created_at': row.created_at,
            }
            for row in rows
        ])

    @action(detail=True, methods=['get'], url_path='workflow-operations')
    def workflow_operations(self, request, pk=None):
        item = self.get_object()
        from .models import DeferredWorkflowCommand
        from .services.deferred_workflow import operation_payload
        commands = DeferredWorkflowCommand.objects.filter(item=item).order_by('-created_at')
        operation_id = request.query_params.get('operation_id')
        if operation_id:
            try:
                commands = commands.filter(pk=operation_id)
            except (ValueError, DjangoValidationError):
                return Response({'error': 'Invalid operation id'}, status=400)
        return Response([operation_payload(command, item) for command in commands[:20]])

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Mark task as completed."""
        item = self.get_object()

        if item.mode != 'task':
            return Response({'error': 'Can only complete tasks'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status in {'completed', 'done'} or item.completed_at is not None:
            return Response({
                'message': 'Task is already completed',
                'task_id': item.id
            }, status=status.HTTP_200_OK)

        # PLAT-7422 Defect 1: completing a blocker is a dependency-graph
        # mutation. The legacy ``mark_completed`` path wrote the terminal status
        # with NO dependent reconciliation (``_auto_unblock_tasks_blocked_by``
        # only fired on ``execute_transition``), so every ``blocked`` dependent
        # whose last active blocker was this item stayed blocked beside a
        # terminal blocker — and the deferred PLAT-4215 constraint rejected the
        # COMMIT with an opaque 409 (PLAT-7389 4x, PLAT-7406 1x, 2026-09-01/02).
        # Reconcile in the SAME transaction under the same fail-closed contract
        # as the transition path (PLS-953): a rejected restore rolls the whole
        # completion back and surfaces as a typed 409 naming the dependent.
        from django.db import transaction as _complete_txn
        from .services.workflow_service import (
            WorkflowService as _CompleteWFS,
            WorkflowValidationError as _CompleteWVE,
        )
        try:
            from .services.deferred_workflow import maybe_enqueue
            pending = maybe_enqueue(item, request.user, kind='complete', payload={})
            if pending:
                return Response(pending, status=status.HTTP_202_ACCEPTED)
            with _complete_txn.atomic():
                locked = Item.objects.select_for_update().get(pk=item.pk)
                if locked.status in {'completed', 'done'} or locked.completed_at is not None:
                    return Response({
                        'message': 'Task is already completed',
                        'task_id': locked.id
                    }, status=status.HTTP_200_OK)
                locked.mark_completed()
                # Larger commands were queued before this transaction. Every
                # required restore shares the root's commit on the inline path.
                _CompleteWFS._reconcile_dependency_release(
                    locked,
                    user=request.user,
                    resolved_status=locked.status,
                )
        except _CompleteWVE as exc:
            return Response(
                {
                    'error': str(exc),
                    'code': getattr(exc, 'code', '') or 'dependency_release_rejected',
                },
                status=status.HTTP_409_CONFLICT,
            )

        item = Item.objects.get(pk=item.pk)

        # NOTE: Recurring tasks are now handled by the Redis scheduler
        # (tasks/scheduler.py — KEY_RECURRING). When a recurring task is
        # marked completed, the scheduler reopens it at the next period
        # boundary in-place — we no longer spawn a child instance per
        # cycle. This avoids polluting the backlog with N child items
        # per recurring task per year. See fire_recurring_due in tasks.py.
        return Response({
            'message': (
                'Task completed successfully'
                + (
                    ' (recurring task — will reopen at next period)'
                    if item.is_recurring and item.frequency != 'one_time'
                    else ''
                )
            ),
            'task_id': item.id,
        })

    @action(detail=True, methods=['post'])
    def reopen(self, request, pk=None):
        """Reopen a completed/terminal task.

        Legacy tasks used ``completed`` -> ``pending``. Workflow-backed tasks use
        terminal slugs such as ``done`` and need to re-enter the workflow graph;
        otherwise pulse_reopen_task returns a successful-looking no-op and linked
        PRs are orphaned behind a closed task (PLS-240).
        """
        item = self.get_object()

        if item.mode != 'task':
            return Response({'error': 'Can only reopen tasks'}, status=status.HTTP_400_BAD_REQUEST)

        if item.workflow_name:
            from .services.workflow_service import WorkflowService, WorkflowValidationError

            if not WorkflowService._status_is_terminal(item.status):
                return Response({
                    'message': 'Task is not completed',
                    'task_id': item.id,
                    'item_key': item.item_key,
                    'title': item.title,
                }, status=status.HTTP_200_OK)

            workflow = Workflow.objects.filter(slug__iexact=item.workflow_name).first()
            if not workflow:
                workflow = Workflow.objects.filter(name__iexact=item.workflow_name).first()
            transition = None
            if workflow:
                candidates = WorkflowTransition.objects.filter(
                    workflow=workflow,
                    from_status=item.status,
                )
                for name, target in (
                    ('Reopen for Review', 'in_review'),
                    ('Reopen for Progress', 'in_progress'),
                    ('Reopen', 'reopened'),
                    ('Reopen', 'open'),
                ):
                    transition = candidates.filter(name__iexact=name, to_status=target).first()
                    if transition:
                        break

            if not transition:
                return Response({
                    'error': (
                        f"No reopen transition is configured for workflow '{item.workflow_name}' "
                        f"from terminal status '{item.status}'."
                    ),
                    'task_id': item.id,
                    'item_key': item.item_key,
                    'title': item.title,
                }, status=status.HTTP_409_CONFLICT)

            try:
                item = WorkflowService.execute_transition(
                    item,
                    transition.to_status,
                    user=request.user,
                    transition_id=transition.id,
                    focus_queryset=PermissionService.get_user_item_queryset(
                        request.user
                    ),
                )
            except WorkflowValidationError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            if item.completed_at is not None:
                item.completed_at = None
                item.save(update_fields=['completed_at', 'updated_at'])

            return Response({
                'message': 'Task reopened successfully',
                'task_id': item.id,
                'item_key': item.item_key,
                'title': item.title,
                'status': item.status,
            })

        if item.status != 'completed':
            return Response({
                'message': 'Task is not completed',
                'task_id': item.id,
                'item_key': item.item_key,
                'title': item.title,
            }, status=status.HTTP_200_OK)

        item.status = 'pending'
        item.completed_at = None
        item.save(update_fields=['status', 'completed_at', 'updated_at'])

        return Response({
            'message': 'Task reopened successfully',
            'task_id': item.id,
            'item_key': item.item_key,
            'title': item.title,
            'status': item.status,
        })

    @action(detail=True, methods=['post'])
    def set_reminder(self, request, pk=None):
        """Set or update reminder time for task."""
        item = self.get_object()
        reminder_time = request.data.get('reminder_time')

        if not reminder_time:
            return Response({'error': 'reminder_time is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if isinstance(reminder_time, str):
                reminder_time = datetime.fromisoformat(reminder_time.replace('Z', '+00:00'))

            item.reminder_time = reminder_time
            item.reminder_sent = False
            item.reminder_count = 0
            item.save()

            return Response({
                'message': 'Reminder set successfully',
                'item_id': item.id,
                'reminder_time': item.reminder_time
            })
        except (ValueError, TypeError) as e:
            return Response({
                'error': f'Invalid reminder_time format: {str(e)}'
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def snooze(self, request, pk=None):
        """Snooze an item (silence for a duration)."""
        item = self.get_object()
        duration_minutes = request.data.get('duration_minutes', 60)
        reason = request.data.get('reason', 'Snoozed')

        until = timezone.now() + timedelta(minutes=duration_minutes)
        item.silence(until=until, silenced_by='user', reason=reason)

        return Response({
            'message': 'Item snoozed',
            'item_id': item.id,
            'silenced_until': item.silenced_until
        })

    # === Action-specific endpoints ===

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve an action."""
        item = self.get_object()

        if item.mode != 'action':
            return Response({'error': 'Can only approve actions'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status != 'pending':
            return Response({'error': f'Cannot approve action in status: {item.status}'}, status=status.HTTP_400_BAD_REQUEST)

        comment = request.data.get('comment', '')
        approved_by = request.data.get('approved_by', 'user:manual')

        item.approve(approved_by=approved_by)

        # Notify assignee + watchers
        from .tasks import send_action_notification
        send_action_notification.delay(str(item.id))

        return Response({
            'message': 'Action approved',
            'item_id': item.id,
            'status': item.status
        })

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject an action."""
        item = self.get_object()

        if item.mode != 'action':
            return Response({'error': 'Can only reject actions'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status != 'pending':
            return Response({'error': f'Cannot reject action in status: {item.status}'}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', '')
        rejected_by = request.data.get('rejected_by', 'user:manual')

        item.reject(reason=reason, rejected_by=rejected_by)

        # Notify assignee + watchers
        from .tasks import send_action_notification
        send_action_notification.delay(str(item.id))

        return Response({
            'message': 'Action rejected',
            'item_id': item.id,
            'status': item.status,
            'reason': reason
        })

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """Force execute an approved action."""
        item = self.get_object()

        if item.mode != 'action':
            return Response({'error': 'Can only execute actions'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status not in ['approved', 'pending']:
            return Response({'error': f'Cannot execute action in status: {item.status}'}, status=status.HTTP_400_BAD_REQUEST)

        # TODO: Implement callback execution
        return Response({
            'message': 'Execution not yet implemented',
            'item_id': item.id
        }, status=status.HTTP_501_NOT_IMPLEMENTED)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Retry a failed action."""
        item = self.get_object()

        if item.mode != 'action':
            return Response({'error': 'Can only retry actions'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status != 'failed':
            return Response({'error': 'Can only retry failed actions'}, status=status.HTTP_400_BAD_REQUEST)

        if item.retry_count >= item.max_retries:
            return Response({'error': 'Max retries exceeded'}, status=status.HTTP_400_BAD_REQUEST)

        item.retry_count += 1
        item.status = 'approved'  # Reset to approved for re-execution
        item.error_message = ''
        item.save()

        return Response({
            'message': 'Action queued for retry',
            'item_id': item.id,
            'retry_count': item.retry_count
        })

    # === PLS-675 deploy-verify typed receipts (role-gated production write path) ===

    @action(detail=True, methods=['post'], url_path='issue-live-probe-receipt')
    def issue_live_probe_receipt(self, request, pk=None):
        """Mint an immutable typed live-probe receipt (QA only).

        PLS-675 / Rei P1: the gate trusts only MechanicalCommentFactory receipts
        whose issuer_email equals the authenticated causal-actor snapshot. This
        is the production write path; direct factory minting with a forged
        issuer is refused.
        """
        from .services.workflow_service import (
            WorkflowService, WorkflowValidationError, ReceiptEvidenceValidationError,
        )

        item = self.get_object()
        # PLAT-9244: evidence may be the PLAT-8177 inline-package dict (the
        # same envelope the waiver endpoint accepts) — never blind-strip it.
        evidence = request.data.get('evidence')
        if evidence is not None and not isinstance(evidence, (str, dict)):
            return Response(
                {'error': 'invalid_evidence',
                 'detail': 'evidence must be an inline package (object) or a '
                           'plain string'},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            comment = WorkflowService.issue_live_probe_receipt(
                item, actor=request.user, evidence=evidence,
            )
        except ReceiptEvidenceValidationError as exc:
            return Response(
                {'error': 'invalid_evidence', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST)
        except WorkflowValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response({
            'id': comment.id,
            'origin_event': comment.origin_event,
            'origin_event_id': comment.origin_event_id,
            'origin_payload': comment.origin_payload,
            'caused_by_email_snapshot': comment.caused_by_email_snapshot,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='issue-deploy-verify-waiver')
    def issue_deploy_verify_waiver(self, request, pk=None):
        """Mint an immutable typed deploy-verify waiver (devops/admin-ops/qa)."""
        from .services.workflow_service import WorkflowService, WorkflowValidationError
        from .services.workflow_service import ReceiptEvidenceValidationError

        item = self.get_object()
        reason = (request.data.get('reason') or '').strip()
        # PLAT-8177: the mint request must carry the authorization context —
        # an inline evidence package or a content-addressed trail-pointer.
        # Receipts without one are rejected (provenance gap).
        evidence = request.data.get('evidence')
        if evidence is not None and not isinstance(evidence, (str, dict)):
            return Response(
                {'error': 'invalid_evidence',
                 'detail': 'evidence must be an inline package (object) or a '
                           'content-addressed trail-pointer (object)'},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            comment = WorkflowService.issue_deploy_verify_waiver(
                item, actor=request.user, reason=reason, evidence=evidence,
            )
        except ReceiptEvidenceValidationError as exc:
            return Response(
                {'error': 'invalid_evidence', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST)
        except WorkflowValidationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        return Response({
            'id': comment.id,
            'origin_event': comment.origin_event,
            'origin_event_id': comment.origin_event_id,
            'origin_payload': comment.origin_payload,
            'caused_by_email_snapshot': comment.caused_by_email_snapshot,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='reclassify-acceptance')
    def reclassify_acceptance(self, request, pk=None):
        """PLAT-9427 — seat-accessible reclassification of a mis-filed
        acceptance_mode on a record-class row (live → code, downgrade-only,
        audited). Refusals are typed 400/403; success mints an immutable
        mechanical receipt.
        """
        from .services.workflow_service import WorkflowService, WorkflowValidationError
        item = self.get_object()
        target_mode = (request.data.get('target_mode') or 'code').strip().lower()
        if target_mode not in ('code',):
            return Response(
                {'error': 'invalid_target_mode',
                 'detail': 'only "code" is supported (live → code, downgrade-only)'},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            comment = WorkflowService.reclassify_acceptance_mode(
                item, actor=request.user, target_mode=target_mode,
                reason=request.data.get('reason') or '',
            )
        except WorkflowValidationError as exc:
            msg = str(exc)
            if 'refused:' in msg or 'requires' in msg or 'automation' in msg:
                return Response({'error': 'reclassification_refused',
                                 'detail': msg},
                                status=status.HTTP_403_FORBIDDEN)
            return Response({'error': 'invalid_reclassification',
                             'detail': msg},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'id': comment.id,
            'origin_event': comment.origin_event,
            'origin_event_id': comment.origin_event_id,
            'origin_payload': comment.origin_payload,
            'acceptance_mode': item.acceptance_mode,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='focused-qa-verify')
    def focused_qa_verify(self, request, pk=None):
        """PLS-695 — run the Focused-QA verifier over an item (read-only).

        Returns a fully-provenanced verdict: artifact kind, evidence schema +
        version, classifier inputs, per-field findings, every source queried
        (with cursors/pages/linked artifacts), and the terminal observation
        state. NEVER mutates the source record and NEVER files a bug — a
        ``not_observable`` outcome must not be rendered as evidence-absent.
        """
        item = self.get_object()
        if not (request.user.is_staff or request.user.is_superuser):
            if not PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists():
                raise NotFound('Item not found')
        from .services.focused_qa_verifier import verify_item
        verdict = verify_item(item)
        return Response(verdict.as_dict())

    # === Alert-specific endpoints ===

    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """Acknowledge an alert."""
        item = self.get_object()

        if item.mode != 'alert':
            return Response({'error': 'Can only acknowledge alerts'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status not in ['firing']:
            return Response({'error': f'Cannot acknowledge alert in status: {item.status}'}, status=status.HTTP_400_BAD_REQUEST)

        comment = request.data.get('comment', '')
        acknowledged_by = request.data.get('acknowledged_by', 'user:manual')

        item.acknowledge(acknowledged_by=acknowledged_by, comment=comment)

        return Response({
            'message': 'Alert acknowledged',
            'item_id': item.id,
            'status': item.status
        })

    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        """Resolve an alert."""
        item = self.get_object()

        if item.mode != 'alert':
            return Response({'error': 'Can only resolve alerts'}, status=status.HTTP_400_BAD_REQUEST)

        if item.status not in ['firing', 'acknowledged']:
            return Response({'error': f'Cannot resolve alert in status: {item.status}'}, status=status.HTTP_400_BAD_REQUEST)

        note = request.data.get('resolution_note', '')
        resolved_by = request.data.get('resolved_by', 'user:manual')

        item.resolve(resolved_by=resolved_by, note=note)

        return Response({
            'message': 'Alert resolved',
            'item_id': item.id,
            'status': item.status
        })

    @action(detail=True, methods=['post'])
    def silence(self, request, pk=None):
        """Silence an item."""
        item = self.get_object()
        duration_minutes = request.data.get('duration_minutes')
        reason = request.data.get('reason', '')

        if not duration_minutes:
            return Response({'error': 'duration_minutes is required'}, status=status.HTTP_400_BAD_REQUEST)

        until = timezone.now() + timedelta(minutes=duration_minutes)
        item.silence(until=until, silenced_by='user:manual', reason=reason)

        return Response({
            'message': 'Item silenced',
            'item_id': item.id,
            'silenced_until': item.silenced_until
        })

    # === Filtered views ===

    @action(detail=False, methods=['get'])
    def tasks(self, request):
        """Get all tasks."""
        queryset = self.get_queryset().filter(mode='task')
        serializer = ItemListSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'tasks': serializer.data
        })

    @action(detail=True, methods=['get'])
    def stage_history(self, request, pk=None):
        """PLS-13: per-status dwell intervals for this item, derived from the
        ItemActivity status-transition log (status, entered_at, exited_at,
        duration_seconds; last interval is open)."""
        from .services import stage_metrics
        item = self.get_object()
        return Response(stage_metrics.item_stage_history(item))

    @action(detail=False, methods=['get'])
    def team_throughput(self, request):
        """PLS-13: per-team x status avg/p90 dwell + throughput (transitions
        out/day) over a window. Query params: ?days=N (default 14), ?project=KEY."""
        from .services import stage_metrics
        try:
            days = int(request.query_params.get('days', 14))
        except (TypeError, ValueError):
            days = 14
        project = request.query_params.get('project') or None
        return Response({
            'window_days': days,
            'project': project,
            'rows': stage_metrics.team_throughput(days=days, project_key=project),
        })

    @action(detail=False, methods=['get'])
    def cycle_times(self, request):
        """PLS-362: per-item cycle/lead time for items completed in the window,
        for the analytics scatterplot/histogram. Query params: ?days=N
        (default 30), ?project=KEY."""
        from .services import stage_metrics
        try:
            days = int(request.query_params.get('days', 30))
        except (TypeError, ValueError):
            days = 30
        project = request.query_params.get('project') or None
        payload = stage_metrics.cycle_times(days=days, project_key=project)
        payload['project'] = project
        return Response(payload)

    @action(detail=False, methods=['get'])
    def cumulative_flow(self, request):
        """PLS-361: end-of-day status counts per day for the analytics
        cumulative-flow diagram. Query params: ?days=N (default 14, max 90),
        ?project=KEY."""
        from .services import stage_metrics
        try:
            days = int(request.query_params.get('days', 14))
        except (TypeError, ValueError):
            days = 14
        project = request.query_params.get('project') or None
        payload = stage_metrics.cumulative_flow(days=days, project_key=project)
        payload['project'] = project
        return Response(payload)

    @action(detail=False, methods=['get'], url_path='org-progress')
    def org_progress(self, request):
        """Org-scoped progress snapshot for the shizuha.com home dashboard.

        Combines, all filtered to the CALLER's org scope (via get_queryset —
        an explicit ?organization=ID must be one the caller belongs to, else
        403; HIVE-327 tenant-isolation contract):
          - timeseries: per-bucket created/completed/terminal task counts from
            the ItemActivity transition log (the resolution-rate + intake trend).
          - by_status: current open-vs-terminal status distribution (the donut).
          - throughput: per-team x status dwell + throughput (bottleneck bars).
          - snapshot: headline counts (blocked / in_progress / open / overdue).

        Query params: ?hours=N (default 24, max 720) & ?buckets=N (default 24)
        for the trend; ?days=N (default 7) for throughput/dwell; ?project=KEY.
        """
        from .services import stage_metrics
        from django.db.models import Count
        from django.utils import timezone as _tz

        def _int(name, default):
            try:
                return int(request.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        hours = _int('hours', 24)
        buckets = _int('buckets', 24)
        days = _int('days', 7)
        project = request.query_params.get('project') or None

        # Caller/org-scoped item queryset (permission + org filtered).
        scoped = self.get_queryset().exclude(mode='alert')
        if project:
            scoped = scoped.filter(project__key=str(project).upper())

        timeseries = stage_metrics.progress_timeseries(
            hours=hours, buckets=buckets, items=scoped)
        throughput = stage_metrics.team_throughput(days=days, items=scoped)
        open_dwell = stage_metrics.current_stage_dwell(items=scoped)

        by_status = {
            (row['status'] or 'unknown'): row['n']
            for row in scoped.order_by().values('status').annotate(n=Count('id', distinct=True))
        }
        terminal_set = set(TERMINAL_STATUS_SLUGS)
        waiting_set = set(WAITING_QUEUE_STATUS_SLUGS)
        open_total = sum(n for s, n in by_status.items() if s not in terminal_set)
        blocked = sum(n for s, n in by_status.items() if s in waiting_set)
        in_progress = by_status.get('in_progress', 0) + by_status.get('in_review', 0)

        now = _tz.now()
        overdue = scoped.filter(
            due_date__lt=now).exclude(status__in=TERMINAL_STATUS_SLUGS).count()

        return Response({
            'project': project,
            'timeseries': timeseries,
            'by_status': by_status,
            'throughput': throughput,
            'open_dwell': open_dwell,
            'snapshot': {
                'open': open_total,
                'blocked': blocked,
                'in_progress': in_progress,
                'overdue': overdue,
                'completed_window': timeseries['totals']['completed'],
                'created_window': timeseries['totals']['created'],
                'terminal_window': timeseries['totals']['terminal'],
            },
        })

    @action(detail=False, methods=['get'])
    def agent_overview(self, request):
        """PLS-20: per-agent org-chart cards grouped by team. For each team in the
        pod roster, each agent's live task aggregates from the data Pulse already
        owns — active (non-terminal, non-waiting) assigned, waiting
        (blocked/scheduled), a per-status breakdown, and completed counts
        (24h / 7d). Cheap because Pulse owns the task data. The frontend layers on
        the exporter-sourced status/error-rate/last-activity and PLS-13 cycle time
        (see `team_throughput`) and links each card to the agent's queue."""
        from django.db.models import Count
        from django.utils import timezone
        from datetime import timedelta
        from .services.workflow_service import WorkflowService
        terminal = {s.lower() for s in TERMINAL_STATUS_SLUGS}
        waiting = {s.lower() for s in WAITING_QUEUE_STATUS_SLUGS}
        now = timezone.now()
        d1 = now - timedelta(days=1)
        d7 = now - timedelta(days=7)
        health = _scrape_agent_health()  # {short_name: {status, auth_errors, last_activity_s}}
        teams = []
        for team, members in WorkflowService._pod_roster().items():
            agents = []
            seen = set()
            for email in WorkflowService._flat_roster(members):
                if not email or email.lower() in seen:
                    continue
                seen.add(email.lower())
                qs = Item.objects.filter(assignee_email__iexact=email)
                active = qs.exclude(status__in=terminal)
                working = active.exclude(status__in=waiting)  # non-terminal, non-waiting
                by_status = {row['status']: row['c']
                             for row in active.values('status').annotate(c=Count('id'))}
                # PLS-20 REQUIRED: "what the agent is on right now" — the most
                # recently touched active (non-waiting) item (key + title).
                cur = working.order_by('-updated_at').only('item_key', 'title', 'status').first()
                current_item = (
                    {'key': cur.item_key, 'title': cur.title, 'status': cur.status}
                    if cur else None
                )
                agents.append({
                    'email': email,
                    'active': working.count(),
                    'waiting': active.filter(status__in=waiting).count(),
                    'by_status': by_status,
                    'current_item': current_item,
                    'completed_24h': qs.filter(completed_at__gte=d1).count(),
                    'completed_7d': qs.filter(completed_at__gte=d7).count(),
                    'health': health.get(email.split('@')[0], {}),
                })
            teams.append({'team': team, 'agents': agents})
        return Response({'generated_at': now.isoformat(), 'teams': teams})

    @action(detail=False, methods=['get'], url_path='agent-activity')
    def agent_activity(self, request):
        """PLS-305 (PLS-270 Slice B): agent coordination console read model —
        roster + availability + per-agent WIP workstreams with blockers, PR
        links, and next workflow transitions, projected from canonical Pulse
        state (Item/ItemLink/metadata.pull_requests/TeamRoster/
        AgentAvailability). Query params: ?team=SLUG, ?project=KEY."""
        from .services import agent_activity
        return Response(agent_activity.agent_activity_snapshot(
            team=request.query_params.get('team') or None,
            project_key=request.query_params.get('project') or None,
        ))

    @action(detail=False, methods=['get'])
    def actions(self, request):
        """Get all actions."""
        queryset = self.get_queryset().filter(mode='action')
        serializer = ItemListSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'actions': serializer.data
        })

    @action(detail=False, methods=['get'])
    def alerts(self, request):
        """Get all alerts (default: firing + acknowledged)."""
        queryset = self.get_queryset().filter(mode='alert')
        # Default to active alerts only
        if not request.query_params.get('status'):
            queryset = queryset.filter(status__in=['firing', 'acknowledged'])
        serializer = AlertSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'alerts': serializer.data
        })

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending items assigned to the current user (tasks due, actions pending approval, alerts firing)."""
        now = timezone.now()
        today_end = now.replace(hour=23, minute=59, second=59)
        user = request.user

        # Get user's ID and email for filtering
        user_id = getattr(user, 'id', None)
        user_email = getattr(user, 'email', None)

        # Base queryset filtered by current user (assigned to them)
        base_filter = Q(assignee_id=user_id) if user_id else Q()
        if user_email:
            base_filter |= Q(assignee_email=user_email)

        tasks = self.get_queryset().filter(
            base_filter,
            mode='task',
            status__in=['pending', 'in_progress'],
        ).filter(
            Q(due_date__lte=today_end) | Q(due_date__isnull=True)
        )
        actions = self.get_queryset().filter(
            base_filter,
            mode='action',
            status='pending'
        )
        alerts = self.get_queryset().filter(
            base_filter,
            mode='alert',
            status__in=['firing', 'acknowledged']
        )

        return Response({
            'tasks': ItemListSerializer(tasks, many=True).data,
            'actions': ItemListSerializer(actions, many=True).data,
            'alerts': AlertSerializer(alerts, many=True).data,
        })

    @action(detail=False, methods=['get'])
    def overdue(self, request):
        """Get all overdue items."""
        now = timezone.now()
        items = self.get_queryset().filter(
            due_date__lt=now,
            status__in=['pending', 'in_progress', 'firing', 'acknowledged']
        )
        serializer = ItemListSerializer(items, many=True)
        return Response({
            'count': items.count(),
            'items': serializer.data
        })

    @action(detail=False, methods=['get'])
    def due_soon(self, request):
        """Get items due within specified hours (default 24)."""
        hours = int(request.query_params.get('hours', 24))
        now = timezone.now()
        due_threshold = now + timedelta(hours=hours)

        items = self.get_queryset().filter(
            due_date__gte=now,
            due_date__lte=due_threshold,
            status__in=['pending', 'in_progress', 'firing', 'acknowledged']
        )
        serializer = ItemListSerializer(items, many=True)
        return Response({
            'count': items.count(),
            'hours': hours,
            'items': serializer.data
        })

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Get today's summary for the current user."""
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        # Use user-scoped queryset
        base_queryset = self._get_user_scoped_queryset()

        tasks_due_today = base_queryset.filter(
            mode='task',
            due_date__gte=today_start,
            due_date__lte=today_end,
            status__in=['pending', 'in_progress']
        )
        tasks_overdue = base_queryset.filter(
            mode='task',
            due_date__lt=today_start,
            status__in=['pending', 'in_progress']
        )
        actions_pending = base_queryset.filter(
            mode='action',
            status='pending'
        )
        alerts_firing = base_queryset.filter(
            mode='alert',
            status='firing'
        )
        alerts_acknowledged = base_queryset.filter(
            mode='alert',
            status='acknowledged'
        )

        return Response({
            'tasks_due_today': tasks_due_today.count(),
            'tasks_overdue': tasks_overdue.count(),
            'actions_pending': actions_pending.count(),
            'alerts_firing': alerts_firing.count(),
            'alerts_acknowledged': alerts_acknowledged.count(),
            'tasks': ItemListSerializer(tasks_due_today, many=True).data,
            'actions': ItemListSerializer(actions_pending, many=True).data,
            'alerts': AlertSerializer(alerts_firing | alerts_acknowledged, many=True).data,
        })

    @action(detail=False, methods=['get'], url_path='heartbeat-preflight')
    def heartbeat_preflight(self, request):
        """Return a bounded scheduler decision for the authenticated agent.

        Agent runtimes call this before spending provider tokens on a periodic
        heartbeat.  Keep this path deliberately independent of the generic
        item-list paginator, serializer, permission resolver, and queue sort:
        the scheduler needs only proof that at least one alert/task exists, not
        an exact total or task payload.

        Presence fields are 0/1 markers, not exact counts.  A non-privileged
        principal may only inspect its own canonical Shizuha email identity.
        """
        requested_email = (
            request.query_params.get('assignee_email')
            or getattr(request.user, 'email', '')
            or ''
        ).strip().lower()
        if not requested_email:
            return Response(
                {
                    'detail': 'assignee_email is required',
                    'code': 'heartbeat_preflight_owner_required',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .services.workflow_service import WorkflowService

        caller_email = (getattr(request.user, 'email', '') or '').strip().lower()
        is_privileged = bool(
            getattr(request.user, 'is_staff', False)
            or getattr(request.user, 'is_superuser', False)
        )
        # Operator 2026-09-24: the agent's identity comes from the SIGNED
        # TOKEN, never from what the request claims. A non-privileged caller's
        # assignee_email param is advisory only — the lookup always runs on the
        # token identity, so a stale/miss-scoped param cannot point the
        # scheduler at the wrong agent (or an empty one) and park a loaded
        # agent idle. Cross-org work is included by design: an agent's queue
        # is every org its identity holds work in.
        if not is_privileged:
            requested_email = caller_email or requested_email
        is_self = WorkflowService._emails_same_agent_identity(
            caller_email,
            requested_email,
        )
        if not is_privileged and not is_self:
            raise PermissionDenied(
                'Heartbeat preflight may only inspect the authenticated owner.'
            )

        email_variants = {requested_email}
        if requested_email.endswith('@shizuha.com'):
            email_variants.add(
                requested_email[:-len('@shizuha.com')] + '@agents.shizuha.io'
            )
        elif requested_email.endswith('@agents.shizuha.io'):
            email_variants.add(
                requested_email[:-len('@agents.shizuha.io')] + '@shizuha.com'
            )

        # EmailField values and JWT identities are normalized to lowercase.
        # Keep this as an exact IN predicate so PostgreSQL can use the
        # PLS-698 (assignee_email, mode, is_archived, status) control-plane
        # index; UPPER(email)=UPPER(...) would defeat that index.
        owner_filter = Q(assignee_email__in=email_variants)
        if is_self and getattr(request.user, 'id', None) is not None:
            owner_filter |= (
                Q(assignee_id=request.user.id)
                & (Q(assignee_email__isnull=True) | Q(assignee_email=''))
            )

        owner_items = (
            Item.objects
            .filter(owner_filter, is_archived=False)
            .exclude(project__is_archived=True)
        )

        def response_payload(
            decision,
            reason,
            *,
            alert_count=0,
            ready_task_count=0,
            backlog_count=0,
            blocked_task_count=0,
            future_due_count=0,
            owner_awareness_count=0,
        ):
            return Response({
                'decision': decision,
                'reason': reason,
                'alert_count': int(bool(alert_count)),
                'ready_task_count': int(bool(ready_task_count)),
                'backlog_count': int(bool(backlog_count)),
                'blocked_task_count': int(bool(blocked_task_count)),
                'future_due_count': int(bool(future_due_count)),
                'owner_awareness_count': int(bool(owner_awareness_count)),
                'counts_are_presence_markers': True,
            })

        if owner_items.filter(
            mode='alert',
            status__in=('firing', 'acknowledged'),
        ).exists():
            return response_payload('run', 'active_alert', alert_count=1)

        owner_tasks = owner_items.filter(mode='task')
        active_tasks = annotate_owner_queue_projection(
            filter_active_queue_items(owner_tasks)
        )
        future_recurring = future_recurring_owner_waiting_q()
        ready_tasks = (
            active_tasks
            .filter(_queue_is_waiting=False)
            .exclude(future_recurring)
            # Handoff statuses are still "active" on the assignee queue but
            # are not work THIS seat can progress with a model turn.
            # Scout 2026-08-31: personal in_review counted as ready_task,
            # so idle heartbeats pre-warmed a 255k GLM prefix every 90s
            # and never called pulse_get_my_tasks.
            .exclude(status__in=(
                'in_review', 'review', 'awaiting_merge', 'awaiting_review',
                'awaiting_deploy', 'in_qa', 'verification',
            ))
        )
        if ready_tasks.exists():
            return response_payload('run', 'ready_task', ready_task_count=1)

        blocked_present = active_tasks.filter(status='blocked').exists()
        future_or_holding_present = active_tasks.filter(
            Q(_queue_is_waiting=True) | future_recurring
        ).exists()
        # Held-andon in_progress (PLS-278 destination unresolved) is waiting,
        # not ready. Do not wake a DeepSeek turn, and do not pull backlog
        # onto a seat whose only "WIP" is an unseated operator ask.
        unresolved_operator_hold = active_tasks.filter(
            _queue_is_waiting=True,
        ).exclude(future_recurring).exclude(_queue_owner_awareness=True).filter(
            status__in=('in_progress', 'todo', 'open', 'waiting_external'),
        ).exists()
        # Hiro 2026-08-17: backlog_pull used to beat blocked. Preflight then
        # woke a 129k DeepSeek turn, but pulse_get_my_tasks hides backlog on
        # the default page, so the model only saw the blocked item and spent
        # three turns restating all_blocked. Do not pull more WIP onto a
        # blocked seat. Backlog is a pull lane only when nothing is blocked.
        if blocked_present or unresolved_operator_hold:
            return response_payload(
                'skip',
                'all_blocked',
                blocked_task_count=1,
                future_due_count=future_or_holding_present,
            )

        # PLS-527 / PLAT-4850: operator-evidence-waiting work must not wake a
        # heartbeat via backlog_pull. Consume the same PLS-529 marker predicate
        # used by actionable queue / failover callers — never a parallel rule.
        # Placed AFTER the blocked check: backlog_pull must never beat blocked
        # (Hiro 2026-08-17 hardening — the branch's pre-hardening early check
        # resurrected that bug on the refreshed tree, premerge run 6603).
        from .services.operator_wait import exclude_operator_wait_items
        if exclude_operator_wait_items(
            owner_tasks.filter(status='backlog')
        ).exists():
            return response_payload('run', 'backlog_pull', backlog_count=1)
        owner_awareness_only = False
        if future_or_holding_present:
            if active_tasks.filter(
                Q(_queue_is_waiting=True) | future_recurring,
            ).exclude(_queue_owner_awareness=True).exists():
                return response_payload(
                    'skip', 'future_due', future_due_count=1,
                )
            owner_awareness_only = True
        # Hive grants Cortex leases on team_queue (unstarted team work). SCLI
        # only asked this endpoint about personal assignee_email, so holders
        # skip-model'd with queue_empty while Engineering/security still had
        # unassigned ready/needs_triage (sara/revi 2026-08-22).
        team_bindings = _heartbeat_preflight_team_bindings(requested_email)
        if team_bindings:
            # Team slugs repeat across organizations. Retain the roster's org
            # alongside its team so another tenant's queue cannot wake this seat.
            team_scope = Q()
            for org_id, team_name in team_bindings:
                team_scope |= Q(project__org_id=org_id, assignment_group=team_name)
            team_unstarted = Item.objects.filter(
                team_scope,
                mode='task',
                is_archived=False,
                status__in=('open', 'todo', 'doing', 'ready', 'needs_triage'),
            ).exclude(project__is_archived=True).filter(
                Q(assignee_email__isnull=True) | Q(assignee_email=''),
            )
            if team_unstarted.exists():
                return response_payload(
                    'run',
                    'team_unstarted',
                    ready_task_count=1,
                )
        if owner_awareness_only:
            return response_payload(
                'skip', 'awareness_only', owner_awareness_count=1,
            )
        return response_payload('skip', 'queue_empty')

    @action(detail=False, methods=['get'])
    def my_tasks(self, request):
        """
        Get items assigned to the current user (or specified user for staff).
        Regular users can only see their own tasks.
        Staff can query any user's tasks by passing user_id.
        """
        user_id = request.query_params.get('user_id')

        # Default to current user
        if not user_id:
            user_id = request.user.id
        else:
            # Non-staff can only see their own tasks
            if not request.user.is_staff and int(user_id) != request.user.id:
                return Response(
                    {'error': 'You can only view your own tasks'},
                    status=status.HTTP_403_FORBIDDEN
                )

        # PLS-382: resolve tasks by the FEDERATED-stable email, not only
        # assignee_id. Pulse auth is federated (FederatedUser.id = the
        # shizuha-id user_id from the JWT), but items are historically keyed
        # by assignee_email and their assignee_id points at whatever local
        # pulse user existed when the task was assigned. Expand the known
        # Shizuha agent/company alias pair here too so the my_tasks legacy
        # action stays consistent with the /api/items/ queue path and the
        # broader PermissionService owner scope.
        principal_email = None
        if not request.query_params.get('user_id'):
            principal_email = (getattr(request.user, 'email', '') or '').strip()
        else:
            # Staff querying another user_id: resolve that user's email so the
            # id-vs-email split doesn't hide their tasks either.
            try:
                from .user_service import user_service as _us
                user_info = _us.get_user_by_id(int(user_id))
                principal_email = (user_info or {}).get('email') if user_info else None
            except Exception:
                principal_email = None

        assignee_filter = Q(assignee_id=user_id)
        if principal_email:
            email = principal_email.lower()
            assignee_filter |= Q(assignee_email__iexact=email)
            if email.endswith('@shizuha.com'):
                assignee_filter |= Q(assignee_email__iexact=email[:-len('@shizuha.com')] + '@agents.shizuha.io')
            elif email.endswith('@agents.shizuha.io'):
                assignee_filter |= Q(assignee_email__iexact=email[:-len('@agents.shizuha.io')] + '@shizuha.com')

        items = self.get_queryset().filter(assignee_filter)
        # PLS-88: annotate recent-activity signal so agents can detect new
        # reviewer feedback on held tasks without a per-task pulse_list_comments
        # fetch (the SCLI-76 churn pattern). Subquery (not a join+aggregate) so
        # it doesn't multiply rows or disturb the queue_order annotations/sort.
        # PLS-1294: this is an attention signal — system lifecycle/audit
        # comments (failover/rebalance chatter) must not count, or failover
        # storms inflate every returning user's what-needs-me badge.
        _comments = Comment.objects.filter(
            item=OuterRef('pk'),
            origin_kind__in=Comment.SUBSTANTIVE_ORIGIN_KINDS,
        )
        items = items.annotate(
            _comment_count=Coalesce(
                Subquery(
                    _comments.values('item').annotate(c=Count('id')).values('c')
                ),
                0,
            ),
            _last_comment_at=Subquery(
                _comments.order_by('-created_at').values('created_at')[:1]
            ),
        )
        serializer = ItemListSerializer(items, many=True)
        return Response({
            'user_id': int(user_id),
            'count': items.count(),
            'items': serializer.data
        })

    @action(detail=False, methods=['get'])
    def recurring_tasks(self, request):
        """Get all recurring task templates."""
        items = self.get_queryset().filter(
            mode='task',
            is_recurring=True,
            parent_item__isnull=True
        )
        serializer = RecurringTaskSerializer(items, many=True)
        return Response({
            'count': items.count(),
            'recurring_tasks': serializer.data
        })

    @action(detail=False, methods=['get'])
    def assignable_users(self, request):
        """
        Get list of users that can be used for assignee filter.

        Returns users who are members of projects the current user can access.
        Superusers get all unique assignees across all organizations.

        Query params:
            project: Filter to specific project
            organization: Filter to specific organization
        """
        user = request.user
        project_id = request.query_params.get('project')
        organization_id = request.query_params.get('organization')

        users = []
        seen = set()

        def _resolve_name(uid, email):
            """Resolve full name from shizuha-id, fallback to email prefix."""
            if uid:
                info = user_service.get_user_by_id(uid)
                if info:
                    first = info.get('first_name', '').strip()
                    last = info.get('last_name', '').strip()
                    full = f"{first} {last}".strip()
                    if full:
                        return full
            return email.split('@')[0] if email else 'Unknown'

        if user.is_superuser:
            # Superuser: return all unique assignees from all items
            # Include items assigned by EMAIL only (no assignee_id) — common for agent-created tasks
            assignees = Item.objects.exclude(
                assignee_id__isnull=True, assignee_email__isnull=True
            ).exclude(
                assignee_id__isnull=True, assignee_email=''
            ).values('assignee_id', 'assignee_email').distinct()

            # Deduplicate by email when assignee_id is null but email is set
            email_to_id = {}
            for a in assignees:
                aid, email = a['assignee_id'], a['assignee_email']
                if aid and email:
                    email_to_id[email] = aid  # prefer rows that have an id

            for a in assignees:
                aid, email = a['assignee_id'], a['assignee_email']
                # Skip null-id rows when we have a row with an id for the same email
                if aid is None and email and email in email_to_id:
                    continue
                key = (aid, email)
                if key not in seen:
                    seen.add(key)
                    users.append({
                        'user_id': aid,
                        'email': email,
                        'user_name': _resolve_name(aid, email),
                    })
        else:
            # Get accessible projects
            accessible_projects = PermissionService.get_user_projects(
                user=user,
                organization_id=organization_id,
                user_email=getattr(user, 'email', None)
            )

            if project_id:
                accessible_projects = accessible_projects.filter(id=project_id)

            # Get members from these projects (PLS-832: identity-dedupe)
            for project in accessible_projects:
                members = PermissionService.dedupe_project_memberships(
                    PermissionService.get_valid_project_memberships(project),
                )

                for member in members:
                    key = (member.user_id, (member.user_email or '').lower())
                    if key not in seen:
                        seen.add(key)
                        users.append({
                            'user_id': member.user_id,
                            'email': member.user_email,
                            'user_name': _resolve_name(member.user_id, member.user_email),
                        })

        return Response({
            'count': len(users),
            'users': users
        })

    @action(detail=False, methods=['get'], url_path='search-org-members')
    def search_org_members(self, request):
        """
        Search organization members by name or email.

        Query params:
            search: Search term (min 2 chars)
            organization: Optional organization ID. When omitted (the @-mention
                autocomplete case), returns the deduped union of members across
                every organization the caller can access.
        """
        search = request.query_params.get('search', '').strip()
        org_id = request.query_params.get('organization')

        from .services.admin_service import admin_service

        if org_id:
            org_ids = [int(org_id)]
        else:
            accessible = PermissionService.get_user_projects(
                request.user.id,
                user_email=getattr(request.user, 'email', None),
                user=request.user,
            )
            org_ids = list(
                accessible.exclude(org_id__isnull=True)
                .values_list('org_id', flat=True)
                .distinct()
            )

        if not org_ids:
            return Response({'count': 0, 'members': []})

        result = []
        seen = set()
        for oid in org_ids:
            org_members = admin_service.get_org_members(
                int(oid), search=search if search else None
            )
            for member in org_members:
                user_email = member.get('user_email') or member.get('email')
                user_id = member.get('user_id') or member.get('id')
                user_name = member.get('user_name') or member.get('name') or member.get('username')
                if not user_email:
                    continue
                key = user_id or user_email.lower()
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    'user_id': user_id,
                    'user_email': user_email,
                    'user_name': user_name or user_email.split('@')[0],
                    'role': member.get('role', 'member'),
                })

        return Response({'count': len(result), 'members': result})

    @action(detail=False, methods=['get'])
    def team_tasks(self, request):
        """
        Get tasks grouped by team member or assignment_group.

        Shows tasks for all team members in accessible projects,
        grouped by assignee or by assignment_group (team).

        Query params:
            project: Filter to specific project
            organization: Filter to specific organization
            include_completed: Include completed tasks (default: false)
            mode: Filter by mode (default: task)
            group_by: 'assignee' (default) or 'team' (group by assignment_group)
        """
        user = request.user
        project_id = request.query_params.get('project')
        organization_id = request.query_params.get('organization')
        include_completed = request.query_params.get('include_completed', 'false').lower() == 'true'
        mode = request.query_params.get('mode', 'task')
        group_by = request.query_params.get('group_by', 'assignee')

        # Get base queryset respecting user permissions
        if user.is_superuser:
            # Superuser: access all items
            queryset = Item.objects.filter(mode=mode)
            if project_id:
                queryset = queryset.filter(project_id=project_id)
            if organization_id:
                queryset = queryset.filter(project__org_id=organization_id)
        else:
            # Regular users: only accessible projects
            accessible_projects = PermissionService.get_user_projects(
                user.id,
                organization_id,
                user_email=getattr(user, 'email', None)
            )

            if project_id:
                accessible_projects = accessible_projects.filter(id=project_id)

            queryset = Item.objects.filter(
                mode=mode,
                project__in=accessible_projects
            )

        # Exclude all terminal/done statuses when include_completed=false (the default).
        # The old code only excluded status='completed', which left cancelled, rejected,
        # closed, done, deferred, etc. visible — causing 71% of rows to be terminal (PLS-45).
        if not include_completed:
            from .models import Status as _Status
            _terminal_slugs = set(
                _Status.objects.filter(category='done').values_list('slug', flat=True)
            )
            _terminal_slugs.update(TERMINAL_STATUS_SLUGS)
            queryset = queryset.exclude(status__in=_terminal_slugs)

        # Only include items in projects (not personal items)
        queryset = queryset.filter(project__isnull=False)
        # PLS-7: PermissionService.get_user_projects() still returns SOFT-archived
        # (deleted) projects, so this workload view must drop their tasks to match
        # the archived-project filter on the individual item endpoints.
        queryset = queryset.exclude(project__is_archived=True)

        from .services.team_page import (
            MEMBER_TASK_LIMIT,
            annotate_list_comments,
            assignee_summary,
            team_summary,
        )

        # PLAT-321: one Redis read for the whole page. Absent / Redis-down → unknown.
        avail_map = {}
        try:
            import json as _json
            from .services.workflow_service import WorkflowService
            _raw = WorkflowService._redis_client().get('pulse:agent_availability')
            if _raw:
                avail_map = {str(k).lower(): v for k, v in _json.loads(_raw).items()}
        except Exception:
            avail_map = {}

        def _availability_status(*emails):
            for email in emails:
                value = avail_map.get((email or '').lower())
                if value is True:
                    return 'active'
                if value is False:
                    return 'stopped'
            return 'unknown'

        def _task_page(items_qs):
            total = items_qs.count()
            page = annotate_list_comments(
                items_qs.select_related('project', 'parent_item').order_by('-updated_at')
            )[:MEMBER_TASK_LIMIT]
            return {
                'task_count': total,
                'truncated': total > MEMBER_TASK_LIMIT,
                'tasks': ItemListSerializer(page, many=True).data,
            }

        if group_by == 'team':
            team_name = (request.query_params.get('team') or '').strip()
            if team_name:
                if team_name == 'Unassigned':
                    detail = queryset.filter(
                        Q(assignment_group__isnull=True) | Q(assignment_group='')
                    )
                else:
                    detail = queryset.filter(assignment_group=team_name)
                payload = _task_page(detail)
                payload['team_name'] = team_name
                return Response(payload)
            result = team_summary(queryset)
            return Response({
                'count': len(result),
                'group_by': 'team',
                'teams': result,
            })

        assignee_email = (request.query_params.get('assignee_email') or '').strip()
        if assignee_email:
            payload = _task_page(queryset.filter(assignee_email__iexact=assignee_email))
            payload['assignee_email'] = assignee_email
            return Response(payload)

        result = assignee_summary(queryset)
        for row in result:
            row['availability_status'] = _availability_status(row['assignee_email'])

        return Response({
            'count': len(result),
            'group_by': 'assignee',
            'team_members': result,
        })

    @action(detail=False, methods=['get'])
    def user_workload(self, request):
        """
        Get workload statistics per user.

        Returns task counts grouped by user with statistics:
        - total: Total assigned tasks
        - pending: Pending tasks
        - in_progress: In-progress tasks
        - completed: Completed tasks
        - overdue: Overdue tasks
        - created: Tasks created by user (reporter)

        Query params:
            project: Filter to specific project
            organization: Filter to specific organization
        """
        user = request.user
        project_id = request.query_params.get('project')
        organization_id = request.query_params.get('organization')

        # Get base queryset respecting user permissions
        if user.is_superuser:
            queryset = Item.objects.filter(mode='task')
            if project_id:
                queryset = queryset.filter(project_id=project_id)
            if organization_id:
                queryset = queryset.filter(project__org_id=organization_id)
        else:
            accessible_projects = PermissionService.get_user_projects(
                user.id,
                organization_id,
                user_email=getattr(user, 'email', None)
            )
            if project_id:
                accessible_projects = accessible_projects.filter(id=project_id)
            queryset = Item.objects.filter(mode='task', project__in=accessible_projects)

        # Only project items
        queryset = queryset.filter(project__isnull=False)
        # PLS-7: exclude SOFT-archived (deleted) projects' tasks from workload —
        # get_user_projects() still includes archived projects.
        queryset = queryset.exclude(project__is_archived=True)

        from django.utils import timezone
        from .services.team_page import workload_summary

        avail_map = {}
        try:
            import json as _json
            from .services.workflow_service import WorkflowService
            _raw = WorkflowService._redis_client().get('pulse:agent_availability')
            if _raw:
                avail_map = {str(k).lower(): v for k, v in _json.loads(_raw).items()}
        except Exception:
            avail_map = {}

        result = workload_summary(queryset, timezone.now())
        for row in result:
            value = avail_map.get((row.get('email') or '').lower())
            if value is True:
                row['availability_status'] = 'active'
            elif value is False:
                row['availability_status'] = 'stopped'
            else:
                row['availability_status'] = 'unknown'

        return Response({
            'count': len(result),
            'users': result
        })

    @action(detail=True, methods=['post'])
    @_release_assignment_wip_reservation
    def assign(self, request, pk=None):
        """Assign or unassign item to/from a user.

        To assign: POST with assignee_id or assignee_email
        To unassign: POST with assignee_id=null and assignee_email=null (or omit both)

        Assignment grants access to this item only. Project-wide membership is an
        explicit authorization decision managed through the project members API.
        """
        item = self.get_object()
        # The decorator releases this call's token on every early response and
        # exception. A successful Item.save() consumes it first, making cleanup
        # a no-op; callers never wait for TTL after a failed post-reserve path.
        request._pulse_assignment_wip_item = item
        assignee_id = request.data.get('assignee_id')
        assignee_email = request.data.get('assignee_email')
        assignment_group = request.data.get('assignment_group')
        # PLS-859: keep the caller-supplied slug so alias rewrites are never silent.
        requested_assignment_group = (
            str(assignment_group).strip() if assignment_group is not None else None
        )
        direct_assignment_reason = (request.data.get('direct_assignment_reason') or '').strip()
        # PLS-539 (revi #47 P1.1): ONE canonical command resolves id->email and yields
        # the single direct-vs-team precedence, consumed UNCHANGED here as at create +
        # PATCH. A group with NO reason is a TEAM route: the co-supplied assignee is a
        # stale hint (e.g. a client-sent team lead) and is DROPPED so Pulse resolves the
        # live member. Otherwise a concrete assignee is a DIRECT override (gate below).
        _handoff = normalize_direct_handoff(
            assignee_id=assignee_id, assignee_email=assignee_email,
            assignment_group=assignment_group, reason=direct_assignment_reason)
        team_route_requested = _handoff.is_team_route
        # A concrete assignee that is NOT a team route is a DIRECT override; mark the
        # instance so Item.save validates by the assignee's binding to the PERSISTED
        # group (co-supplied team -> exact-team membership; leftover team -> task-org).
        if _handoff.is_direct:
            item._pls539_direct_override = True
            # PLS-539 (hiro/ADR-0010): carry the authorized reason for the direct-
            # handoff crossing receipt Item.save mints (audit only; gated on the flag).
            item._pls539_direct_handoff_reason = _handoff.reason
        if team_route_requested:
            # Team assignment is authoritative; a co-supplied concrete assignee is a
            # stale hint — drop it and resolve the live least-loaded member.
            assignee_id = None
            assignee_email = None
        # Reject a phantom team (e.g. 'cortex') here too — the assign endpoint sets
        # assignment_group directly, bypassing the create/update serializer's
        # validate_assignment_group. A team that routes to no one strands the task.
        from .services.workflow_service import (
            WorkflowService as _WS,
            CAPACITY_UNAVAILABLE as _WS_CAPACITY_UNAVAILABLE,
        )
        if assignment_group:
            assignment_group = _WS._canonical_team_name(assignment_group)
        assignment_group_canonicalized = bool(
            requested_assignment_group
            and assignment_group
            and _WS._canonical_team_name(requested_assignment_group) == assignment_group
            and str(requested_assignment_group).strip().lower().replace('_', '-').replace(' ', '-')
            != str(assignment_group).strip().lower()
        )
        if assignment_group and not _WS._team_registered_for_item(
            assignment_group,
            item,
        ):
            # Name the caller-supplied slug when it differs from the canonical form so
            # an alias rewrite cannot hide which input was rejected (PLS-859).
            offending = requested_assignment_group or assignment_group
            err = (
                f"Team '{offending}' is not registered for this task organization."
            )
            if assignment_group_canonicalized and offending != assignment_group:
                err += (
                    f" (canonicalized from '{offending}' → '{assignment_group}')"
                )
            elif requested_assignment_group and offending != assignment_group:
                err += f" (resolved as '{assignment_group}')"
            return Response(
                {
                    'error': err,
                    'detail': err,
                    'requested_assignment_group': requested_assignment_group,
                    'assignment_group': assignment_group,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        actor_email = (getattr(request.user, 'email', '') or '').lower()

        # PLS-16 operator-assignment guard: agents must NOT assign tasks to
        # the operator directly. The operator is LAST RESORT; only the Chief of
        # Staff (Shizuha) may hand him work after vetting the escalation chain,
        # and the operator may self-assign from the UI. There is intentionally no
        # awaiting_merge bypass — the CEO/operator is out of the routine merge
        # loop, so merge work must follow the review/merge workflow instead of
        # landing directly on his plate.
        target_assignee_email = enforce_operator_assignment_gate(
            actor_email,
            assignee_id,
            assignee_email,
            context=f"custom /assign/ at status='{item.status}'",
        )

        if direct_handoff_reason_denied(
                actor_email, _handoff.assignee_email, _handoff.reason,
                is_direct=_handoff.is_direct):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied({
                'assignee_email': (
                    'Direct person assignment requires direct_assignment_reason. '
                    'Use assignment_group for normal team routing so Pulse can '
                    'choose an available team member.'
                )
            })

        # PLAT-236: block a direct PUSH of new/ready work onto an engineer who is
        # already at the WIP cap (the open-loop push that grew one queue to 144).
        # Fires on EITHER assignee_email OR assignee_id pushes (P2 on PR #26).
        # Exemptions keep in-flight work and operator intent flowing: the CEO,
        # re-asserting the current owner, and tasks already in a working status
        # (an in-flight handoff must always land a concrete owner — orphan-safety
        # outranks the cap; see _WORKING_STATUSES).
        if (assignee_email or assignee_id) and (not CEO_EMAIL or actor_email != CEO_EMAIL):
            try:
                from .services.workflow_service import WorkflowService
                from .user_service import user_service as _us
                tgt = (assignee_email or '').strip().lower()
                tuid = assignee_id if (assignee_id and str(assignee_id).isdigit()) else None
                info = None
                try:
                    if tgt and not tuid:
                        info = _us.get_user_by_email(tgt)
                        tuid = info.get('id') if info else None
                    elif tuid and not tgt:
                        info = _us.get_user_by_id(int(tuid))
                        tgt = (info.get('email') or '').strip().lower() if info else ''
                except Exception:
                    pass
                in_cap_team = tgt and any(
                    tgt in {e.strip().lower() for e in WorkflowService._flat_roster(WorkflowService._pod_roster().get(t, []))}
                    for t in WorkflowService._cap_enforced_teams()
                )
                already_owner = bool(tgt) and (item.assignee_email or '').strip().lower() == tgt
                in_flight = (item.status or '').strip().lower() in WorkflowService._WORKING_STATUSES
                if (not WorkflowService._priority_ignores_wip_cap(item.priority)
                        and in_cap_team and not already_owner and not in_flight and
                        tuid and str(tuid).isdigit() and
                        WorkflowService._agent_at_capacity(int(tuid), user_email=tgt or None)):
                    return Response({
                        'error': (
                            f"{tgt or assignee_id} is at the WIP cap "
                            f"(active cap {WorkflowService._wip_cap()}, in-flight cap {WorkflowService._inflight_cap()}). Direct pushes past the "
                            f"cap are blocked (PLAT-236). Use assignment_group for team routing — "
                            f"the task queues group-only and is pulled when capacity frees — or "
                            f"pick an under-cap member."
                        ),
                        'active_cap': WorkflowService._wip_cap(),
                        'inflight_cap': WorkflowService._inflight_cap(),
                    }, status=status.HTTP_400_BAD_REQUEST)
            except Exception:
                logger.exception("PLAT-236 direct-assign cap check failed (non-fatal, allowing assign)")

        # PLS-509: roster-configured teams use the same atomic reservation for
        # direct API assignment as resolver/post-function paths.  No in-flight
        # bypass: review/merge seats are WIP too.
        if (assignee_email or assignee_id) and (not CEO_EMAIL or actor_email != CEO_EMAIL):
            from .user_service import user_service as _us
            tgt = (assignee_email or '').strip().lower()
            tuid = assignee_id if assignee_id and str(assignee_id).isdigit() else None
            if tgt and not tuid:
                info = _us.get_user_by_email(tgt)
                tuid = info.get('id') if info else None
            elif tuid and not tgt:
                info = _us.get_user_by_id(int(tuid))
                tgt = (info.get('email') or '').strip().lower() if info else ''
            if assignment_group:
                item.assignment_group = assignment_group
            # PLS-720: the reservation must target the assignment_group that will
            # ACTUALLY persist at save(). A DIRECT override with NO co-supplied
            # team clears the stale group before Item.save() (PLS-539 revi #313),
            # so reserving against the pre-clear persisted group below mints a
            # token that _verify_wip_reservation then rejects as
            # wip_reservation_target_mismatch (team='' vs reservation=engineering)
            # — the live 409 while the group path seats the identical person.
            # Compute the effective post-clear group and hand it as the override
            # so mint and verify agree. A no-team direct override therefore skips
            # the per-team roster reservation (the PLAT-236 preflight above still
            # applies the org/roster WIP cap for protected teams) and lands the
            # seat exactly like the group path would.
            if (getattr(item, '_pls539_direct_override', False)
                    and not assignment_group):
                _pls720_rsv_group = None
            else:
                _pls720_rsv_group = assignment_group or item.assignment_group
            if tgt and tuid and _WS._wip_roster_config(
                _pls720_rsv_group, item,
            ) is not None:
                admitted = _WS._reserve_wip_slot(
                    item, int(tuid), tgt,
                    assignment_group_override=_pls720_rsv_group,
                )
                if admitted == _WS_CAPACITY_UNAVAILABLE:
                    denial = {
                        'error': 'capacity_unavailable',
                        'message': (
                            'Capacity classification is temporarily unavailable; '
                            'assignment held rather than admitted under an unknown WIP band.'
                        ),
                    }
                    denial.update(dict(
                        getattr(item, '_wip_capacity_diagnostics', None) or {}
                    ))
                    return Response(denial, status=status.HTTP_503_SERVICE_UNAVAILABLE)
                if admitted is False:
                    denial = {
                        'error': 'wip_capacity_denied',
                        'message': (
                            f'{tgt} is at the configured WIP cap; route to the '
                            'team queue instead of assigning directly.'
                        ),
                    }
                    denial.update(dict(
                        getattr(item, '_wip_capacity_diagnostics', None) or {}
                    ))
                    return Response(denial, status=status.HTTP_409_CONFLICT)

        # Team-only assignment: resolve the team to the current available owner.
        # This keeps the task team-routed while still visible in the per-agent
        # heartbeat queue.
        if team_route_requested:
            team_name = _WS._canonical_team_name((assignment_group or '').strip())
            assignment_group = team_name
            if team_name:
                from .services.routing_authority import (
                    RoutingAuthorityError,
                    clear_assignment_to_group_queue,
                    reroute_assignment_to_group_queue,
                )

                # PLS-492: owner shape does not determine routing authority.
                # Clearing a concrete owner while project/group/workflow stay
                # identical is an in-lane CLEAR; REROUTE is reserved for an
                # actual routing-context change.  Capture this once before any
                # mutation so a failed command is never retried under a
                # different interpretation.
                current_routing_context = (
                    item.project_id,
                    _WS._canonical_team_name((item.assignment_group or '').strip()),
                    (item.workflow_name or '').strip(),
                )
                prior_assignment_group = current_routing_context[1]
                def queue_group_only():
                    # ``team_name`` is the effective target, and may change when
                    # security review falls back to the independent review team.
                    # Decide at mutation time so a fallback can never use a stale
                    # same-lane decision captured for the originally requested team.
                    effective_routing_context = (
                        item.project_id,
                        _WS._canonical_team_name((team_name or '').strip()),
                        (item.workflow_name or '').strip(),
                    )
                    if current_routing_context == effective_routing_context:
                        return clear_assignment_to_group_queue(
                            item, reason='no_candidate', save=False,
                        )
                    return reroute_assignment_to_group_queue(
                        item, team_name, reason='no_candidate', save=False,
                    )

                def routing_error_response(exc):
                    return Response({
                        'error': exc.detail,
                        'code': exc.code,
                        'assignment_group': team_name,
                    }, status=status.HTTP_409_CONFLICT)

                try:
                    from .services.workflow_service import WorkflowService
                    # PLS-509: team routing claims the applicable ready/active
                    # slot before selecting an owner. A full active lane remains
                    # group-queued instead of pushing past the configured cap.
                    uid, email = WorkflowService._resolve_team_assignee(
                        team_name,
                        exclude_email=item.created_by_email,
                        enforce_cap=True,
                        item=item,
                        assignment_group_override=team_name,
                    )
                    _wip_configured = WorkflowService._wip_roster_config(team_name, item) is not None
                    if not email and (_wip_configured or WorkflowService._cap_applies(team_name)):
                        u2, e2 = WorkflowService._resolve_team_assignee(
                            team_name, exclude_email=item.created_by_email,
                            enforce_cap=False, item=item)
                        if e2:
                            review_hold = WorkflowService._is_exact_role_assignment_hold(
                                item, team_name,
                            )
                            preserve_same_role_owner = (
                                review_hold
                                and WorkflowService._has_valid_current_exact_role_owner(
                                    item, team_name,
                                    prior_assignment_group=prior_assignment_group,
                                )
                            )
                            if preserve_same_role_owner:
                                item.assignment_group = team_name
                                item.save(update_fields=['assignment_group', 'updated_at'])
                                return Response({
                                    'message': (
                                        f"All '{team_name}' members are at the WIP cap; "
                                        'the existing policy-valid same-role owner was preserved.'
                                    ),
                                    'item_id': item.id,
                                    'item_key': item.item_key,
                                    'title': item.title,
                                    'assignee_id': item.assignee_id,
                                    'assignee_email': item.assignee_email,
                                    'assignee': item.assignee_email,
                                    'assignment_group': team_name,
                                    'queued_at_capacity': True,
                                    'preserved_same_role_owner': True,
                                })
                            # Team exists but is at capacity: queue (group-only).
                            queue_group_only()
                            item.save(update_fields=['assignee_id', 'assignee_email', 'assignment_group'])
                            if WorkflowService._is_review_seat_starvation(
                                    item, team_name):
                                WorkflowService._record_review_seat_starvation(
                                    item,
                                    team_name,
                                    required_caps=WorkflowService._task_required_capabilities(item),
                                    reasons=['over_capacity'],
                                    source='/assign capacity queue',
                                )
                            else:
                                Comment.objects.create(
                                    item=item, author_id=0, author_email='system@pulse',
                                    content=(f"Queued in the **{team_name}** capacity queue — all team members "
                                             f"are at the WIP cap. Pulled "
                                             f"automatically when a slot frees. [PLAT-236]"))
                            return Response({
                                'message': (f"All '{team_name}' members are at the WIP cap; "
                                            f"task queued group-only and "
                                            f"will be pulled when capacity frees (PLAT-236)"),
                                'item_id': item.id,
                                'item_key': item.item_key,
                                'title': item.title,
                                'assignee_id': None,
                                'assignee_email': None,
                                'assignee': None,
                                'assignment_group': team_name,
                                'queued_at_capacity': True,
                            })
                except RoutingAuthorityError as e:
                    return routing_error_response(e)
                except Exception:
                    logger.exception(
                        "Could not resolve assignment_group=%r for item %s",
                        team_name, item.id,
                    )
                    return Response({
                        'error': 'team_assignment_resolution_failed',
                        'assignment_group': team_name,
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                if not email:
                    finding = WorkflowService._maybe_file_zero_member_team_alert(
                        item, team_name, trigger='/assign',
                    )
                    # PLAT-4178: a KNOWN team must NEVER 400 on assign. When the team
                    # resolves to no concrete member — whether all members are at the
                    # WIP cap, all are currently UNAVAILABLE (offline/disabled), or the
                    # team is genuinely zero-member — accept the task GROUP-ONLY so it
                    # stays team-routed and is pulled when a member frees (rebalance
                    # sweep / completion hook), mirroring the at-capacity queue path
                    # above and the automated resolver's group-queue behavior (PLS-278).
                    # The previous 400 fell through only for the "has members but none
                    # currently available" case (no zero-member alert filed, cap-queue
                    # e2 also empty) and left bug tasks unrouted in Needs Triage,
                    # invisible to team queues. Unknown/phantom teams still 400 above
                    # via _is_known_team.
                    #
                    # EXCEPTION: an in-flight (working-status) task must keep a concrete
                    # owner (orphan-safety invariant — a group-only working task is
                    # invisible to the per-agent heartbeat and orphans in review). We
                    # cannot hand it to a team with no available member, so preserve its
                    # current owner and report 409 rather than group-orphaning it.
                    _in_flight = (item.status or '').strip().lower() in WorkflowService._WORKING_STATUSES
                    review_hold = WorkflowService._is_exact_role_assignment_hold(
                        item, team_name,
                    )
                    preserve_same_role_owner = (
                        review_hold
                        and WorkflowService._has_valid_current_exact_role_owner(
                            item, team_name,
                            prior_assignment_group=prior_assignment_group,
                        )
                    )
                    if not finding and _in_flight and not review_hold:
                        return Response(
                            {'error': (
                                f"No available member could be resolved for team '{team_name}' and this "
                                f"task is in-flight (status '{item.status}'); its current owner is preserved "
                                f"rather than group-orphaning an active task. Retry when the team has an "
                                f"available member, or hand off to a specific member with "
                                f"direct_assignment_reason."
                            )},
                            status=status.HTTP_409_CONFLICT,
                        )
                    if preserve_same_role_owner:
                        item.assignment_group = team_name
                        item.save(update_fields=['assignment_group', 'updated_at'])
                        return Response({
                            'message': (
                                f"No new member of team '{team_name}' is currently available; "
                                'the existing policy-valid same-role owner was preserved.'
                            ),
                            'item_id': item.id,
                            'item_key': item.item_key,
                            'title': item.title,
                            'assignee_id': item.assignee_id,
                            'assignee_email': item.assignee_email,
                            'assignee': item.assignee_email,
                            'assignment_group': team_name,
                            'preserved_same_role_owner': True,
                        })
                    try:
                        queue_group_only()
                    except RoutingAuthorityError as e:
                        return routing_error_response(e)
                    item.save(update_fields=['assignee_id', 'assignee_email', 'assignment_group', 'updated_at'])
                    if review_hold:
                        WorkflowService._record_review_seat_starvation(
                            item,
                            team_name,
                            required_caps=WorkflowService._task_required_capabilities(item),
                            reasons=['no_compatible_seat'],
                            source='/assign no candidate',
                        )
                    if finding:
                        message = (
                            f"No member could be resolved for team '{team_name}'; "
                            f"task accepted group-only and zero-member alert filed/confirmed "
                            f"({getattr(finding, 'item_key', None) or getattr(finding, 'id', 'n/a')})."
                        )
                    else:
                        message = (
                            f"No member of team '{team_name}' is currently available; task accepted "
                            f"group-only and will be pulled when a member frees (PLAT-4178)."
                        )
                    return Response({
                        'message': message,
                        'item_id': item.id,
                        'item_key': item.item_key,
                        'title': item.title,
                        'assignee_id': None,
                        'assignee_email': None,
                        'assignee': None,
                        'assignment_group': team_name,
                        'zero_member_team_alert': bool(finding),
                        'queued_no_available_member': not bool(finding),
                    })
                if not WorkflowService._email_in_current_team_active_tier(team_name, email, item=item):
                    return Response({
                        'error': (
                            f"Resolved assignee {email} is not in the current active roster tier "
                            f"for team '{team_name}'; refusing stale team assignment."
                        ),
                        'item_id': item.id,
                        'item_key': item.item_key,
                        'title': item.title,
                        'assignee_email': email,
                        'assignment_group': team_name,
                        'queued_at_capacity': False,
                    }, status=status.HTTP_409_CONFLICT)
                assignee_id = uid
                assignee_email = email
                # PLS-16 (#4): a team route can resolve to the operator
                # (wrongly rostered personal-org review lane, governance-SoD
                # return, human-ops lead). Operator-capable lanes may keep him.
                # Every other team — including when the actor IS the operator
                # (coordinator MCP authenticates as CEO) — group-queues rather
                # than seating HO or dumping onto CoS.
                from .services.workflow_service import WorkflowService as _WS
                if (_WS._is_operator_target(assignee_id, assignee_email)
                        and not _WS._is_operator_capable_team(team_name)):
                    try:
                        queue_group_only()
                    except RoutingAuthorityError as e:
                        return routing_error_response(e)
                    item.save(update_fields=[
                        'assignee_id', 'assignee_email', 'assignment_group',
                        'updated_at',
                    ])
                    logger.warning(
                        "PLS-16: /assign/ team %r resolved to the operator — "
                        "held group-queued (operator is not a %s owner)",
                        team_name, team_name,
                    )
                    return Response({
                        'message': (
                            f"Team '{team_name}' must not resolve to the operator; "
                            f"task accepted group-only and will be pulled when a "
                            f"member is available."
                        ),
                        'item_id': item.id,
                        'item_key': item.item_key,
                        'title': item.title,
                        'assignee_id': None,
                        'assignee_email': None,
                        'assignee': None,
                        'assignment_group': team_name,
                        'queued_no_available_member': True,
                    })

                # PLS-644: a materialized exact-authority team route needs the
                # same destination-tuple decision that workflow assign_team uses.
                # The resolver selected the subject against the destination
                # group while the Item still carries its source group; retain
                # that reviewed decision for the final Item.save receipt fence.
                from .services.routing_authority import (
                    binding_candidates_for_item,
                    subject_authorized_for_item,
                )
                if binding_candidates_for_item(team_name, item) is not None:
                    routing_decision = subject_authorized_for_item(
                        assignee_id,
                        item,
                        team_name=team_name,
                        expected_assignment_group=team_name,
                    )
                    if not routing_decision.allowed:
                        return Response({
                            'error': 'routing_authority_denied',
                            'code': routing_decision.reason,
                            'assignment_group': team_name,
                        }, status=status.HTTP_409_CONFLICT)
                    item._routing_authority_decision = routing_decision

        # Handle unassignment: both null/empty means clear assignment
        if not assignee_id and not assignee_email:
            previous_assignee = item.assignee_email
            from .services.routing_authority import (
                unassign_assignment,
                clear_assignment_to_group_queue,
            )
            _persisted_group = _WS._canonical_team_name(
                (item.assignment_group or '').strip())
            # PLAT-4572 contract (test_public_unassign_clears_guarded_lane):
            # a bare POST on an item WITH a concrete owner is the explicit
            # unassign command — exact OLD tuple -> no routing group. The
            # PLS-1339 retain-path applies only when there is NO owner to
            # remove: unassigning an ownerless group-routed row is a no-op
            # probe (mika's PLAT-9646 carrier), and stripping the group there
            # silently unrouted the carrier behind an empty 200.
            if _persisted_group and not previous_assignee:
                # PLS-1339 (re-break of PLS-487): a bare/unchanged-context
                # assign on an already-team-routed item is an in-lane CLEAR,
                # not an unroute. The prior code fell through to
                # unassign_assignment (exact OLD tuple -> NO routing group),
                # silently nulling assignment_group and returning an empty
                # 200 — the carrier fell off every roster while still flagged
                # actionable. Retain the group: clear the owner to the
                # group-only queue (identical shape to the explicit-reroute
                # path that already behaves correctly), and answer with a
                # structured body naming the retained lane.
                clear_assignment_to_group_queue(
                    item, reason='no_candidate', save=False)
                _clear_direct_assignment_pin(item, save=False)
                item.save()
                logger.info(
                    f"Item {item.id} bare assign with unchanged context: owner "
                    f"cleared to '{_persisted_group}' group queue (PLS-1339; "
                    f"was: {previous_assignee})")
                return Response({
                    'message': (
                        f"Owner cleared to the '{_persisted_group}' group "
                        'queue (unchanged routing context retains the lane; '
                        'PLS-1339). Pulled automatically when a slot frees.'
                    ),
                    'item_id': item.id,
                    'item_key': item.item_key,
                    'assignee': None,
                    'assignee_email': None,
                    'assignment_group': _persisted_group,
                    'queued_group_only': True,
                })
            unassign_assignment(item, save=False)
            _clear_direct_assignment_pin(item, save=False)
            item.save()

            logger.info(f"Item {item.id} unassigned (was: {previous_assignee})")

            return Response({
                'message': 'Item unassigned successfully',
                'item_id': item.id,
                'assignee': None,
                'assignment_group': None,
            })

        # Try to resolve user info (for display name, etc.).
        # AT-97: belt-and-suspenders try/except so a programming-error / non-
        # RequestException from user_service can't 500 the assign. The service
        # already catches RequestException and returns None; this catches
        # everything else, falling through to the email-only path below.
        try:
            user_info = user_service.resolve_user_info(user_id=assignee_id, email=assignee_email)
        except Exception as e:
            logger.warning(
                f"user_service.resolve_user_info(user_id={assignee_id}, email={assignee_email}) "
                f"raised unexpectedly ({type(e).__name__}: {e}); "
                f"falling back to email-only assignment"
            )
            user_info = None

        # If we couldn't find the user but have an email, accept the assignment anyway
        # This allows assigning to users that exist in shizuha-id but haven't been seen locally
        if not user_info and assignee_email:
            # Check if it's an agent email
            is_agent = assignee_email.endswith('@agents.shizuha.io')
            username = assignee_email.split('@')[0]
            user_info = {
                'id': None,
                'email': assignee_email,
                'username': username,
                'full_name': username.replace('-', ' ').replace('_', ' ').title(),
                'is_agent': is_agent,
            }
            logger.info(f"User not found in directory, accepting email: {assignee_email}")
        elif not user_info:
            return Response({'error': 'Either assignee_id or assignee_email is required'}, status=status.HTTP_400_BAD_REQUEST)

        # PLS-381: resolve_user_info can return a truthy dict that LACKS an 'email'
        # key (a partial shizuha-id directory record for the assignee). The
        # downstream project-auto-add block reads user_info['email'] directly and
        # would KeyError into a bare 500. Backfill the email from the caller-
        # supplied assignee_email when the resolved record omits it, so the
        # assignment succeeds and the auto-add keeps working.
        if user_info is not None and not user_info.get('email') and assignee_email:
            user_info['email'] = assignee_email

        is_agent = user_info.get('is_agent', False)

        # PLAT-4213 single-team/single-org guard. The shared service owns the
        # workflow-contract and platform-lane exemptions so every assignment
        # path applies one policy instead of duplicating status lists here.
        _guard_email = (user_info.get('email') or assignee_email or '').strip().lower()
        if not _guard_email:
            return Response({
                'error': (
                    'Assignment requires a canonical assignee email; the identity '
                    'record was incomplete and no caller email was supplied.'
                ),
                'item_key': item.item_key,
            }, status=status.HTTP_400_BAD_REQUEST)
        from .services.workflow_service import WorkflowService as _WSG
        routing_decision = getattr(item, '_routing_authority_decision', None)
        if (
            team_route_requested
            and routing_decision is not None
            and routing_decision.allowed
            and routing_decision.subject_id == user_info.get('id')
            and _WSG._canonical_team_name(
                routing_decision.assignment_group or ''
            ) == _WSG._canonical_team_name(assignment_group or '')
        ):
            _mismatch = False
            _roster_org = _task_org = _WSG.org_slug_for_project(item.project)
        else:
            _mismatch, _roster_org, _task_org = _WSG.assignment_org_mismatch(
                item, _guard_email, assignment_group,
                user_id=user_info.get('id'),
            )
        if _mismatch:
            return Response({
                'error': (
                    f'Organization/team assignment blocked (PLAT-4213): '
                    + _WSG.assignment_denial_reason(_guard_email, _roster_org, _task_org)
                ),
                'item_key': item.item_key,
            }, status=status.HTTP_400_BAD_REQUEST)

        # PLAT-4871/PLS-539: capture the owner BEFORE item.assign() mutates it, so the
        # shared direct-handoff recorder can release an obsolete pin iff the owner
        # actually changed on a non-direct assignment.
        _pls539_assign_prev_assignee = (item.assignee_email or '').strip().lower()
        assigned_ok = item.assign(user_info, save=False)
        if not assigned_ok:
            return Response({
                'error': 'Resolved assignee could not be persisted; assignment left unchanged',
                'item_id': item.id,
                'item_key': item.item_key,
                'title': item.title,
                'assignee_email': assignee_email,
                'assignment_group': assignment_group,
                'queued_at_capacity': False,
            }, status=status.HTTP_409_CONFLICT)

        # Set assignment_group if provided (team-based assignment)
        if assignment_group is not None:
            item.assignment_group = assignment_group or None
        elif getattr(item, '_pls539_direct_override', False):
            # PLS-539 (P1, revi #313): a DIRECT override with NO requested team must
            # not leave the prior team's stale assignment_group persisted — that
            # would re-validate the direct owner through that team on the next write
            # and strand the exact-seat handoff. Clear it atomically with the direct
            # assignment. A co-supplied group is retained (matches create): the caller
            # explicitly asked for it and the direct owner is expected to belong to it.
            item.assignment_group = None
        if (team_route_requested and item.assignee_email
                and not WorkflowService._email_in_current_team_active_tier(
                    item.assignment_group, item.assignee_email, item=item,
                )):
            return Response({
                'error': (
                    f"Persisted assignee {item.assignee_email} is not in the current active roster tier "
                    f"for team '{item.assignment_group}'; refusing stale/no-op team assignment."
                ),
                'item_id': item.id,
                'item_key': item.item_key,
                'title': item.title,
                'assignee_email': item.assignee_email,
                'assignment_group': item.assignment_group,
                'queued_at_capacity': False,
            }, status=status.HTTP_409_CONFLICT)
        # PLAT-4871/PLS-539: persist the assignee + audit ATOMICALLY through the ONE
        # shared direct-handoff contract. Previously the direct comment + pin (and the
        # pin's own metadata save) were written BEFORE item.save(), so a failing
        # assignee save left a persisted comment/pin for an owner that never saved.
        _assign_actor_id = getattr(request.user, 'id', 0) or 0
        _assign_actor_email = getattr(request.user, 'email', '') or 'system@pulse'
        try:
            with transaction.atomic():
                item.save()
                record_direct_handoff(
                    item,
                    assignee_email=_handoff.assignee_email,
                    reason=_handoff.reason,
                    actor_id=_assign_actor_id,
                    actor_email=_assign_actor_email,
                    is_direct=_handoff.is_direct,
                    context='assignment',
                    prev_assignee=_pls539_assign_prev_assignee,
                )
        except DjangoValidationError as exc:
            # Expected model/admission denials are a typed conflict, never a
            # generic 500. The endpoint decorator releases this request's exact
            # unconsumed WIP reservation before returning, so retry is immediate.
            # PLS-859: always emit a human ``detail`` that names every failing
            # field/constraint. MCP historically only surfaced ``error`` /
            # ``detail`` and dropped the structured ``details`` map, so agents
            # saw bare ``assignment_validation_failed`` with no constraint.
            details = _normalize_validation_error_details(exc)
            detail_text = _format_assignment_validation_detail(details)
            return Response({
                'error': 'assignment_validation_failed',
                'detail': detail_text,
                'details': details,
                'item_id': item.id,
                'item_key': item.item_key,
                'assignment_group': item.assignment_group,
                'requested_assignment_group': requested_assignment_group,
            }, status=status.HTTP_409_CONFLICT)

        # Notify the assignee — fires for both humans (push/telegram/discord)
        # and agents (Connect DM via notify_assignment_changed).
        # The legacy `send_task_notification` path is kept for human users; the
        # newer `notify_assignment_changed` path is what actually delivers to
        # agents via shizuha-connect.
        try:
            from .services.notifications import notification_service
            actor = getattr(request.user, 'email', str(getattr(request.user, 'id', '')))
            notification_service.notify_assignment_changed(
                item,
                assigned_by=actor,
                exclude_user_id=getattr(request.user, 'id', None),
            )
        except Exception as e:
            logger.error(f"Failed to send assignment notification: {e}")

        # Keep the legacy user-notification path for human users (push/email).
        if not is_agent and item.assignee_id:
            try:
                item_data = ItemSerializer(item).data
                user_service.send_task_notification(item_data)
            except Exception as e:
                logger.error(f"Failed to send task notification: {e}")

        response_data = {
            'message': 'Item assigned successfully',
            'item_id': item.id,
            'item_key': item.item_key,
            'title': item.title,
            'assignee_id': item.assignee_id,
            'assignee_email': item.assignee_email,
            'assignment_group': item.assignment_group,
            'queued_at_capacity': False,
            'assignee': {
                'id': item.assignee_id,
                'email': item.assignee_email,
                'name': user_info.get('full_name', user_info.get('username')),
                'is_agent': is_agent
            }
        }
        # PLS-859: never silently rewrite a team slug. When the caller asked for
        # an alias (e.g. trading-engineering → engineering), echo both forms so
        # MCP/clients can report the rewrite instead of looking like a fallback
        # to the caller's default team.
        if requested_assignment_group is not None:
            response_data['requested_assignment_group'] = requested_assignment_group
        if assignment_group_canonicalized:
            response_data['assignment_group_canonicalized'] = True
            response_data['message'] = (
                f"Item assigned successfully "
                f"(assignment_group canonicalized from "
                f"'{requested_assignment_group}' → '{assignment_group}')"
            )

        return Response(response_data)

    @action(detail=False, methods=['get'], url_path='by-key/(?P<item_key>[A-Za-z0-9]+-[0-9]+)')
    def by_key(self, request, item_key=None):
        """
        Get item by item_key (e.g., PROJ-123).
        This enables dedicated task detail pages with URL-based keys.
        """
        from django.shortcuts import get_object_or_404
        item = get_object_or_404(Item, item_key__iexact=item_key)
        self.check_object_permissions(request, item)
        serializer = ItemSerializer(item)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def activity(self, request, pk=None):
        """Get activity history for an item."""
        item = self.get_object()
        activities = ItemActivity.objects.filter(item=item).order_by('-created_at')

        # Optional limit
        limit = request.query_params.get('limit')
        if limit:
            try:
                activities = activities[:int(limit)]
            except ValueError:
                pass

        serializer = ItemActivitySerializer(activities, many=True)
        return Response({
            'count': len(serializer.data),
            'results': serializer.data,   # standard key for get_all_pages / pagination callers
            'activities': serializer.data, # kept for frontend compat (ActivityTimeline.jsx)
        })

    @action(detail=False, methods=['get'], url_path='activity-feed')
    def activity_feed(self, request):
        """HIVE-603: recent activity across ALL items the caller can see, one
        merged newest-first stream (item activity + comments). Feeds the
        shizuha.com home live theater. Visibility = exactly this viewset's
        queryset (organization param honored, project permissions applied), so
        the feed can never widen scope beyond the item list itself.

        Params: limit (<=100, default 50), since (ISO datetime — only newer
        events; lets pollers fetch deltas cheaply), actor (email — only that
        actor's events; powers the home cockpit's per-agent activity peek).
        """
        from django.utils.dateparse import parse_datetime

        try:
            limit = min(max(int(request.query_params.get('limit', '50')), 1), 100)
        except ValueError:
            limit = 50
        since = None
        raw_since = (request.query_params.get('since') or '').strip()
        if raw_since:
            since = parse_datetime(raw_since)
        actor = (request.query_params.get('actor') or '').strip().lower()

        item_ids = self.get_queryset().filter(mode='task').order_by().values('id')
        # 'commented' activity rows duplicate the Comment stream below (which
        # carries the excerpt); 'updated' is field-level noise for a feed.
        acts = (
            ItemActivity.objects
            .filter(item_id__in=item_ids)
            .exclude(action__in=['commented', 'updated'])
            .select_related('item')
            .order_by('-created_at')
        )
        comments = (
            Comment.objects
            .filter(item_id__in=item_ids)
            .select_related('item')
            .order_by('-created_at')
        )
        if since:
            acts = acts.filter(created_at__gt=since)
            comments = comments.filter(created_at__gt=since)
        if actor:
            acts = acts.filter(user_email__iexact=actor)
            comments = comments.filter(author_email__iexact=actor)

        events = []
        for a in acts[:limit]:
            events.append({
                'type': a.action,
                'at': a.created_at.isoformat(),
                'actor_email': a.user_email or '',
                'item_key': a.item.item_key or f'#{a.item_id}',
                'item_title': a.item.title or '',
                'project': (a.item.item_key or '').rsplit('-', 1)[0],
                'field': a.field_name or '',
                'old': str(a.old_value or '')[:80],
                'new': str(a.new_value or '')[:80],
            })
        for c in comments[:limit]:
            events.append({
                'type': 'comment',
                'at': c.created_at.isoformat(),
                'actor_email': c.author_email or '',
                'item_key': c.item.item_key or f'#{c.item_id}',
                'item_title': c.item.title or '',
                'project': (c.item.item_key or '').rsplit('-', 1)[0],
                'excerpt': (c.content or '')[:220],
            })
        events.sort(key=lambda e: e['at'], reverse=True)
        events = events[:limit]
        return Response({
            'count': len(events),
            'results': events,
            'server_time': timezone.now().isoformat(),
        })

    @action(detail=True, methods=['get', 'post'])
    def links(self, request, pk=None):
        """Get or create links for an item."""
        item = self.get_object()

        if request.method == 'GET':
            # Get both outward and inward links
            outward = ItemLink.objects.filter(source_item=item).select_related('target_item')
            inward = ItemLink.objects.filter(target_item=item).select_related('source_item')

            return Response({
                'outward_links': ItemLinkSerializer(outward, many=True).data,
                'inward_links': ItemLinkSerializer(inward, many=True).data,
            })

        elif request.method == 'POST':
            serializer = ItemLinkCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            # Resolve target item
            target_item_id = serializer.validated_data.get('target_item_id')
            target_item_key = serializer.validated_data.get('target_item_key')

            if target_item_key:
                target = Item.objects.get(item_key__iexact=target_item_key)
            else:
                target = Item.objects.get(id=target_item_id)

            link_type = serializer.validated_data['link_type']

            # Check for duplicate
            if ItemLink.objects.filter(source_item=item, target_item=target, link_type=link_type).exists():
                return Response(
                    {'error': 'This link already exists'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # PLS-434 (operator 2026-07-11): reject a block link that would create a
            # dependency CYCLE (A blocks B … blocks A). A block ring is a permanent
            # deadlock — every task waits on another in the ring, none can auto-resume.
            from .services.workflow_service import WorkflowService
            if link_type in ('blocks', 'is_blocked_by', 'blocked_by'):
                if link_type == 'blocks':
                    blocker_id, blocked_id = item.id, target.id
                else:  # is_blocked_by / blocked_by: item is blocked BY target → target blocks item
                    blocker_id, blocked_id = target.id, item.id
                if WorkflowService.would_create_block_cycle(blocker_id, blocked_id):
                    return Response(
                        {'error': (
                            f"Refusing to create this block link: it would form a circular block "
                            f"ring ({item.item_key} ↔ {target.item_key} via existing dependencies), "
                            f"a permanent deadlock in which neither task can ever auto-resume. "
                            f"Resolve the real dependency direction instead — only ONE task can block "
                            f"the other. If both genuinely need each other, they are the same unit of "
                            f"work: merge them or split out the shared blocker."
                        )},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Keep model-level tenancy validation as the source of truth while
            # presenting invariant violations as a caller error, not a server fault.
            try:
                link = ItemLink.objects.create(
                    source_item=item,
                    target_item=target,
                    link_type=link_type,
                    created_by_id=request.user.id,
                    created_by_email=request.user.email,
                )
            except DjangoValidationError as exc:
                message = exc.messages[0] if exc.messages else str(exc)
                return Response(
                    {'error': message},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response(ItemLinkSerializer(link).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path='links/(?P<link_id>[^/.]+)')
    def delete_link(self, request, pk=None, link_id=None):
        """Delete a link from an item."""
        # PLS-498 (revi P1): a blocker-edge deletion mutates the DEPENDENT task's
        # block-state, so authorization must be checked against the normalized
        # DEPENDENT, not the URL item. Otherwise a caller authorized only on the
        # blocker could DELETE /items/<blocker>/links/<id> and remove an edge
        # owned by a dependent they cannot mutate (inverse blocks edge). Resolve
        # the URL item WITHOUT authorizing it here; authorize the correct object
        # after direction normalization below — so a dependent owner/assignee (or
        # privileged architect/admin) succeeds from EITHER addressing direction
        # and a blocker-only caller gets 403 with the edge intact.
        from rest_framework.exceptions import NotFound
        _pls498_lookup = self.kwargs.get(self.lookup_field, '')
        item = _resolve_item_identifier(_pls498_lookup, mode=None)
        if item is None:
            raise NotFound(_format_item_identifier_error(_pls498_lookup))

        try:
            link = ItemLink.objects.get(id=link_id, source_item=item)
        except ItemLink.DoesNotExist:
            # Also check inward links for deletion
            try:
                link = ItemLink.objects.get(id=link_id, target_item=item)
            except ItemLink.DoesNotExist:
                return Response({'error': 'Link not found'}, status=status.HTTP_404_NOT_FOUND)

        # PLS-498: removing a stale is_blocked_by/blocks edge must not trip the
        # PLAT-4215 deferred trigger (blocked_item_requires_active_assigned_blocker)
        # when it clears the dependent task's LAST active blocker. The prior bare
        # link.delete() left a `blocked` row with no active blocker, so the
        # constraint fired at COMMIT and surfaced (middleware.py) as HTTP 409 —
        # the reported regression of PLS-80's stale-link removal contract. Delete
        # + recompute + resume ATOMICALLY so the constraint sees a non-blocked
        # row at commit, reusing the exact machinery a terminal blocker triggers
        # (_active_blockers_for / _restore_blocked_item). Resume only when no
        # other active blocker remains; otherwise the dependent stays blocked.
        from django.db import transaction as _pls498_txn
        from .services.workflow_service import (
            WorkflowService as _PLS498_WFS,
            WorkflowValidationError,
            SecurityGateLinkProtectedError as _PLS458_GateErr,
        )
        if link.link_type in ('is_blocked_by', 'blocked_by'):
            _pls498_dependent_id = link.source_item_id
        elif link.link_type == 'blocks':
            _pls498_dependent_id = link.target_item_id
        else:
            _pls498_dependent_id = None

        # PLS-498 (revi P1): authorize the operation against the object it
        # actually mutates — the DEPENDENT for a blocker edge, else the URL item
        # for a generic link (unchanged behavior). check_object_permissions runs
        # the same ItemProjectPermission (staff / org-admin / assignee-reporter
        # participant / project-member) and raises 403 with the edge intact when
        # the caller lacks authority over the dependent.
        if _pls498_dependent_id is not None:
            try:
                _pls498_authz_obj = Item.objects.get(pk=_pls498_dependent_id)
            except Item.DoesNotExist:
                _pls498_authz_obj = item
        else:
            _pls498_authz_obj = item
        self.check_object_permissions(request, _pls498_authz_obj)

        # PLS-498 audit attribution: the ItemLink post_delete signal records the
        # link-removed TaskEvent without request context. Stamp the actor on the
        # instance so the deletion is attributed to this caller (not anonymous).
        link._actor_user_id = request.user.id

        try:
            with _pls498_txn.atomic():
                # PLS-458 (review P1 #2): enforce the SecurityGateState guard on
                # THIS deletion path too — inside the same transaction, gate rows
                # locked FIRST — so a blocker-edge delete cannot bypass the STRIDE
                # gate topology. (ItemLinkViewSet.destroy uses delete_link_guarded,
                # which calls the same helper.)
                _PLS498_WFS._assert_link_not_gate_protected(link)
                if _pls498_dependent_id is not None:
                    # P1 (exclusivity): lock the dependent row FIRST, so two concurrent
                    # removals of different blocker edges serialize on it. The second
                    # waiter recomputes only after the first commits, so it observes the
                    # first edge already gone and correctly resumes when no active blocker
                    # remains — never committing a `blocked` row with zero active blockers
                    # (which the deferred PLAT-4215 trigger rejects as HTTP 409).
                    dependent = Item.objects.select_for_update().get(
                        pk=_pls498_dependent_id)
                    link.delete()
                    if (dependent.status == 'blocked'
                            and not _PLS498_WFS._active_blockers_for(dependent)):
                        # PLS-953: False is a rejected restore just like an exception.
                        # Never commit an edge deletion unless the dependent actually
                        # left blocked; the deferred PLAT-4215 trigger remains the
                        # final fail-closed backstop rather than the user-facing error.
                        try:
                            restored = _PLS498_WFS._restore_blocked_item(
                                dependent,
                                user=request.user,
                            )
                        except Exception as exc:
                            raise WorkflowValidationError(
                                "Dependency release reconciliation rejected: could not "
                                f"restore blocked dependent {dependent.item_key or dependent.pk}.",
                                code='dependency_release_rejected',
                                details={
                                    'dependent_item_key': str(
                                        dependent.item_key or dependent.pk
                                    ),
                                    'link_id': str(link_id),
                                },
                            ) from exc
                        if not restored:
                            raise WorkflowValidationError(
                                "Dependency release reconciliation rejected: restore "
                                "returned false for blocked dependent "
                                f"{dependent.item_key or dependent.pk}.",
                                code='dependency_release_rejected',
                                details={
                                    'dependent_item_key': str(
                                        dependent.item_key or dependent.pk
                                    ),
                                    'link_id': str(link_id),
                                },
                            )
                else:
                    link.delete()
        except _PLS458_GateErr as e:
            return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)
        except WorkflowValidationError as exc:
            payload = {
                'error': exc.code or 'dependency_release_rejected',
                'code': exc.code or 'dependency_release_rejected',
                'detail': str(exc),
            }
            if exc.details:
                payload.update(exc.details)
            return Response(payload, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='mark-duplicate')
    def mark_duplicate(self, request, pk=None):
        """
        Mark this task as a duplicate of another task.
        Creates a `duplicates` link, posts a comment, and closes this task
        to a terminal status (rejected → cancelled → completed, in that order
        of preference depending on what the workflow supports).
        """
        from .services.workflow_service import WorkflowService

        item = self.get_object()
        if (
            str(item.workflow_name or '').lower().replace('_', '-') == 'focused-qa'
            or WorkflowService._is_focused_qa_leg(item)
        ):
            return Response(
                {'error': 'Focused-QA duplicates require the Close Duplicate / Subsumed '
                 'workflow transition and its typed receipt; direct duplicate closure is not allowed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target_key = request.data.get('target_item_key')
        target_id = request.data.get('target_item_id')
        comment_text = request.data.get('comment', '').strip()

        if not target_key and not target_id:
            return Response(
                {'error': 'target_item_key or target_item_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Resolve target
        try:
            if target_key:
                target = Item.objects.get(item_key__iexact=target_key)
            else:
                target = Item.objects.get(id=target_id)
        except Item.DoesNotExist:
            return Response({'error': 'Target task not found'}, status=status.HTTP_404_NOT_FOUND)

        if target.id == item.id:
            return Response({'error': 'Cannot mark a task as a duplicate of itself'}, status=status.HTTP_400_BAD_REQUEST)

        # Create the `duplicates` link (idempotent)
        try:
            ItemLink.objects.get_or_create(
                source_item=item,
                target_item=target,
                link_type='duplicates',
                defaults={
                    'created_by_id': request.user.id,
                    'created_by_email': request.user.email,
                }
            )
        except DjangoValidationError as exc:
            message = exc.messages[0] if exc.messages else str(exc)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)

        # Pick a terminal status. If the workflow has 'rejected', use it.
        # Otherwise fall back to 'cancelled' or 'completed'.
        target_status = None
        if item.workflow_name:
            try:
                from .models import Workflow
                wf = Workflow.objects.filter(slug=item.workflow_name).first()
                if wf:
                    available_slugs = set(wf.statuses.values_list('slug', flat=True))
                    for candidate in ['duplicate', 'rejected', 'cancelled', 'closed', 'completed', 'done']:
                        if candidate in available_slugs:
                            target_status = candidate
                            break
            except Exception:
                pass

        if not target_status:
            # No workflow or couldn't find one — use generic cancelled/completed
            target_status = 'cancelled' if item.status not in ('completed',) else 'completed'

        # Save previous status in metadata so we can restore it on unmark
        metadata = dict(item.metadata or {})
        metadata['duplicate_of'] = {
            'target_id': target.id,
            'target_key': target.item_key,
            'previous_status': item.status,
            'previous_workflow_status': item.workflow_status,
            'marked_by': request.user.email,
            'marked_at': timezone.now().isoformat(),
        }
        item.metadata = metadata

        # Update status (and workflow_status if applicable)
        item.status = target_status
        if item.workflow_name:
            item.workflow_status = target_status
        item.save(update_fields=['status', 'workflow_status', 'metadata', 'updated_at'])

        # Add a comment so it's visible in the activity timeline
        from .models import Comment
        target_label = target.item_key or f"#{target.id}"
        comment_body = (
            f"Marked as duplicate of <strong>{target_label}</strong>: {target.title}"
        )
        if comment_text:
            comment_body += f"\n\n{comment_text}"
        try:
            Comment.objects.create(
                item=item,
                content=comment_body,
                author_id=request.user.id,
                author_email=request.user.email,
            )
        except Exception:
            pass  # Comment creation is best-effort

        # Return the updated item
        return Response({
            'item': ItemSerializer(item).data,
            'target': {
                'id': target.id,
                'item_key': target.item_key,
                'title': target.title,
            },
            'new_status': target_status,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='unmark-duplicate')
    def unmark_duplicate(self, request, pk=None):
        """
        Reverse a mark-as-duplicate action: remove the `duplicates` link,
        restore the previous status, and post a comment.
        """
        item = self.get_object()
        metadata = dict(item.metadata or {})
        dup_info = metadata.get('duplicate_of')

        if not dup_info:
            return Response(
                {'error': 'This task was not marked as a duplicate'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Remove the duplicates link (source = this item)
        target_id = dup_info.get('target_id')
        removed_count = 0
        if target_id:
            removed_count, _ = ItemLink.objects.filter(
                source_item=item,
                target_item_id=target_id,
                link_type='duplicates',
            ).delete()
        else:
            # Fallback: remove any outgoing duplicates link
            removed_count, _ = ItemLink.objects.filter(
                source_item=item,
                link_type='duplicates',
            ).delete()

        # Restore previous status
        prev_status = dup_info.get('previous_status') or 'pending'
        prev_workflow_status = dup_info.get('previous_workflow_status') or prev_status
        item.status = prev_status
        if item.workflow_name:
            item.workflow_status = prev_workflow_status

        # Clean up the metadata flag
        metadata.pop('duplicate_of', None)
        item.metadata = metadata
        item.save(update_fields=['status', 'workflow_status', 'metadata', 'updated_at'])

        # Post a comment to the activity timeline
        from .models import Comment
        target_label = dup_info.get('target_key') or (f"#{target_id}" if target_id else 'the target')
        try:
            Comment.objects.create(
                item=item,
                content=f"Unmarked as duplicate of <strong>{target_label}</strong> — status restored to <em>{prev_workflow_status}</em>",
                author_id=request.user.id,
                author_email=request.user.email,
            )
        except Exception:
            pass

        return Response({
            'item': ItemSerializer(item).data,
            'restored_status': prev_workflow_status,
            'link_removed': bool(removed_count),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post', 'delete'])
    def watchers(self, request, pk=None):
        """Manage watchers for an item."""
        item = self.get_object()

        if request.method == 'GET':
            watchers = ItemWatcher.objects.filter(item=item)
            serializer = ItemWatcherSerializer(watchers, many=True)
            return Response({
                'count': watchers.count(),
                'watchers': serializer.data
            })

        elif request.method == 'POST':
            # Add current user as watcher
            watcher, created = ItemWatcher.objects.get_or_create(
                item=item,
                user_id=request.user.id,
                defaults={
                    'user_email': request.user.email,
                    'user_name': (
                        f"{getattr(request.user, 'first_name', '')} {getattr(request.user, 'last_name', '')}".strip()
                        or getattr(request.user, 'username', '')
                    ),
                }
            )

            if created:
                return Response({
                    'message': 'Now watching this item',
                    'watcher': ItemWatcherSerializer(watcher).data
                }, status=status.HTTP_201_CREATED)
            else:
                return Response({
                    'message': 'Already watching this item',
                    'watcher': ItemWatcherSerializer(watcher).data
                })

        elif request.method == 'DELETE':
            # Remove current user as watcher
            deleted, _ = ItemWatcher.objects.filter(item=item, user_id=request.user.id).delete()

            if deleted:
                return Response({'message': 'Stopped watching this item'})
            else:
                return Response({'message': 'You were not watching this item'})

    @action(detail=True, methods=['get'])
    def attachments(self, request, pk=None):
        """Get attachments for an item."""
        item = self.get_object()
        attachments = Attachment.objects.filter(item=item)
        serializer = AttachmentSerializer(attachments, many=True)
        return Response({
            'count': attachments.count(),
            'attachments': serializer.data
        })

    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """Get item statistics — includes user-scoped 'my' counts for dashboard."""
        from .models import Status
        queryset = self.get_queryset()
        if request.query_params.get('projection') == 'status_counts':
            # Home needs a complete grouped count, not a paginated item list or
            # all the unrelated statistics below. Keep the ordinary permission,
            # organization, mode and active filters from get_queryset intact.
            counts = list(queryset.order_by().values('status').annotate(count=Count('id', distinct=True)))
            categories = dict(Status.objects.filter(
                slug__in=[row['status'] for row in counts],
            ).values_list('slug', 'category'))
            for row in counts:
                row['status_category'] = categories.get(row['status'])
            return Response({'counts': counts,
                             'total': sum(row['count'] for row in counts),
                             'as_of': timezone.now().isoformat()})
        now = timezone.now()
        last_24h = now - timedelta(hours=24)

        # User-scoped filter (items assigned to current user by ID or email)
        user = request.user
        my_filter = Q(assignee_id=user.id)
        if getattr(user, 'email', None):
            my_filter |= Q(assignee_email=user.email)
        my_items = queryset.filter(my_filter)

        # Org-wide (global) task rollup for OPS / MCP `pulse_get_statistics`
        # reporting (PLAT-171). The user-scoped `tasks` block below powers the
        # dashboard's "my" cards; the MCP rollup needs ORG-WIDE figures that
        # reconcile with `by_mode.task`.
        #
        # Status sets are derived CONSISTENTLY from Status.category (PLAT-171
        # review / PLAT-145 lesson) so workflow-specific slugs aren't missed by a
        # hardcoded list, with the legacy enum set (TERMINAL_STATUS_SLUGS) added
        # for pre-Status-table rows whose `status` column still holds the old
        # enum (no Status row). Per the AC, "completed" = reaching ANY terminal
        # state (done OR cancelled/rejected/failed/etc.), so the same `terminal`
        # set feeds both `completed` and the exclusion from `pending` — no rows
        # fall into neither bucket.
        from .models import Status
        done_cat = set(Status.objects.filter(category='done').values_list('slug', flat=True))
        scheduled_cat = set(Status.objects.filter(category='scheduled').values_list('slug', flat=True))
        # Terminal (a closure) = done-category ∪ the full legacy terminal enum set
        # (completed/closed/done/resolved/merged/cancelled/rejected/duplicate/
        # wont_fix/failed/expired/deferred).
        terminal_slugs = done_cat | set(TERMINAL_STATUS_SLUGS)
        # Inactive (not pending) = terminal ∪ scheduled-category (parked recurring
        # tasks aren't active work).
        inactive_slugs = terminal_slugs | scheduled_cat

        global_task_qs = queryset.filter(mode='task')
        # 24h closure window keyed on `resolution_date` (when the item entered a
        # terminal state), falling back to `completed_at` — NOT `updated_at`,
        # which a maintenance edit of an old closed task would falsely bump
        # (PLAT-171 review). Mirrors the view's own done-window pattern (~L432).
        # KNOWN LIMITATION: a recurring cycle that completes is auto-re-parked to
        # 'scheduled', clearing resolution_date + completed_at, so its closure
        # isn't captured here — counting those needs the activity/transition log
        # (tracked follow-up; out of scope for this rollup).
        global_completed_24h = global_task_qs.filter(
            status__in=terminal_slugs
        ).filter(
            Q(resolution_date__gte=last_24h)
            | Q(resolution_date__isnull=True, completed_at__gte=last_24h)
        ).count()

        stats = {
            'total_items': queryset.count(),
            # User-scoped stats (what the dashboard cards read).
            # "pending" = actionable now: the SAME active-queue definition the
            # dashboard "My Tasks · Active" list uses (filter_active_queue_items),
            # so the stat card and the list directly beneath it cannot disagree.
            # PLS-872: the card previously used only TERMINAL_STATUS_SLUGS +
            # future-scheduled-recurring, which counted backlog/scheduled/
            # waiting_external rows the Active list drops — "Pending Tasks 13"
            # vs "My Tasks 12" in one viewport.
            'tasks': {
                'pending': (
                    filter_active_queue_items(
                        my_items.filter(mode='task')
                    ).count()
                ),
                'completed': my_items.filter(mode='task', status='completed').count(),
                'total': my_items.filter(mode='task').count(),
            },
            # Org-wide task rollup (total reconciles with by_mode.task) — PLAT-171.
            # Consistent category-derived sets: completed = reached a terminal
            # state; pending excludes terminal AND scheduled (parked recurring
            # tasks don't inflate pending). Same `terminal_slugs` on both sides so
            # no row is counted in neither bucket.
            'global_tasks': {
                'total': global_task_qs.count(),
                'pending': global_task_qs.exclude(status__in=inactive_slugs).count(),
                'completed': global_task_qs.filter(status__in=terminal_slugs).count(),
                'completed_last_24h': global_completed_24h,
            },
            'actions': {
                'pending': my_items.filter(mode='action', status='pending').count(),
            },
            'alerts': {
                # PLS-830: firing KPI must be ORG-WIDE (permission-scoped via
                # `queryset`) and count ONLY status='firing' so it agrees with
                # `by_status.firing`, the Alerts UI, and `mode=alert&status=firing`.
                # Previously it used `my_items` (assigned-to-me) and folded
                # acknowledged into firing, so a caller with no alerts assigned
                # saw 0 while live firing alerts existed (all-clear false state).
                'firing': queryset.filter(mode='alert', status='firing').count(),
                'acknowledged': queryset.filter(mode='alert', status='acknowledged').count(),
            },
            # Global breakdowns
            'by_mode': {
                'task': queryset.filter(mode='task').count(),
                'action': queryset.filter(mode='action').count(),
                'alert': queryset.filter(mode='alert').count(),
            },
            'by_status': {},
            'by_priority': {
                'low': queryset.filter(priority='low').count(),
                'normal': queryset.filter(priority='normal').count(),
                'high': queryset.filter(priority='high').count(),
                'urgent': queryset.filter(priority='urgent').count(),
            },
            'overdue': my_items.filter(
                due_date__lt=now,
                status__in=['pending', 'in_progress', 'firing', 'acknowledged']
            ).count(),
            'due_today': my_items.filter(
                due_date__date=now.date(),
                status__in=['pending', 'in_progress']
            ).count(),
            # Org-wide headline velocity metric. Previously user-scoped, so it
            # read 0 for the MCP service account even as the org closed work
            # (PLAT-171). The my-scoped value is kept separately below for any
            # per-user consumer.
            'tasks_completed_last_24h': global_completed_24h,
            'my_tasks_completed_last_24h': my_items.filter(
                mode='task',
                status='completed',
                completed_at__gte=last_24h
            ).count(),
        }

        # Get status counts. .values('status').annotate(...) MUST clear the model's
        # default ordering first: Item.Meta.ordering otherwise leaks its fields into
        # the GROUP BY, making each row its own group so every status returns
        # count=1 (PLAT-171 — the "by_status all 1s" symptom). order_by() resets
        # the grouping; count a stable column (id), not the grouped column.
        status_counts = queryset.order_by().values('status').annotate(count=Count('id'))
        for item in status_counts:
            stats['by_status'][item['status']] = item['count']

        return Response(stats)

    # === Export / Import ===

    @action(detail=False, methods=['get'], url_path='export/bundle')
    def export_bundle(self, request):
        """
        Export the current queryset (after all filters applied) as a ZIP bundle.
        Query params are the same as the list endpoint (status, project, mode, etc.).
        """
        from django.http import HttpResponse
        from .services.export_service import export_bundle as _export_bundle

        queryset = self.filter_queryset(self.get_queryset())
        include_comments = request.query_params.get('include_comments', 'true').lower() == 'true'
        include_links = request.query_params.get('include_links', 'true').lower() == 'true'
        source_instance = request.build_absolute_uri('/pulse/').rstrip('/')

        zip_bytes = _export_bundle(
            queryset,
            include_comments=include_comments,
            include_links=include_links,
            source_instance=source_instance,
            user=request.user,
        )

        timestamp = timezone.now().strftime('%Y-%m-%d-%H%M%S')
        filename = f'pulse-bundle-{timestamp}.zip'
        response = HttpResponse(zip_bytes, content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = str(len(zip_bytes))
        return response

    @action(detail=False, methods=['get'], url_path='export/csv')
    def export_csv(self, request):
        """Export the current queryset as a CSV file."""
        from django.http import HttpResponse
        from .services.export_service import export_csv as _export_csv

        queryset = self.filter_queryset(self.get_queryset())
        csv_text = _export_csv(queryset)

        timestamp = timezone.now().strftime('%Y-%m-%d-%H%M%S')
        filename = f'pulse-tasks-{timestamp}.csv'
        response = HttpResponse(csv_text, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @action(detail=False, methods=['post'], url_path='import/bundle', parser_classes=[MultiPartParser])
    def import_bundle(self, request):
        """
        Import a Pulse bundle (ZIP produced by /export/bundle/).
        Multipart upload: field name "file".
        Optional form fields: target_project_id, include_comments, include_links.
        """
        from .services.import_service import import_bundle as _import_bundle, ImportError as _ImportError

        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response(
                {'error': 'No file uploaded (expected multipart field "file")'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_project = None
        target_project_id = request.data.get('target_project_id')
        if target_project_id:
            target_project = self._authorized_import_target_project(
                request, target_project_id,
            )
            if target_project is None:
                return Response(
                    {'error': 'Target project not found or unavailable'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        include_comments = str(request.data.get('include_comments', 'true')).lower() == 'true'
        include_links = str(request.data.get('include_links', 'true')).lower() == 'true'

        try:
            result = _import_bundle(
                uploaded.read(),
                target_project=target_project,
                importer_user=request.user,
                include_comments=include_comments,
                include_links=include_links,
            )
        except _ImportError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result.as_dict(), status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='import/csv', parser_classes=[MultiPartParser])
    def import_csv(self, request):
        """
        Import a CSV file of tasks.
        Multipart upload: field name "file".
        Optional form field: target_project_id.
        """
        from .services.import_service import import_csv as _import_csv

        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response(
                {'error': 'No file uploaded (expected multipart field "file")'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_project = None
        target_project_id = request.data.get('target_project_id')
        if target_project_id:
            target_project = self._authorized_import_target_project(
                request, target_project_id,
            )
            if target_project is None:
                return Response(
                    {'error': 'Target project not found or unavailable'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        try:
            csv_text = uploaded.read().decode('utf-8')
        except UnicodeDecodeError:
            return Response(
                {'error': 'File is not valid UTF-8 text'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = _import_csv(
            csv_text,
            target_project=target_project,
            importer_user=request.user,
        )

        return Response(result.as_dict(), status=status.HTTP_200_OK)

    # === Helper methods ===

    @staticmethod
    def _authorized_import_target_project(request, target_project_id):
        """Resolve an import destination through browse + create authority.

        Returning ``None`` for nonexistent and unauthorized projects keeps the
        endpoint non-enumerating.  The caller-provided id never selects an
        organization directly; the target Project remains the tenant source.
        """
        try:
            target_project_id = int(target_project_id)
        except (TypeError, ValueError):
            return None
        project = PermissionService.get_user_projects(
            user=request.user,
        ).filter(
            pk=target_project_id,
            is_active=True,
            is_archived=False,
        ).first()
        if project is None:
            return None
        if not PermissionService.can_access_project(
            request.user.id,
            project,
            permission='create',
            user_email=getattr(request.user, 'email', None),
            user=request.user,
        ):
            return None
        return project

    def _create_next_recurring_instance(self, completed_task):
        """Create next instance of a recurring task."""
        if not completed_task.is_recurring or completed_task.frequency == 'one_time':
            return None

        next_due_date = self._calculate_next_due_date(completed_task)

        if next_due_date and (not completed_task.recurrence_end_date or
                            next_due_date <= completed_task.recurrence_end_date):

            next_task = Item.objects.create(
                mode='task',
                item_type=completed_task.item_type,
                title=completed_task.title,
                description=completed_task.description,
                source=completed_task.source,
                assignee_id=completed_task.assignee_id,
                assignee_email=completed_task.assignee_email,
                created_by_id=AI_ID,
                created_by_email='ai@shizuha.com',
                due_date=next_due_date,
                priority=completed_task.priority,
                frequency=completed_task.frequency,
                recurrence_pattern=completed_task.recurrence_pattern,
                recurrence_end_date=completed_task.recurrence_end_date,
                is_recurring=completed_task.is_recurring,
                parent_item=completed_task,
                notification_preferences=completed_task.notification_preferences,
                metadata=completed_task.metadata,
                tags=completed_task.tags,
                estimated_hours=completed_task.estimated_hours,
                labels=completed_task.labels,
            )

            if completed_task.reminder_time and completed_task.due_date:
                reminder_offset = completed_task.due_date - completed_task.reminder_time
                next_task.reminder_time = next_due_date - reminder_offset
                next_task.save()

            logger.info(f"Created next recurring instance {next_task.id} from {completed_task.id}")
            return next_task

        return None

    def _calculate_next_due_date(self, task):
        """Calculate next due date based on frequency.

        Delegates to services.recurring.calculate_next_due_date so the
        scheduler loop and this legacy endpoint share the same logic.
        """
        from .services.recurring import calculate_next_due_date
        return calculate_next_due_date(
            task.due_date,
            task.frequency,
            task.get_recurrence_pattern_dict() if task.recurrence_pattern else None,
        )

    @action(detail=False, methods=['post'], url_path='migrate-workflow')
    def migrate_workflow(self, request):
        """PLS-54: migrate task(s) to a different workflow, remapping each
        task's status to the target workflow's same-category status.

        Body: {task_keys: [..] | task_key: "..", target_workflow: "..",
               dry_run: bool (default True)}.

        dry_run (default) returns the planned remap (old vs new workflow+status)
        WITHOUT mutating. A non-dry-run apply is destructive and admin/CoS-gated
        (PLAT-218): it records a reversible pre-migration entry in
        metadata['workflow_migrations'] and re-files each task through the
        workflow engine (Item.save -> category-based status coercion), never raw
        SQL. dry_run and apply share Item.remap_status_for_workflow so the
        preview always matches the applied result.
        """
        data = request.data or {}
        keys = data.get('task_keys')
        if not keys and data.get('task_key'):
            keys = [data['task_key']]
        target = (data.get('target_workflow') or '').strip()
        dry_run = data.get('dry_run', True)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ('false', '0', 'no', '')
        if not keys or not isinstance(keys, (list, tuple)):
            return Response({'error': 'task_keys (a list) or task_key is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not target:
            return Response({'error': 'target_workflow is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        wf = Workflow.resolve(target)
        if not wf:
            return Response({'error': f'target workflow {target!r} not found'},
                            status=status.HTTP_404_NOT_FOUND)

        user = request.user
        is_privileged = getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)
        is_admin_ops_backup = False
        if not dry_run and not is_privileged:
            # PLAT-6037: workflow migration is explicit-list, category-preserving,
            # and carries a reversible before-state record.  A current member of
            # the task organization's Admin Ops roster is therefore a delegated
            # operational executor.  Authorize every target independently below;
            # no target means no cross-org or first-item-derived authority.
            #
            # PLAT-6037/PLS-1069 cell C: the delegated check MUST be exact-org.
            # _is_team_member falls back to the platform (shizuha) roster for
            # shizuha-only lanes (admin-ops/human-ops) on a foreign-org task,
            # which would let a shizuha Admin Ops backup borrow authority onto
            # another org's item. Use the exact-org helper so the cross-org
            # negative control fails closed.
            from .services.workflow_service import WorkflowService
            is_admin_ops_backup = True
            for key in keys:
                candidate = _resolve_item_identifier(key, mode='task')
                if not candidate or not WorkflowService._is_exact_org_team_member(
                    getattr(user, 'email', ''), 'admin-ops', item=candidate,
                ):
                    is_admin_ops_backup = False
                    break
        if not dry_run and not (is_privileged or is_admin_ops_backup):
            return Response(
                {'error': 'Applying a workflow migration requires staff/CoS or '
                          'exact-org Admin Ops delegated authority (PLAT-6037). '
                          'Use dry_run=true to preview.'},
                status=status.HTTP_403_FORBIDDEN)

        from django.utils import timezone
        results = []
        for key in keys:
            item = _resolve_item_identifier(key, mode='task')
            if not item:
                _normalized, identifier_type, suggestion = _describe_item_identifier(key)
                results.append({
                    'task_key': key,
                    'identifier_type': identifier_type,
                    'suggested_identifier': suggestion,
                    'error': _format_item_identifier_error(key),
                })
                continue
            new_status = Item.remap_status_for_workflow(item.status, wf)
            entry = {
                'task_key': item.item_key,
                'from': {'workflow_name': item.workflow_name,
                         'status': item.status,
                         'workflow_status': item.workflow_status},
                'to': {'workflow_name': wf.name, 'status': new_status},
                'applied': False,
            }
            if not dry_run:
                meta = item.metadata if isinstance(item.metadata, dict) else {}
                meta.setdefault('workflow_migrations', []).append({
                    'from': entry['from'],
                    'to': {'workflow_name': wf.name},
                    'at': timezone.now().isoformat(),
                    'by': getattr(user, 'email', '') or getattr(user, 'username', ''),
                    'authority': (
                        'admin-ops-delegated'
                        if is_admin_ops_backup and not is_privileged
                        else 'staff'
                    ),
                })
                item.metadata = meta
                item.workflow_name = wf.name
                item.workflow_status = None  # let save() heal via category coercion
                item.save()
                entry['to']['status'] = item.status  # actual post-save slug
                entry['applied'] = True
            results.append(entry)

        return Response({'dry_run': bool(dry_run),
                         'target_workflow': wf.name,
                         'count': len(results),
                         'results': results})


# Backward compatibility alias
TaskViewSet = ItemViewSet


# =============================================================================
# Alert-specific API Views
# =============================================================================


class AiRecapListCreateView(APIView):
    """List and create bounded, citation-backed AI recaps for readable projects."""

    permission_classes = [permissions.IsAuthenticated]

    def _visible_runs(self, request):
        project_ids = PermissionService.get_user_projects(user=request.user).values_list('id', flat=True)
        qs = AiGenerationRun.objects.filter(project_id__in=project_ids).select_related('project', 'sprint')
        project_id = request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        recap_type = request.query_params.get('type')
        if recap_type:
            qs = qs.filter(type=recap_type)
        return qs

    def get(self, request):
        limit = request.query_params.get('limit', 25)
        try:
            limit = min(max(1, int(limit)), 100)
        except (TypeError, ValueError):
            limit = 25
        serializer = AiGenerationRunSerializer(self._visible_runs(request)[:limit], many=True)
        return Response({'results': serializer.data, 'max_window_days': MAX_WINDOW_DAYS})

    def post(self, request):
        data = request.data or {}
        project_id = data.get('project_id')
        if not project_id:
            return Response(
                {'detail': 'project_id is required and must reference an active project'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            project = _accessible_projects_for_user(request.user).get(
                id=project_id, is_active=True
            )
        except (Project.DoesNotExist, ValueError, TypeError):
            raise PermissionDenied('You do not have access to that project.')

        sprint = None
        sprint_id = data.get('sprint_id')
        if sprint_id:
            try:
                sprint = Sprint.objects.get(id=sprint_id, project=project)
            except (Sprint.DoesNotExist, ValueError, TypeError):
                return Response({'detail': 'sprint_id does not reference an existing sprint'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            start = datetime.fromisoformat(str(data.get('source_window_start')).replace('Z', '+00:00'))
            end = datetime.fromisoformat(str(data.get('source_window_end')).replace('Z', '+00:00'))
            if timezone.is_naive(start):
                start = timezone.make_aware(start, timezone.get_current_timezone())
            if timezone.is_naive(end):
                end = timezone.make_aware(end, timezone.get_current_timezone())
            run = create_recap_run(
                user=request.user,
                project=project,
                sprint=sprint,
                recap_type=data.get('type') or 'project',
                source_window_start=start,
                source_window_end=end,
                model=data.get('model') or DEFAULT_MODEL,
                prompt_template_version=data.get('prompt_template_version') or PROMPT_TEMPLATE_VERSION,
                client_id=data.get('client_id') or 'pulse-api',
            )
        except RecapPermissionDenied as exc:
            raise PermissionDenied(str(exc))
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(AiGenerationRunSerializer(run).data, status=status.HTTP_201_CREATED)


class AiRecapDetailView(APIView):
    """Read a recap only if the caller can read its source project."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        project_ids = PermissionService.get_user_projects(user=request.user).values_list('id', flat=True)
        try:
            run = AiGenerationRun.objects.select_related('project', 'sprint').get(id=run_id, project_id__in=project_ids)
        except AiGenerationRun.DoesNotExist:
            raise PermissionDenied('Recap is outside the caller readable scope')
        return Response(AiGenerationRunSerializer(run).data)


class AiRecapFeedbackView(APIView):
    """Append human correction/usefulness feedback to a generated recap."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, run_id):
        project_ids = PermissionService.get_user_projects(user=request.user).values_list('id', flat=True)
        try:
            run = AiGenerationRun.objects.select_related('project', 'sprint').get(id=run_id, project_id__in=project_ids)
        except AiGenerationRun.DoesNotExist:
            raise PermissionDenied('Recap is outside the caller readable scope')
        data = request.data or {}
        verdict = (data.get('verdict') or '').strip()
        if not verdict:
            return Response({'detail': 'verdict is required'}, status=status.HTTP_400_BAD_REQUEST)
        run = append_recap_feedback(
            run=run,
            user=request.user,
            verdict=verdict,
            comment=data.get('comment') or '',
            bullet_index=data.get('bullet_index'),
        )
        return Response(AiGenerationRunSerializer(run).data)

class StatuszView(APIView):
    """Mechanism-liveness heartbeats for the meta-probe (PLAT-1295).

    Public, unauthenticated, read-only JSON exposing the last-fired unix
    timestamps of internal mechanisms plus server ``now``, so an http-json-age
    liveness signal (mechanism-liveness-probe.yaml) can detect a silently-dead
    mechanism while this pod stays healthy — the PLS-210 failure class that a
    k8s-availability proxy cannot see.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        import time
        from django.core.cache import cache
        from .liveness import (
            DEPLOY_VERIFY_GATE,
            DEPLOY_VERIFY_MERGE_EVENT,
            INVARIANT_VALIDATOR,
            RECURRING_ANDON,
            ROSTER_RECONCILIATION,
            STALE_SWEEP,
            OPERATOR_WAIT,
            WIP_ADMISSION,
            ZERO_MEMBER_TEAM_DETECTOR,
            read_heartbeats_status,
            read_merge_event_retry_failure,
        )
        # PLS-778: an all-null heartbeat set means EITHER nothing has fired OR
        # the liveness store is unreachable. Surface which one, so the reading
        # is attributable at the time it is taken rather than never.
        hb, liveness_store_error = read_heartbeats_status()
        pin_failure_metrics = _direct_assignment_pin_persist_failure_metrics()
        cap_routing_failures = cache.get('pulse:pls365_capability_routing_hold_failures', 0)
        roster_reconciliation = cache.get('pulse:roster_reconciliation:last')
        roster_reconciliation_failure = cache.get(
            'pulse:roster_reconciliation:last_failure'
        )
        zero_member_team_detector = cache.get(
            'pulse:zero_member_team_detector:last'
        )
        zero_member_team_detector_failure = cache.get(
            'pulse:zero_member_team_detector:last_failure'
        )
        merge_event_failure = read_merge_event_retry_failure()
        return Response({
            'now': time.time(),
            'liveness_store_ok': liveness_store_error is None,
            'liveness_store_error': liveness_store_error,
            'recurring_andon_last_run_ts': hb.get(RECURRING_ANDON),
            'invariant_validator_last_fired_ts': hb.get(INVARIANT_VALIDATOR),
            'deploy_verify_gate_last_fired_ts': hb.get(DEPLOY_VERIFY_GATE),
            'deploy_verify_merge_event_last_success_ts': hb.get(
                DEPLOY_VERIFY_MERGE_EVENT
            ),
            'deploy_verify_merge_event_last_failure': merge_event_failure,
            'stale_sweep_last_run_ts': hb.get(STALE_SWEEP),
            'operator_wait_last_run_ts': hb.get(OPERATOR_WAIT),
            'wip_admission_last_fired_ts': hb.get(WIP_ADMISSION),
            'roster_reconciliation_last_success_ts': hb.get(
                ROSTER_RECONCILIATION
            ),
            'roster_reconciliation': roster_reconciliation,
            'roster_reconciliation_last_failure': roster_reconciliation_failure,
            'zero_member_team_detector_last_success_ts': hb.get(
                ZERO_MEMBER_TEAM_DETECTOR
            ),
            'zero_member_team_detector': zero_member_team_detector,
            'zero_member_team_detector_last_failure': (
                zero_member_team_detector_failure
            ),
            'direct_assignment_pin_persist_failure_count': pin_failure_metrics['count'],
            'direct_assignment_pin_persist_last_failure_ts': pin_failure_metrics['last_failure_ts'],
            'capability_routing_notification_failure_count': cap_routing_failures,
        })


class LaneHealthView(APIView):
    """PLS-1035: reporter-accessible lane-health snapshot (read-only).

    Returns per-lane roster version, seat liveness verdicts, resolver
    routability summary, queue depth, and last assignment — so a Team-Outage
    responder can confirm lane state from one view without DevOps diagnosis.
    Any authenticated seat may read it (no DevOps-only role).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .services.lane_health import all_lanes_health, lane_health

        team = request.query_params.get('team') or request.query_params.get('lane')
        building = False
        retry_after = None
        if team:
            payload = lane_health(team)
            building = payload.get('build_status') == 'building'
            retry_after = payload.get('retry_after_seconds')
            response = Response(payload)
        else:
            lanes = all_lanes_health()
            building_lanes = [
                l for l in lanes if l.get('build_status') == 'building'
            ]
            building = bool(building_lanes)
            if building_lanes:
                retry_after = building_lanes[0].get('retry_after_seconds')
            response = Response({'lanes': lanes})
        # PLS-1244: a cold-snapshot window serves bounded building placeholders
        # instead of hanging on the full build. Signal clients to retry rather
        # than treat the payload as authoritative liveness data.
        if building:
            response.status_code = 503
            response['Retry-After'] = str(retry_after or 5)
        return response


class AlertSubmitView(APIView):
    """
    Submit a new alert with automatic deduplication.
    POST /api/alerts/

    This endpoint is public to allow external alerting systems
    (e.g., Prometheus Alertmanager) to submit alerts.
    """
    permission_classes = [permissions.AllowAny]  # Public for external systems

    def post(self, request):
        serializer = AlertSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = serializer.validated_data.get('project_id')
        if project_id:
            if not getattr(request.user, 'is_authenticated', False):
                return Response(
                    {
                        'error': (
                            'Project-routed alerts require authentication or an '
                            'organization-owned webhook key.'
                        ),
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            project = PermissionService.get_user_projects(
                user=request.user,
            ).filter(pk=project_id, is_active=True, is_archived=False).first()
            if project is None or not PermissionService.can_access_project(
                request.user.id,
                project,
                permission='create',
                user_email=getattr(request.user, 'email', None),
                user=request.user,
            ):
                return Response(
                    {'error': 'Project not found or unavailable'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        try:
            alert, is_new = AlertDeduplicationService.submit_alert(serializer.validated_data)
        except AlertIdentityError as exc:
            return Response({
                'error': 'alert_identity_error',
                'detail': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)

        if is_new:
            from .tasks import send_alert_notification
            send_alert_notification.delay(alert.id)

            return Response({
                'message': 'Alert created',
                'item_id': alert.id,
                'item_key': alert.item_key,
                'fingerprint': alert.fingerprint,
                'status': alert.status,
                'assignment_group': alert.assignment_group,
                'assignee_email': alert.assignee_email,
            }, status=status.HTTP_201_CREATED)
        else:
            return Response({
                'message': 'Alert deduplicated',
                'item_id': alert.id,
                'item_key': alert.item_key,
                'fingerprint': alert.fingerprint,
                'occurrence_count': alert.occurrence_count,
                'status': alert.status,
                'assignment_group': alert.assignment_group,
                'assignee_email': alert.assignee_email,
            }, status=status.HTTP_200_OK)


class AlertBulkAcknowledgeView(APIView):
    """
    Acknowledge multiple alerts at once.
    POST /api/alerts/bulk-acknowledge/
    Requires authentication.
    """
    # Uses default permission_classes from settings (IsAuthenticated)

    def post(self, request):
        item_ids = request.data.get('item_ids', [])
        source = request.data.get('source')
        severity = request.data.get('severity')
        label_matchers = request.data.get('label_matchers', {})
        acknowledged_by = request.data.get('acknowledged_by', 'user:bulk')

        queryset = Item.objects.filter(mode='alert', status='firing')

        if item_ids:
            queryset = queryset.filter(id__in=item_ids)
        if source:
            queryset = queryset.filter(source=source)
        if severity:
            queryset = queryset.filter(severity=severity)
        for key, value in label_matchers.items():
            queryset = queryset.filter(**{f'labels__{key}': value})

        count = 0
        for alert in queryset:
            alert.acknowledge(acknowledged_by=acknowledged_by)
            count += 1

        return Response({
            'message': f'Acknowledged {count} alerts',
            'count': count
        })


class AlertBulkResolveView(APIView):
    """
    Resolve multiple alerts at once.
    POST /api/alerts/bulk-resolve/
    Requires authentication.
    """
    # Uses default permission_classes from settings (IsAuthenticated)

    def post(self, request):
        item_ids = request.data.get('item_ids', [])
        source = request.data.get('source')
        resolved_by = request.data.get('resolved_by', 'user:bulk')
        resolution_note = request.data.get('resolution_note', '')

        queryset = Item.objects.filter(mode='alert', status__in=['firing', 'acknowledged'])

        if item_ids:
            queryset = queryset.filter(id__in=item_ids)
        if source:
            queryset = queryset.filter(source=source)

        count = 0
        for alert in queryset:
            alert.resolve(resolved_by=resolved_by, note=resolution_note)
            count += 1

        return Response({
            'message': f'Resolved {count} alerts',
            'count': count
        })


class AlertBulkDeleteView(APIView):
    """
    Delete multiple alerts at once.
    POST /api/alerts/bulk-delete/
    Requires authentication.
    """

    def post(self, request):
        item_ids = request.data.get('item_ids', [])

        if not item_ids:
            return Response(
                {'error': 'item_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        queryset = Item.objects.filter(mode='alert', id__in=item_ids)

        count = queryset.count()
        deleted_ids = list(queryset.values_list('id', flat=True))

        queryset.delete()
        logger.info(f"Bulk deleted {count} alerts by user {request.user.id}: {deleted_ids}")

        return Response({
            'message': f'Deleted {count} alerts',
            'count': count,
            'deleted_ids': deleted_ids
        })


# =============================================================================
# External Webhook Integrations (Grafana, Prometheus, etc.)
# =============================================================================

class GrafanaWebhookView(APIView):
    """
    Receive alerts from Grafana webhook contact point.
    POST /api/webhooks/grafana/<key>/

    This endpoint accepts Grafana's default webhook payload format
    and transforms it into the internal alert format.

    Authentication is via webhook key in the URL path.
    """
    permission_classes = [permissions.AllowAny]  # Auth via webhook key

    def post(self, request, key):
        # Validate webhook key
        try:
            webhook_key = WebhookKey.objects.get(key=key, is_active=True)
        except WebhookKey.DoesNotExist:
            return Response(
                {'error': 'Invalid or inactive webhook key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Parse Grafana payload
        payload = request.data
        alerts_data = payload.get('alerts', [])

        if not alerts_data:
            return Response(
                {'error': 'No alerts in payload'},
                status=status.HTTP_400_BAD_REQUEST
            )

        created_count = 0
        deduplicated_count = 0
        resolved_count = 0
        results = []

        for alert in alerts_data:
            alert_status = alert.get('status', 'firing')

            # Extract data from Grafana alert format
            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})
            values = alert.get('values', {})

            # Build title from rulename + alertname labels
            alertname = labels.get('alertname', '')
            rulename = labels.get('rulename', '')

            if rulename and alertname and rulename != alertname:
                title = f'{rulename}: {alertname}'
            elif rulename:
                title = rulename
            elif alertname:
                title = alertname
            else:
                title = annotations.get('summary') or 'Grafana Alert'

            # Build description from annotations
            description = annotations.get('description', '')
            if not description and annotations.get('summary'):
                description = annotations.get('summary')

            # Map Grafana severity to our severity (if present in labels)
            grafana_severity = labels.get('severity', webhook_key.default_severity)
            severity_map = {
                'critical': 'critical',
                'error': 'error',
                'warning': 'warning',
                'info': 'info',
                'page': 'critical',  # Common Grafana convention
                'ticket': 'warning',
            }
            severity = severity_map.get(grafana_severity.lower(), webhook_key.default_severity)

            # Merge auto_labels from webhook key
            merged_labels = {**webhook_key.auto_labels, **labels}

            # Build payload with Grafana-specific data
            alert_payload = {
                'grafana_fingerprint': alert.get('fingerprint'),
                'generator_url': alert.get('generatorURL'),
                'silence_url': alert.get('silenceURL'),
                'dashboard_url': alert.get('dashboardURL'),
                'panel_url': alert.get('panelURL'),
                'image_url': alert.get('imageURL'),
                'starts_at': alert.get('startsAt'),
                'ends_at': alert.get('endsAt'),
                'values': values,
                'annotations': annotations,
            }

            if alert_status == 'resolved':
                # Handle resolution - find and resolve matching alert
                resolved = self._resolve_alert(webhook_key, merged_labels, alert.get('fingerprint'))
                if resolved:
                    resolved_count += 1
                    results.append({
                        'status': 'resolved',
                        'title': title,
                        'fingerprint': alert.get('fingerprint')
                    })
            else:
                # Create or deduplicate firing alert.
                # Note: assignee is intentionally left empty at ingest
                # (industry-standard routing — alerts are scoped to a project
                # and optionally a team group; individuals claim on ack).
                routed_project_id = _routed_default_project_id(webhook_key)
                routed_project = (
                    Project.objects.filter(id=routed_project_id).first()
                    if routed_project_id else None
                )
                alert_data = {
                    'item_type': f'alert.{severity}',
                    'title': title,
                    'description': description,
                    'source': webhook_key.source_name,
                    'source_id': alert.get('fingerprint'),
                    'source_url': fit_source_url(alert.get('generatorURL') or alert.get('dashboardURL')),
                    'severity': severity,
                    'priority': 'high' if severity in ['critical', 'error'] else 'normal',
                    'labels': merged_labels,
                    'payload': alert_payload,
                    'project_id': routed_project_id,
                    'assignment_group': _label_assignment_group(
                        merged_labels,
                        webhook_key.default_assignment_group,
                        project=routed_project,
                    ),
                }

                try:
                    item, is_new = AlertDeduplicationService.submit_alert(alert_data)
                except AlertIdentityError as exc:
                    results.append({
                        'status': 'failed',
                        'error': 'alert_identity_error',
                        'detail': str(exc),
                        'title': title,
                    })
                    continue

                if is_new:
                    created_count += 1
                    from .tasks import send_alert_notification
                    send_alert_notification.delay(item.id)
                else:
                    deduplicated_count += 1

                results.append({
                    'status': 'created' if is_new else 'deduplicated',
                    'item_id': item.id,
                    'fingerprint': item.fingerprint,
                    'title': title
                })

        # Record usage
        webhook_key.record_usage()

        failed_count = sum(1 for result in results if result.get('status') == 'failed')
        response_status = status.HTTP_201_CREATED
        if failed_count and not (created_count or deduplicated_count or resolved_count):
            response_status = status.HTTP_400_BAD_REQUEST
        elif deduplicated_count or resolved_count or failed_count:
            response_status = status.HTTP_200_OK

        return Response({
            'message': f'Processed {len(alerts_data)} alerts',
            'created': created_count,
            'deduplicated': deduplicated_count,
            'resolved': resolved_count,
            'failed': failed_count,
            'results': results
        }, status=response_status)

    def _resolve_alert(self, webhook_key, labels, fingerprint):
        """Find and resolve an alert by fingerprint or labels.

        Scoping is by source_name (+ source_id/fingerprint when available).
        We do NOT filter by assignee — alerts are now created unassigned,
        and an assignee set later (via claim/ack) must not prevent resolve.
        """
        queryset = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged'],
            source=webhook_key.source_name,
        )

        if fingerprint:
            queryset = queryset.filter(source_id=fingerprint)

        alert = queryset.first()
        if alert:
            alert.resolve(resolved_by=f'grafana:{webhook_key.source_name}', note='Auto-resolved by Grafana')
            return True
        return False


class PrometheusAlertmanagerWebhookView(APIView):
    """
    Receive alerts from Prometheus Alertmanager webhook receiver.
    POST /api/webhooks/alertmanager/<key>/

    Accepts Alertmanager v2 webhook format.
    """
    permission_classes = [permissions.AllowAny]  # Auth via webhook key

    def post(self, request, key):
        # Validate webhook key
        try:
            webhook_key = WebhookKey.objects.get(key=key, is_active=True)
        except WebhookKey.DoesNotExist:
            return Response(
                {'error': 'Invalid or inactive webhook key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Alertmanager format is similar to Grafana but with some differences
        payload = request.data
        alerts_data = payload.get('alerts', [])

        if not alerts_data:
            return Response(
                {'error': 'No alerts in payload'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # PLS-86: do NOT process the batch synchronously in the request. The
        # per-alert dedup/create holds per-fingerprint SELECT…FOR UPDATE row locks
        # across create/update; under an alert burst (with Alertmanager retrying
        # the same batches) concurrent requests piled onto those locks → 25-63s
        # latency → 500 → Alertmanager dropped the batch after 6 retries. Validate
        # + enqueue + return immediately so the webhook stays sub-second and
        # lock-free; the Celery worker does the dedup/create/resolve upserts. The
        # group-level `status` is passed through as the per-alert resolved fallback.
        payload_status = str(payload.get('status') or '').strip().lower()
        from .tasks import process_alertmanager_alerts
        process_alertmanager_alerts.delay(webhook_key.id, alerts_data, payload_status)

        return Response({
            'message': f'Accepted {len(alerts_data)} alerts for processing',
            'queued': len(alerts_data),
        }, status=status.HTTP_200_OK)


class GenericWebhookView(APIView):
    """
    Generic webhook endpoint that accepts a simplified format.
    POST /api/webhooks/generic/<key>/

    Accepts our standard alert format, useful for custom integrations.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request, key):
        try:
            webhook_key = WebhookKey.objects.get(key=key, is_active=True)
        except WebhookKey.DoesNotExist:
            return Response(
                {'error': 'Invalid or inactive webhook key'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Use standard AlertSubmitSerializer
        serializer = AlertSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # Apply webhook key routing defaults. Unlike the legacy flow, we do
        # NOT stomp assignee_id/email from the key — alerts are routed to a
        # project + team group and left unassigned (industry-standard).
        # Caller-supplied assignee, if any, is preserved.
        data['source'] = data.get('source') or webhook_key.source_name
        routed_project_id = _routed_default_project_id(webhook_key)
        if routed_project_id and not data.get('project_id'):
            data['project_id'] = routed_project_id
        if webhook_key.default_assignment_group and not data.get('assignment_group'):
            data['assignment_group'] = webhook_key.default_assignment_group

        # Defaults are persisted configuration, but they still pass the same
        # exact project/Team boundary as caller-supplied alert routing.
        routed_serializer = AlertSubmitSerializer(data=data)
        routed_serializer.is_valid(raise_exception=True)
        data = routed_serializer.validated_data

        # Merge auto_labels
        data['labels'] = {**webhook_key.auto_labels, **data.get('labels', {})}

        # Handle resolution if status is 'resolved'
        if data.get('status') == 'resolved':
            from django.conf import settings as dj_settings
            from .tasks import _alert_dm_cache_key, _clear_alert_dm_reservation

            # Clear DM-only dedup so a later re-fire can notify again
            # (origin-ci + Alertmanager paths).
            sid = data.get('source_id') or data.get('title') or ''
            if sid:
                _clear_alert_dm_reservation(
                    _alert_dm_cache_key(data['source'], sid)
                )

            # Match by source (+ source_id if provided). No assignee filter —
            # alerts are ingested unassigned; claiming must not block resolve.
            queryset = Item.objects.filter(
                mode='alert',
                status__in=['firing', 'acknowledged'],
                source=data['source'],
            )
            if data.get('source_id'):
                queryset = queryset.filter(source_id=data['source_id'])

            resolved = queryset.first()
            if resolved:
                resolved.resolve(resolved_by=f'webhook:{webhook_key.source_name}')
                webhook_key.record_usage()
                return Response({
                    'message': 'Alert resolved',
                    'item_id': resolved.id
                })
            # DM-only sources (origin-ci) often never create Items — still 200.
            if data['source'] == 'origin-ci' and not getattr(
                dj_settings, 'PULSE_ORIGIN_CI_PERSIST_ITEMS', False
            ):
                webhook_key.record_usage()
                return Response({'message': 'DM-only alert resolved (no Item row)'})
            return Response({
                'message': 'No matching alert found to resolve'
            }, status=status.HTTP_404_NOT_FOUND)

        # Operator doctrine: alerts NEVER create Pulse tasks by default.
        # origin-ci (ORIG-11) is DM-only unless PULSE_ORIGIN_CI_PERSIST_ITEMS=true.
        from django.conf import settings as dj_settings
        if data.get('source') == 'origin-ci' and not getattr(
            dj_settings, 'PULSE_ORIGIN_CI_PERSIST_ITEMS', False
        ):
            from .tasks import _dm_alertmanager_firing
            result = _dm_alertmanager_firing(
                webhook_key,
                title=data.get('title') or 'Origin CI FAILED',
                description=data.get('description') or '',
                severity=data.get('severity') or 'error',
                labels=data.get('labels') or {},
                payload=data.get('payload') or {},
                source_url=data.get('source_url') or '',
                source_id=data.get('source_id') or data.get('title') or '',
            )
            webhook_key.record_usage()
            return Response({
                'message': f'origin-ci DM-only ({result})',
                'persist': False,
            }, status=status.HTTP_200_OK)

        # Create alert
        try:
            alert, is_new = AlertDeduplicationService.submit_alert(data)
        except AlertIdentityError as exc:
            webhook_key.record_usage()
            return Response({
                'error': 'alert_identity_error',
                'detail': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)
        webhook_key.record_usage()

        if is_new:
            from .tasks import send_alert_notification
            send_alert_notification.delay(alert.id)

            return Response({
                'message': 'Alert created',
                'item_id': alert.id,
                'fingerprint': alert.fingerprint
            }, status=status.HTTP_201_CREATED)
        else:
            return Response({
                'message': 'Alert deduplicated',
                'item_id': alert.id,
                'fingerprint': alert.fingerprint,
                'occurrence_count': alert.occurrence_count
            })


class GitHubWebhookView(APIView):
    """
    GitHub webhook endpoint — receives `pull_request` events and updates the
    PR state stored on linked tasks.

    POST /api/webhooks/github/

    Auth: X-Hub-Signature-256 header, HMAC-SHA256 over raw body with
    `GITHUB_WEBHOOK_SECRET` env var. GitHub standard.

    Event handling:
      - pull_request (action=opened|closed|reopened|edited|synchronize)
        → find tasks whose metadata.pull_requests[].url matches the PR HTML URL
        → update entry's `status` field to `open` / `merged` / `closed`
        → when action=closed with merged=true and task is at awaiting_merge,
          optionally auto-fire the Merge transition (controlled by
          GITHUB_WEBHOOK_AUTO_MERGE_TRANSITION env flag, default OFF).

    See Jira's "Update Issue Field" automation + GitHub integration pattern:
    webhook receiver matches linked PR URL to items, updates metadata, and
    optionally triggers a workflow transition when the merge event arrives.
    """
    permission_classes = [permissions.AllowAny]
    # Disable DRF parsing — we need the raw body for signature verification
    parser_classes = []  # type: ignore[assignment]

    def post(self, request):
        import hmac
        import hashlib
        import os
        import json as _json

        raw_body = request.body or b''
        secret = os.environ.get('GITHUB_WEBHOOK_SECRET', '').encode()
        signature_header = request.headers.get('X-Hub-Signature-256', '')

        if secret:
            if not signature_header.startswith('sha256='):
                return Response({'error': 'Missing or malformed signature'},
                                status=status.HTTP_401_UNAUTHORIZED)
            expected = 'sha256=' + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature_header):
                return Response({'error': 'Bad signature'},
                                status=status.HTTP_401_UNAUTHORIZED)
        # If secret is not configured, accept without auth but log a warning.
        # Operators must set GITHUB_WEBHOOK_SECRET in production.
        else:
            logger.warning('GitHub webhook hit without GITHUB_WEBHOOK_SECRET configured — '
                           'accepting unauthenticated. Set the env var for production.')

        event = request.headers.get('X-GitHub-Event', '')
        if event == 'ping':
            return Response({'ok': True, 'msg': 'pong'})
        if event != 'pull_request':
            return Response({'ok': True, 'ignored': event})

        try:
            payload = _json.loads(raw_body.decode('utf-8'))
        except (UnicodeDecodeError, _json.JSONDecodeError) as e:
            return Response({'error': f'Invalid JSON: {e}'},
                            status=status.HTTP_400_BAD_REQUEST)

        action = payload.get('action') or ''
        pr = payload.get('pull_request') or {}
        pr_html_url = (pr.get('html_url') or '').rstrip('/')
        pr_merged = bool(pr.get('merged'))
        pr_state = (pr.get('state') or '').lower()  # 'open' or 'closed'
        pr_head_sha = ((pr.get('head') or {}).get('sha') or '').strip()
        pr_merge_commit_sha = (pr.get('merge_commit_sha') or '').strip()
        if not pr_html_url:
            return Response({'error': 'No pull_request.html_url in payload'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Determine the new status value we want on the stored PR entry.
        if pr_merged:
            new_pr_status = 'merged'
        elif pr_state == 'closed':
            new_pr_status = 'closed'
        else:
            new_pr_status = 'open'

        # Find all items that have this PR linked.
        # metadata.pull_requests is a JSON list — we iterate candidate items
        # filtered by a cheap JSON-contains query, then match exactly in Python.
        matched_ids = []
        candidates = Item.objects.filter(metadata__pull_requests__isnull=False)
        # Actor for activity rows: merger's GitHub login if available, else
        # 'github-webhook'. Webhooks don't carry a Shizuha identity so we use
        # user_id=0 and surface the login in user_email for display.
        actor_login = (pr.get('merged_by') or {}).get('login') or (pr.get('user') or {}).get('login') or ''
        actor_email = f"{actor_login}@users.noreply.github.com" if actor_login else 'github-webhook@shizuha.local'
        for item in candidates.only('id', 'item_key', 'status', 'workflow_name', 'metadata'):
            prs = (item.metadata or {}).get('pull_requests') or []
            changed_entries = []
            for entry in prs:
                if (entry.get('url') or '').rstrip('/') == pr_html_url:
                    old_status = entry.get('status') or 'unknown'
                    old_head_sha = (entry.get('head_sha') or '').strip()
                    old_merge_commit_sha = (entry.get('merge_commit_sha') or '').strip()
                    status_changed = entry.get('status') != new_pr_status
                    head_changed = bool(pr_head_sha) and old_head_sha != pr_head_sha
                    merge_commit_changed = (
                        new_pr_status == 'merged'
                        and bool(pr_merge_commit_sha)
                        and old_merge_commit_sha != pr_merge_commit_sha
                    )
                    if status_changed or head_changed or merge_commit_changed:
                        entry['status'] = new_pr_status
                        if pr_head_sha:
                            entry['head_sha'] = pr_head_sha
                        entry['last_synced_at'] = __import__('django.utils.timezone',
                                                             fromlist=['now']).now().isoformat()
                        if new_pr_status == 'merged':
                            entry['merged_at'] = pr.get('merged_at')
                            entry['merged_by'] = (pr.get('merged_by') or {}).get('login')
                            if pr_merge_commit_sha:
                                entry['merge_commit_sha'] = pr_merge_commit_sha
                        changed_entries.append((
                            old_status, old_head_sha, old_merge_commit_sha, entry,
                            status_changed, head_changed, merge_commit_changed,
                        ))
            if changed_entries:
                # Overwrite metadata.pull_requests (already mutated in place).
                # .update() bypasses signals on purpose — we log the activity
                # rows explicitly below so history shows webhook-driven state
                # flips (opened/merged/closed).
                Item.objects.filter(pk=item.pk).update(metadata=item.metadata)
                matched_ids.append(item.item_key or str(item.pk))
                for (
                    old_status, old_head_sha, old_merge_commit_sha, entry,
                    status_changed, head_changed, merge_commit_changed,
                ) in changed_entries:
                    number = entry.get('number')
                    repo = entry.get('repo')
                    label = f"#{number} {repo}" if (number and repo) else (f"#{number}" if number else (entry.get('url') or ''))
                    if status_changed:
                        old_value = f"{label} — {old_status}"
                        new_value = f"{label} — {new_pr_status}"
                    elif head_changed:
                        old_value = f"{label} — head {old_head_sha or 'unknown'}"
                        new_value = f"{label} — head {entry.get('head_sha')}"
                    else:
                        old_value = f"{label} — merge {old_merge_commit_sha or 'unknown'}"
                        new_value = f"{label} — merge {entry.get('merge_commit_sha')}"
                    ItemActivity.objects.create(
                        item_id=item.pk,
                        action='pr_status_changed',
                        field_name='Pull Request',
                        old_value=old_value,
                        new_value=new_value,
                        user_id=0,
                        user_email=actor_email[:254],
                    )
                if (
                    new_pr_status == 'merged'
                    and pr_merge_commit_sha
                    and any(row[4] or row[6] for row in changed_entries)
                ):
                    # PLS-521 invalidation producer: a newer exact merge commit
                    # invalidates every prior deployment/monitoring snapshot.
                    # If the Security-reviewed manifest does not exist yet this
                    # is a harmless no-op; manifest creation starts generation 1.
                    try:
                        if VerificationRequirementManifest.objects.filter(
                            item=item, is_current=True,
                        ).exists():
                            from .services.deployment_evidence import invalidate_evidence
                            invalidate_evidence(
                                item=item,
                                reason='newer_merge',
                                actor_email=actor_email,
                            )
                    except Exception:
                        logger.exception(
                            'PLS-521 failed to invalidate deployment evidence after merge for %s',
                            item.item_key or item.pk,
                        )
                if new_pr_status == 'merged':
                    try:
                        from .services.focused_qa import ensure_focused_qa_for_item
                        ensure_focused_qa_for_item(item, trigger='pr_merged')
                    except Exception:
                        logger.warning("PLS-184 focused QA generation failed after PR merge webhook", exc_info=True)
                    try:
                        from .services.recurring_cycle_close import close_recurring_cycle_if_complete
                        close_recurring_cycle_if_complete(
                            item,
                            trigger_kind='PR',
                            trigger_label=pr_html_url,
                            actor_email=actor_email,
                            # PLS-855: bind the merged PR to the generation whose
                            # window contains its merge date so a late/stale merge
                            # cannot close the CURRENT cycle.
                            trigger_at=pr.get('merged_at'),
                        )
                    except Exception as exc:
                        try:
                            from .services.recurring_cycle_close import alert_recurring_cycle_auto_close_failure
                            alert_recurring_cycle_auto_close_failure(
                                item,
                                context='github_pr_merge_webhook',
                                exc=exc,
                            )
                        except Exception:
                            logger.exception("PLAT-1353 recurring-cycle fail-loud path failed after PR merge webhook")
                        logger.warning("PLAT-1353 recurring-cycle auto-close failed after PR merge webhook", exc_info=True)

        # PLS-514: source a routing work item from the PR itself.
        #
        # Everything above only touches tasks that ALREADY link this PR. A PR
        # with no linked task — an on-call fix answering an alert, since alerts
        # are DM-only and never create tasks — falls straight through, and so
        # does one whose task sits on a workflow with no transition to
        # in_review. In both cases the author cannot route it however diligent
        # they are, so no author-side prompt reaches them; the item has to come
        # from the PR. Default OFF and repo-allowlisted (see pr_routing).
        pr_routing_item = None
        try:
            from .services.pr_routing import ensure_pr_routing_item
            repo_full = ((payload.get('repository') or {}).get('full_name') or '').strip()
            pr_number = pr.get('number')
            if repo_full and pr_number:
                routed = ensure_pr_routing_item(
                    repo=repo_full,
                    number=int(pr_number),
                    url=pr_html_url,
                    title=pr.get('title') or '',
                    draft=bool(pr.get('draft')),
                    state=pr_state,
                    merged=pr_merged,
                )
                if routed is not None:
                    pr_routing_item = routed.item_key or str(routed.pk)
        except Exception:
            # A routing-item failure must never fail the webhook — the PR-state
            # sync above is the load-bearing half and has already been written.
            logger.warning('PLS-514 PR routing item failed for %s', pr_html_url, exc_info=True)

        result = {
            'ok': True,
            'event': event,
            'action': action,
            'pr_url': pr_html_url,
            'new_pr_status': new_pr_status,
            'pr_routing_item': pr_routing_item,
            'items_updated': matched_ids,
        }

        # Optional auto-transition: when a PR is merged, auto-fire "Merge" on any
        # linked task currently at awaiting_merge. Off by default; opt-in via env.
        if (pr_merged and
                os.environ.get('GITHUB_WEBHOOK_AUTO_MERGE_TRANSITION', '').lower() in
                ('1', 'true', 'yes', 'on')):
            try:
                from .services.workflow_service import WorkflowService
                from django.contrib.auth.models import User
                # Use a system-user attribution for the auto-transition.
                sys_user = (User.objects.filter(email='system@shizuha.com').first()
                            or User.objects.filter(is_superuser=True).first())
                auto_transitioned = []
                for key in matched_ids:
                    try:
                        item = Item.objects.get(item_key=key)
                    except Item.DoesNotExist:
                        continue
                    # PLS-675 / PLAT-1351 v4: shared merge-event contract. Merge
                    # completion is NOT deployment verification. deployed/live →
                    # non-terminal deploy stage; code → done. Fail-loud on error.
                    target = WorkflowService.apply_merge_event_transition(
                        item,
                        user=sys_user,
                        pr_html_url=pr_html_url,
                        merged_by=(pr.get('merged_by') or {}).get('login', ''),
                    )
                    if target:
                        auto_transitioned.append(key)
                result['auto_transitioned'] = auto_transitioned
            except Exception as e:
                logger.warning(f'Auto-Merge transition wrapper failed: {e}')

        return Response(result)


class WebhookKeyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing user webhook keys.
    Users can only see and manage their OWN webhook keys.
    """
    queryset = WebhookKey.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import WebhookKeySerializer, WebhookKeyCreateSerializer
        if self.action == 'create':
            return WebhookKeyCreateSerializer
        return WebhookKeySerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_staff:
            queryset = WebhookKey.objects.all()
            user_id = self.request.query_params.get('user_id')
            if user_id:
                queryset = queryset.filter(user_id=user_id)
            return queryset
        return WebhookKey.objects.filter(user_id=user.id)

    def perform_create(self, serializer):
        serializer.save(
            user_id=self.request.user.id,
            user_email=getattr(self.request.user, 'email', None)
        )

    def create(self, request, *args, **kwargs):
        """Override create to return full serialized response with key and URLs."""
        from .serializers import WebhookKeySerializer
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        # Return the created object with the read serializer (includes key, URLs, etc.)
        read_serializer = WebhookKeySerializer(serializer.instance, context={'request': request})
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def regenerate(self, request, pk=None):
        """Regenerate the webhook key (invalidates old URLs)."""
        webhook_key = self.get_object()
        old_key = webhook_key.key
        webhook_key.key = WebhookKey.generate_key()
        webhook_key.save()

        return Response({
            'message': 'Webhook key regenerated',
            'old_key': old_key[:8] + '...',
            'new_key': webhook_key.key
        })

    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle the webhook key's active status."""
        webhook_key = self.get_object()
        webhook_key.is_active = not webhook_key.is_active
        webhook_key.save(update_fields=['is_active', 'updated_at'])

        return Response({
            'id': webhook_key.id,
            'name': webhook_key.name,
            'is_active': webhook_key.is_active
        })

    @action(detail=False, methods=['get'])
    def webhook_info(self, request):
        """Get webhook URL templates and setup instructions."""
        base_url = request.build_absolute_uri('/').rstrip('/')

        return Response({
            'grafana': {
                'url_template': f'{base_url}/pulse/api/webhooks/grafana/<YOUR_KEY>/',
                'method': 'POST',
                'description': 'Use this URL as the Grafana webhook contact point',
                'settings': {
                    'http_method': 'POST',
                    'username': '(leave empty)',
                    'password': '(leave empty)',
                    'max_alerts': 0,  # 0 = unlimited
                    'disable_resolve_message': False  # Keep enabled to auto-resolve
                }
            },
            'alertmanager': {
                'url_template': f'{base_url}/pulse/api/webhooks/alertmanager/<YOUR_KEY>/',
                'method': 'POST',
                'description': 'Use this URL as a Prometheus Alertmanager webhook receiver'
            },
            'generic': {
                'url_template': f'{base_url}/pulse/api/webhooks/generic/<YOUR_KEY>/',
                'method': 'POST',
                'description': 'Generic webhook for custom integrations',
                'payload_format': {
                    'title': 'string (required)',
                    'description': 'string',
                    'source': 'string (required)',
                    'severity': 'info|warning|error|critical',
                    'labels': 'object',
                    'payload': 'object',
                    'status': 'firing|resolved'
                }
            }
        })


# =============================================================================
# Action-specific API Views
# =============================================================================

class ActionSubmitView(APIView):
    """
    Submit a new action.
    POST /api/actions/submit/

    This endpoint is public to allow external systems to submit actions
    for approval workflows.
    """
    permission_classes = [permissions.AllowAny]  # Public for external systems

    def post(self, request):
        serializer = ActionSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        item_type = data.get('item_type', 'action.generic')
        if not item_type.startswith('action.'):
            item_type = f'action.{item_type}'

        action = Item.objects.create(
            mode='action',
            item_type=item_type,
            title=data['title'],
            description=data.get('description', ''),
            source=data['source'],
            source_id=data.get('source_id'),
            priority=data.get('priority', 'normal'),
            payload=data.get('payload', {}),
            labels=data.get('labels', {}),
            callback_url=data.get('callback_url'),
            callback_method=data.get('callback_method', 'POST'),
            callback_headers=data.get('callback_headers', {}),
            requires_approval=data.get('requires_approval', True),
            due_date=data.get('due_date'),
            status='pending'
        )

        # TODO: Check approval rules for auto-approve

        return Response({
            'message': 'Action submitted',
            'item_id': action.id,
            'status': action.status,
            'requires_approval': action.requires_approval
        }, status=status.HTTP_201_CREATED)


# =============================================================================
# Rule ViewSets
# =============================================================================

class ApprovalRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for ApprovalRule management. Requires authentication."""
    queryset = ApprovalRule.objects.all()
    serializer_class = ApprovalRuleSerializer
    # Uses default permission_classes from settings (IsAuthenticated)

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test a rule against sample data."""
        rule = self.get_object()
        # TODO: Implement rule testing logic
        return Response({'message': 'Rule testing not yet implemented'})


class SilenceRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for SilenceRule management. Requires authentication."""
    queryset = SilenceRule.objects.all()
    serializer_class = SilenceRuleSerializer
    # Uses default permission_classes from settings (IsAuthenticated)

    @action(detail=False, methods=['get'])
    def active(self, request):
        """Get currently active silence rules."""
        now = timezone.now()
        rules = SilenceRule.objects.filter(is_active=True).filter(
            Q(starts_at__isnull=True) | Q(starts_at__lte=now)
        ).filter(
            Q(ends_at__isnull=True) | Q(ends_at__gt=now)
        )
        serializer = SilenceRuleSerializer(rules, many=True)
        return Response({
            'count': rules.count(),
            'rules': serializer.data
        })

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test a silence rule against an item."""
        rule = self.get_object()
        item_id = request.data.get('item_id')

        if not item_id:
            return Response({'error': 'item_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            item = Item.objects.get(id=item_id)
            matches = SilencingService._rule_matches(rule, item)
            return Response({
                'matches': matches,
                'rule_name': rule.name,
                'item_id': item_id
            })
        except Item.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)


class EscalationRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for EscalationRule management. Requires authentication."""
    queryset = EscalationRule.objects.all()
    serializer_class = EscalationRuleSerializer
    # Uses default permission_classes from settings (IsAuthenticated)


class InhibitionRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for InhibitionRule management. Requires authentication."""
    queryset = InhibitionRule.objects.all()
    serializer_class = InhibitionRuleSerializer
    # Uses default permission_classes from settings (IsAuthenticated)

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test an inhibition rule."""
        rule = self.get_object()
        # TODO: Implement rule testing logic
        return Response({'message': 'Inhibition rule testing not yet implemented'})


class SubscriptionViewSet(viewsets.ModelViewSet):
    """ViewSet for Subscription management. Requires authentication."""
    queryset = Subscription.objects.all()
    serializer_class = SubscriptionSerializer
    # Uses default permission_classes from settings (IsAuthenticated)


class FingerprintConfigViewSet(viewsets.ModelViewSet):
    """ViewSet for FingerprintConfig management. Requires authentication."""
    queryset = FingerprintConfig.objects.all()
    serializer_class = FingerprintConfigSerializer
    lookup_field = 'source'
    # Uses default permission_classes from settings (IsAuthenticated)


# =============================================================================
# User Notification Channel ViewSet
# =============================================================================

class UserNotificationChannelViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing user notification channels (Discord, Telegram).
    Users can only see and manage their OWN notification channels.
    Requires authentication.

    Security:
    - IsAuthenticated: All requests require valid JWT token
    - IsOwnerOrStaffForChannels: Object-level permission ensures users can only
      access/modify their own notification channels unless they are staff
    """
    queryset = UserNotificationChannel.objects.all()
    serializer_class = UserNotificationChannelSerializer
    # IsAuthenticated is inherited from settings, IsOwnerOrStaffForChannels adds object-level checks
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrStaffForChannels]

    def get_serializer_class(self):
        if self.action == 'create':
            return UserNotificationChannelCreateSerializer
        return UserNotificationChannelSerializer

    def get_queryset(self):
        """
        Filter queryset to only show the authenticated user's channels.
        Users can only see their own notification channels.
        Staff users can see all channels.
        """
        user = self.request.user

        # Staff can see all channels (for admin purposes)
        if user.is_staff:
            queryset = UserNotificationChannel.objects.all()
            # Allow staff to filter by user_id
            user_id = self.request.query_params.get('user_id')
            if user_id:
                queryset = queryset.filter(user_id=user_id)
        else:
            # Regular users only see their own channels
            queryset = UserNotificationChannel.objects.filter(user_id=user.id)

        # Filter by channel_type
        channel_type = self.request.query_params.get('channel_type')
        if channel_type:
            queryset = queryset.filter(channel_type=channel_type)

        # Filter by is_active
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        return queryset

    def perform_create(self, serializer):
        """Automatically set user_id to the authenticated user."""
        serializer.save(user_id=self.request.user.id, user_email=self.request.user.email)

    @action(detail=False, methods=['get'])
    def by_user(self, request):
        """
        Get all notification channels for a specific user.
        Regular users can only query their own channels.
        Staff can query any user's channels.
        """
        user_id = request.query_params.get('user_id')

        # If no user_id provided, use current user
        if not user_id:
            user_id = request.user.id

        # Non-staff can only see their own channels
        if not request.user.is_staff and int(user_id) != request.user.id:
            return Response(
                {'error': 'You can only view your own notification channels'},
                status=status.HTTP_403_FORBIDDEN
            )

        channels = UserNotificationChannel.objects.filter(user_id=user_id)
        serializer = UserNotificationChannelSerializer(channels, many=True)
        return Response({
            'user_id': int(user_id),
            'count': channels.count(),
            'channels': serializer.data
        })

    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test a notification channel by sending a test message."""
        channel = self.get_object()

        serializer = TestNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        test_message = serializer.validated_data.get('message')

        from tasks.services.notifications import notification_service

        success = False
        error = ''

        if channel.channel_type == 'discord':
            success, error = notification_service.discord.send(
                channel.discord_webhook_url,
                test_message,
                notification_service.discord.create_embed(
                    title="🧪 Test Notification",
                    description=test_message,
                    color=notification_service.COLORS['info'],
                    footer='Shizuha Hub - Test'
                )
            )
        elif channel.channel_type == 'telegram':
            formatted = f"<b>🧪 Test Notification</b>\n\n{test_message}"
            success, error = notification_service.telegram.send(
                channel.telegram_bot_token,
                channel.telegram_chat_id,
                formatted
            )

        # Update channel stats
        channel.last_notification_at = timezone.now()
        if success:
            channel.error_count = 0
            channel.last_error = ''
        else:
            channel.error_count += 1
            channel.last_error = error
        channel.save()

        if success:
            return Response({
                'success': True,
                'message': f'Test notification sent successfully to {channel.name}'
            })
        else:
            return Response({
                'success': False,
                'error': error
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle a notification channel's active status."""
        channel = self.get_object()
        channel.is_active = not channel.is_active
        channel.save(update_fields=['is_active', 'updated_at'])

        return Response({
            'id': channel.id,
            'name': channel.name,
            'is_active': channel.is_active,
            'message': f'Channel {"enabled" if channel.is_active else "disabled"}'
        })


# =============================================================================
# Task Bulk Operations
# =============================================================================

def get_user_scoped_items(user, item_ids):
    """
    Get items scoped to the current user.
    Users can only modify items assigned to them or created by them.
    Staff can modify any item.
    """
    if user.is_staff:
        qs = Item.objects.filter(id__in=item_ids)
    else:
        qs = Item.objects.filter(
            id__in=item_ids
        ).filter(
            Q(assignee_id=user.id) | Q(created_by_id=user.id)
        )
    # PLS-7: the bulk task endpoints (bulk-complete/update/delete) share this
    # scope, so they must apply the same SOFT-archived (deleted) project filter
    # that ItemViewSet.get_queryset() does — otherwise a caller with cached task
    # IDs (or any staff user) could still mutate tasks hidden in a deleted
    # project. Personal (null-project) items are unaffected: project__is_archived
    # is NULL for them, so exclude() does not drop them.
    return qs.exclude(project__is_archived=True)


def _routed_default_project_id(webhook_key):
    """PLS-7: resolve a webhook key's default routing project, skipping a
    SOFT-archived (deleted) project.

    The legacy DELETE / bulk-delete paths soft-archive a Project (its items are
    then hidden by the archived-project filter). An active webhook with a cached
    ``default_project`` would otherwise keep creating alerts that are immediately
    invisible. Returns the key's ``default_project_id`` only when that project is
    not archived; ``None`` otherwise (routing then falls back to project-less),
    and it auto-resumes if the project is later un-archived.
    """
    pid = webhook_key.default_project_id
    if pid and Project.objects.filter(id=pid, is_archived=True).exists():
        return None
    return pid


def _label_assignment_group(labels: dict, fallback, *, project=None) -> str | None:
    """Prefer an owner label only when it names this project's exact Team.

    Alertmanager/Grafana alert labels may carry owner_group=devops (or similar)
    to declare the owning team. Ignoring that label and routing everything to the
    webhook key's static default sends devops alerts to whoever holds the key
    default (e.g. architecture/Hiro) — the PLS-122 misrouting. Use owner_group
    when present and valid; fall back to the key default otherwise. A missing
    project has no organization boundary and therefore cannot select a Team.
    """
    from .services.workflow_service import WorkflowService
    if project is None:
        return None
    probe = Item(project=project)
    for candidate in (labels.get('owner_group'), fallback):
        canonical = WorkflowService._canonical_team_name(candidate)
        if canonical and WorkflowService._team_registered_for_item(canonical, probe):
            return canonical
    return None


class TaskBulkCompleteView(APIView):
    """
    Complete multiple tasks at once.
    POST /api/tasks/bulk-complete/
    Requires authentication. Users can only complete their own tasks.
    """
    # Uses default permission_classes from settings (IsAuthenticated)

    def post(self, request):
        item_ids = request.data.get('item_ids', [])

        if not item_ids:
            return Response(
                {'error': 'item_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Scope to user's items only
        queryset = get_user_scoped_items(request.user, item_ids).filter(
            mode='task',
            status__in=['pending', 'in_progress']
        )

        count = 0
        completed_ids = []
        for task in queryset:
            task.mark_completed()
            count += 1
            completed_ids.append(task.id)
            logger.info(f"Bulk completed task {task.id} by user {request.user.id}")

        return Response({
            'message': f'Completed {count} tasks',
            'count': count,
            'completed_ids': completed_ids
        })


class TaskBulkUpdateView(APIView):
    """
    Update status and/or priority for multiple tasks.
    POST /api/tasks/bulk-update/
    Requires authentication. Users can only update their own tasks.
    """
    # Uses default permission_classes from settings (IsAuthenticated)

    def post(self, request):
        item_ids = request.data.get('item_ids', [])
        new_status = request.data.get('status')
        new_priority = request.data.get('priority')

        if not item_ids:
            return Response(
                {'error': 'item_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not new_status and not new_priority:
            return Response(
                {'error': 'At least one of status or priority is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate status
        valid_statuses = ['pending', 'in_progress', 'completed']
        if new_status and new_status not in valid_statuses:
            return Response(
                {'error': f'Invalid status. Must be one of: {valid_statuses}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate priority
        valid_priorities = ['low', 'normal', 'high', 'urgent']
        if new_priority and new_priority not in valid_priorities:
            return Response(
                {'error': f'Invalid priority. Must be one of: {valid_priorities}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Scope to user's items only
        queryset = get_user_scoped_items(request.user, item_ids).filter(mode='task')

        count = 0
        updated_ids = []
        for task in queryset:
            if new_status:
                if new_status == 'completed':
                    task.mark_completed()
                else:
                    task.status = new_status
            if new_priority:
                task.priority = new_priority
            task.save()
            count += 1
            updated_ids.append(task.id)

        return Response({
            'message': f'Updated {count} tasks',
            'count': count,
            'updated_ids': updated_ids,
            'updates': {
                'status': new_status,
                'priority': new_priority
            }
        })


class TaskBulkDeleteView(APIView):
    """
    Delete multiple tasks at once.
    POST /api/tasks/bulk-delete/
    Requires authentication. Users can only delete their own tasks.
    """
    # Uses default permission_classes from settings (IsAuthenticated)

    def post(self, request):
        item_ids = request.data.get('item_ids', [])

        if not item_ids:
            return Response(
                {'error': 'item_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Scope to user's items only
        queryset = get_user_scoped_items(request.user, item_ids).filter(mode='task')

        count = queryset.count()
        deleted_ids = list(queryset.values_list('id', flat=True))

        queryset.delete()
        logger.info(f"Bulk deleted {count} tasks by user {request.user.id}: {deleted_ids}")

        return Response({
            'message': f'Deleted {count} tasks',
            'count': count,
            'deleted_ids': deleted_ids
        })


class ProjectBulkDeleteView(APIView):
    """
    Delete multiple projects at once.
    POST /api/projects/bulk-delete/
    Requires authentication. Only projects the user can manage are deleted.
    """

    def post(self, request):
        project_ids = request.data.get('project_ids', [])

        if not project_ids:
            return Response(
                {'error': 'project_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        projects = Project.objects.filter(id__in=project_ids, is_active=True)
        archived_ids = []
        skipped = []

        for project in projects:
            if self._can_manage_project(request.user, project):
                archived_ids.append(project.id)
            else:
                skipped.append({'id': project.id, 'reason': 'Permission denied'})

        if archived_ids:
            # PLS-7 #1: SOFT-archive instead of permanent delete. The previous
            # unguarded bulk `Project...delete()` permanently destroyed live
            # projects (cascading all items) with no archive-first or confirmation,
            # bypassing the hard_delete guards. Permanent deletion is now only via
            # the per-project guarded hard-delete endpoint; this archives the
            # manageable set (reversible via unarchive).
            Project.objects.filter(id__in=archived_ids).update(
                is_archived=True, archived_at=timezone.now(),
            )
            logger.info(f"Bulk archived (soft-delete) {len(archived_ids)} projects by user {request.user.id}: {archived_ids}")

        return Response({
            # NOTE: this endpoint now soft-archives (not permanent-deletes). Keys
            # kept for backward compat; `deleted_ids` == the archived set.
            'deleted_count': len(archived_ids),
            'deleted_ids': archived_ids,
            'archived_ids': archived_ids,
            'soft_deleted': True,
            'skipped': skipped,
        })

    def _can_manage_project(self, user, project):
        """Check if user can manage this project."""
        from .services.permissions import PermissionService
        return PermissionService.can_access_project(
            getattr(user, 'id', None),
            project,
            permission='manage_project',
            user_email=getattr(user, 'email', None),
            user=user,
        )


# =============================================================================
# Organization Management - DEPRECATED
# Organization CRUD is now handled by shizuha-admin at /admin/api/organizations/
# This service only stores org_id references to organizations in shizuha-admin.
# =============================================================================


# =============================================================================
# Project ViewSet
# =============================================================================

class ProjectViewSet(viewsets.ModelViewSet):
    """
    ViewSet for project management.

    Users can:
    - List projects they have access to
    - Create new projects in organizations they're members of
    - View/update projects based on their membership and access level
    - Manage project members (project admins only)

    Note: Organizations are managed by shizuha-admin. Projects reference
    organizations via org_id field. Use /admin/api/organizations/ for org CRUD.
    """
    queryset = Project.objects.filter(is_active=True)
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated, ProjectPermission]

    def get_serializer_class(self):
        if self.action == 'create':
            return ProjectCreateSerializer
        return ProjectSerializer

    def get_queryset(self):
        """Filter to projects the user can access."""
        user = self.request.user

        # PLS-523: GET /api/projects/ is a SAFE, read-only method (RFC 9110) and
        # must NOT mutate membership rows. The previous read-time
        # `_resolve_pending_project_memberships()` write promoted an email-only
        # ProjectMembership to `user_id=U`; when a `ProjectMembership(project, U)`
        # already existed it raised IntegrityError on unique(project, user_id),
        # which `list()` converted to the seat-scoped 500 (tora / TRD orgs 3+186).
        # Because the write ran before org filtering, unscoped and every
        # org-scoped read failed identically. PermissionService.get_user_projects
        # already authorizes by user_id OR normalized user_email (L2), so
        # email-only rows stay visible without any write; membership linking
        # belongs to an explicit write/event lifecycle, not a list request.
        from .services.permissions import PermissionService
        # Pass the user object so PermissionService reads the tenant-scoped
        # organization_memberships claim directly (same layered resolver as
        # items: L0 superuser → L1 org admin → L2 project membership → L2b
        # org member in open projects). Pre-refactor this shortcut skipped
        # L0 + L1 for non-staff org admins, which is why Shizuha hit a
        # silent 403 on /api/projects/SM/ despite being org-1 admin.
        queryset = PermissionService.get_user_projects(user=user)

        # Filter by organization (using org_id)
        #
        # PLS-605: this previously read `?organization=` directly and passed the
        # raw string to `.filter(org_id=...)`, so a non-numeric value reached the
        # ORM and raised, surfacing as HTTP 500 via PLS-358's fail-loud wrapper.
        # It also honoured ONLY `?organization=`, silently ignoring the documented
        # `X-Organization-ID` header and `?organization_id=` — so a caller using
        # the documented header believed it had scoped the request and received
        # another org's projects with a 200.
        #
        # `_explicit_org_id` fixes both: it resolves all three documented
        # selectors and fails closed (400 'Invalid organization scope.' per
        # PLS-670/ADR-0054) on an unparseable value instead of handing it to
        # the ORM.
        #
        # Deliberately `_explicit_org_id` and NOT `_ensure_explicit_org_membership`
        # here. The membership variant additionally returns 403 for a well-formed
        # request naming an org you don't belong to, where this endpoint currently
        # returns 200 with zero rows (PermissionService.get_user_projects has
        # already scoped the queryset, so nothing leaks either way). PLS-605's
        # acceptance requires "no behaviour change for well-formed ?organization=
        # requests", and 200-empty -> 403 would be exactly that. Full 403 parity
        # with items/boards belongs to the uniform PLS-439 selector port, applied
        # to every route at once rather than to this one route ahead of the rest.
        org_id = _explicit_org_id(self.request)
        org_slug = self.request.query_params.get('organization_slug')
        # `is not None`, NOT truthiness — see the ObjectiveViewSet note. Here the
        # falsy-zero bug is worse than a dropped filter: `?organization=0` would
        # fall through to `elif org_slug`, so a contradictory lower-precedence
        # selector could answer a request that named an explicit numeric org
        # (revi P1). Explicit selector precedence must not depend on the value.
        if org_id is not None:
            queryset = queryset.filter(org_id=org_id)
        elif org_slug:
            # Lookup org by slug from shizuha-admin
            from .services.admin_service import admin_service
            org_data = admin_service.get_organization_by_slug(org_slug)
            if org_data:
                queryset = queryset.filter(org_id=org_data['id'])
            else:
                queryset = queryset.none()

        # Filter by archived status - allow unarchive action to see archived projects
        include_archived = self.request.query_params.get('include_archived', 'false')
        if include_archived.lower() != 'true' and self.action not in ('unarchive', 'hard_delete'):
            queryset = queryset.filter(is_archived=False)

        queryset = queryset.annotate(
            _member_count=Count('memberships', filter=Q(memberships__is_active=True), distinct=True),
            _item_count=Count('items', distinct=True),
        )

        # PLS-574: ProjectSerializer exposes the caller's effective can_create
        # bit so control-plane clients can distinguish visible/read-only projects
        # from writable ones without an N+1 membership lookup.
        from django.db.models import Prefetch
        from .services.permissions import _email_variant_q, _is_valid_int_id
        is_valid_id, integer_user_id = _is_valid_int_id(getattr(user, 'id', None))
        identity_q = Q(user_id=integer_user_id) if is_valid_id else None
        email_q = _email_variant_q(('user_email',), getattr(user, 'email', None))
        if email_q is not None:
            identity_q = email_q if identity_q is None else identity_q | email_q
        request_memberships = ProjectMembership.objects.filter(is_active=True)
        if identity_q is None:
            request_memberships = request_memberships.none()
        else:
            request_memberships = request_memberships.filter(identity_q)
        return queryset.prefetch_related(Prefetch(
            'memberships',
            queryset=request_memberships,
            to_attr='_request_project_memberships',
        ))

    def list(self, request, *args, **kwargs):
        """List projects with fail-loud markers for the recurring PLS-280 class."""
        try:
            return super().list(request, *args, **kwargs)
        except OperationalError:
            logger.exception(
                '%s layer=db/query endpoint=/api/projects/ action=list',
                PROJECT_LIST_DB_ERROR_MARKER,
            )
            return Response(
                {
                    'detail': 'Pulse project list failed at the database/query layer.',
                    'code': 'projects_db_operational_error',
                    'marker': PROJECT_LIST_DB_ERROR_MARKER,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except APIException:
            raise
        except Exception:
            logger.exception(
                '%s layer=unexpected endpoint=/api/projects/ action=list',
                PROJECT_LIST_UNEXPECTED_ERROR_MARKER,
            )
            return Response(
                {
                    'detail': 'Pulse project list failed unexpectedly.',
                    'code': 'projects_unexpected_error',
                    'marker': PROJECT_LIST_UNEXPECTED_ERROR_MARKER,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def perform_create(self, serializer):
        """Create project with membership (atomic transaction)."""
        # Verify user has access to the organization via shizuha-admin
        org_id = serializer.validated_data['org_id']
        if not self.request.user.is_staff:
            from .services.admin_service import admin_service
            if not admin_service.is_org_member(self.request.user.id, org_id, self.request.user.email):
                raise drf_serializers.ValidationError({
                    'org_id': 'You are not a member of this organization'
                })

        # Get user email - check if it's a fallback email from FederatedUser
        user_email = self.request.user.email
        has_real_email = getattr(self.request.user, 'has_real_email', True)
        if not has_real_email:
            logger.warning(
                f"Creating project with fallback email for user {self.request.user.id}. "
                f"JWT may be missing email claim. Email used: {user_email}"
            )

        with transaction.atomic():
            project = serializer.save(created_by_id=self.request.user.id)

            # Add creator as project admin
            ProjectMembership.objects.create(
                project=project,
                user_id=self.request.user.id,
                user_email=user_email,
                role='admin',
                can_browse=True,
                can_create=True,
                can_edit=True,
                can_delete=True,
                can_manage_project=True,
                can_manage_members=True,
                grant_source='project_create',
                grant_event_id=uuid.uuid4(),
                grant_issuer_email=user_email,
                authorization_version=1,
            )
        logger.info(f"Project created: {project.key} in org_id={org_id} by user {self.request.user.id}")

    def create(self, request, *args, **kwargs):
        """Override create to return full ProjectSerializer with id."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Return full project data using ProjectSerializer
        response_serializer = ProjectSerializer(serializer.instance)
        headers = self.get_success_headers(response_serializer.data)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
            headers=headers
        )

    @action(detail=True, methods=['get', 'post'])
    def members(self, request, pk=None):
        """List or add project members."""
        project = self.get_object()

        if request.method == 'GET':
            from .services.permissions import PermissionService
            memberships = PermissionService.get_valid_project_memberships(
                project,
            )
            # PLS-832: collapse duplicate identity edges before list/count.
            deduped = PermissionService.dedupe_project_memberships(memberships)
            serializer = ProjectMembershipSerializer(deduped, many=True)
            return Response({
                'count': len(deduped),
                'members': serializer.data
            })

        elif request.method == 'POST':
            # Check permission - project member managers and org admins/owners can add members
            if not self._can_manage_members(request.user, project):
                return Response(
                    {'error': 'You do not have permission to add members'},
                    status=status.HTTP_403_FORBIDDEN
                )

            serializer = ProjectMemberAddSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            user_id = serializer.validated_data.get('user_id') or None
            user_email = serializer.validated_data.get('user_email', '')
            role = serializer.validated_data['role']

            # Resolve user info to ensure we have both user_id and user_email
            if user_email and not user_id:
                user_info = user_service.get_user_from_id_service(user_email)
                if not user_info:
                    return Response(
                        {'error': f'User with email "{user_email}" does not exist'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # For real users, get their ID
                if not user_info.get('is_agent') and user_info.get('id'):
                    try:
                        user_id = int(user_info['id'])
                    except (ValueError, TypeError):
                        pass

            # Check if already a member (user_id, email, and alias variants —
            # PLS-832: prevent (user_id) + (email-only) duplicate edges).
            from .services.permissions import _email_variants
            existing = list(
                ProjectMembership.objects.filter(project=project, is_active=True)
            )
            candidate_emails = set(_email_variants(user_email)) if user_email else set()
            for row in existing:
                if user_id and row.user_id == user_id:
                    return Response(
                        {'error': 'User is already a member'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                row_emails = set(_email_variants(row.user_email))
                if candidate_emails and (candidate_emails & row_emails):
                    return Response(
                        {'error': 'User is already a member'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # Email-only existing row that will resolve to this user_id.
                if user_id and row.user_id is None and row_emails and candidate_emails:
                    if row_emails & candidate_emails:
                        return Response(
                            {'error': 'User is already a member'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

            # Verify user is org member via shizuha-admin
            from .services.admin_service import admin_service
            from .services.workflow_service import WorkflowService
            is_org_member = False
            if user_id:
                is_org_member = admin_service.is_org_member(user_id, project.org_id, user_email)
            elif user_email:
                # Email-only identities (including agents) remain tenant-bound.
                # An address alone is not authority to join a project.
                aliases = WorkflowService._agent_email_aliases(user_email)
                org_members = admin_service.get_org_members(
                    project.org_id,
                    search=user_email,
                ) or []
                is_org_member = any(
                    aliases & WorkflowService._agent_email_aliases(
                        member.get('user_email') or member.get('email') or ''
                    )
                    for member in org_members
                    if isinstance(member, dict)
                )

            if not is_org_member:
                return Response(
                    {'error': 'User must be an organization member first'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Create membership
            new_membership = ProjectMembership.objects.create(
                project=project,
                user_id=user_id,
                user_email=user_email,
                role=role,
                grant_source='membership_api',
                grant_event_id=uuid.uuid4(),
                grant_issuer_email=getattr(request.user, 'email', '') or '',
                authorization_version=1,
            )

            logger.info(f"Project member added: user_id={user_id}, email={user_email} to {project.key}")
            return Response({
                'message': 'Member added',
                'membership': ProjectMembershipSerializer(new_membership).data
            }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='org-members')
    def org_members(self, request, pk=None):
        """
        Get organization members for a project, with optional search.

        Query params:
            ?search=email_or_name - Filter by email or name

        Returns list of org members suitable for assignee dropdowns.
        Includes indication of whether each member is already in the project.
        """
        project = self.get_object()

        search = request.query_params.get('search', '').strip()

        # Get org members from admin service
        from .services.admin_service import admin_service
        org_members = admin_service.get_org_members(project.org_id, search=search if search else None)

        # Get current project member emails for comparison
        project_member_emails = set(
            ProjectMembership.objects.filter(
                project=project,
                is_active=True
            ).values_list('user_email', flat=True)
        )

        # Format response
        result = []
        for member in org_members:
            user_email = member.get('user_email') or member.get('email')
            user_id = member.get('user_id') or member.get('id')
            user_name = member.get('user_name') or member.get('name') or member.get('username')

            if not user_email:
                continue

            result.append({
                'user_id': user_id,
                'user_email': user_email,
                'user_name': user_name or user_email.split('@')[0],
                'role': member.get('role', 'member'),
                'is_project_member': user_email in project_member_emails,
            })

        return Response({
            'count': len(result),
            'members': result
        })

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """List project items."""
        project = self.get_object()

        queryset = Item.objects.filter(project=project)

        # Apply filters from request params
        mode = request.query_params.get('mode')
        if mode:
            queryset = queryset.filter(mode=mode)

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        priority = request.query_params.get('priority')
        if priority:
            queryset = queryset.filter(priority=priority)

        assignee_id = request.query_params.get('assignee_id')
        if assignee_id:
            queryset = queryset.filter(assignee_id=assignee_id)

        serializer = ItemListSerializer(queryset.order_by('-created_at'), many=True)
        return Response({
            'count': queryset.count(),
            'project_key': project.key,
            'project_name': project.name,
            'items': serializer.data
        })

    def _is_org_admin_or_owner(self, user, project):
        """Check if user is an admin or owner of the project's organization."""
        from .services.admin_service import admin_service
        return admin_service.can_manage_org(user.id, project.org_id, getattr(user, 'email', None))

    def _can_manage_project(self, user, project):
        """Check if user can manage this project (archive, delete, etc.)."""
        from .services.permissions import PermissionService
        return PermissionService.can_access_project(
            getattr(user, 'id', None),
            project,
            permission='manage_project',
            user_email=getattr(user, 'email', None),
            user=user,
        )

    def _can_manage_members(self, user, project):
        """Check if user can manage project members."""
        from .services.permissions import PermissionService
        return PermissionService.can_access_project(
            getattr(user, 'id', None),
            project,
            permission='manage_members',
            user_email=getattr(user, 'email', None),
            user=user,
        )

    def destroy(self, request, *args, **kwargs):
        """PLS-7 #1: DELETE must NOT permanently delete a project.

        The default ModelViewSet.destroy hard-deletes the row (cascading every
        item/comment/attachment), bypassing the archive-first + confirmation +
        admin-only guards on `hard_delete`. Soft-archive instead (reversible);
        permanent deletion is ONLY via POST .../hard-delete/.
        """
        project = self.get_object()
        if not self._can_manage_project(request.user, project):
            return Response(
                {'error': 'You do not have permission to delete this project'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not project.is_archived:
            project.is_archived = True
            project.archived_at = timezone.now()
            project.save()
            logger.info(f"Project soft-deleted (archived) via DELETE: {project.key} by user {request.user.id}")
        return Response(
            {
                'message': 'Project archived (soft-deleted). To permanently delete, POST to '
                           '/api/projects/{id}/hard-delete/ — admin-only, requires the project to be '
                           'archived and an explicit confirm_key + confirm_item_count.',
                'project_key': project.key,
                'is_archived': True,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Archive a project."""
        project = self.get_object()

        # Check permission - project managers and org admins/owners can archive
        if not self._can_manage_project(request.user, project):
            return Response(
                {'error': 'You do not have permission to archive this project'},
                status=status.HTTP_403_FORBIDDEN
            )

        project.is_archived = True
        project.archived_at = timezone.now()
        project.save()

        logger.info(f"Project archived: {project.key} by user {request.user.id}")
        return Response({
            'message': 'Project archived',
            'project': ProjectSerializer(project).data
        })

    @action(detail=True, methods=['post'])
    def unarchive(self, request, pk=None):
        """Unarchive a project."""
        project = self.get_object()

        # Check permission - project managers and org admins/owners can unarchive
        if not self._can_manage_project(request.user, project):
            return Response(
                {'error': 'You do not have permission to unarchive this project'},
                status=status.HTTP_403_FORBIDDEN
            )

        project.is_archived = False
        project.archived_at = None
        project.save()

        logger.info(f"Project unarchived: {project.key} by user {request.user.id}")
        return Response({
            'message': 'Project unarchived',
            'project': ProjectSerializer(project).data
        })

    @action(detail=True, methods=['post'], url_path='hard-delete')
    def hard_delete(self, request, pk=None):
        """Permanently delete an ARCHIVED project and cascade all its data (PLS-7).

        Guardrails: (1) admin/operator-only; (2) the project must be archived
        first; (3) the caller must confirm by echoing the project key + the exact
        current item count; (4) the delete cascades items/comments/links/
        attachments/components/versions/sprints via the ORM (every child FK is
        on_delete=CASCADE) inside one transaction; (5) an audit line records who
        deleted what. This is IRREVERSIBLE — there is no undo.
        """
        project = self.get_object()

        # (1) admin/operator-only — stricter than _can_manage_project: a project
        # manager who is NOT an org admin/owner may archive but must not be able
        # to permanently delete. Staff (operator) or org admin/owner only.
        if not (request.user.is_staff or self._is_org_admin_or_owner(request.user, project)):
            return Response(
                {'error': 'Permanent deletion is restricted to organization admins/owners (or staff). '
                          'Project managers can archive but not hard-delete.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Parse the confirmation from the request (no DB access). The archive
        # check + count + match are re-verified UNDER a row lock inside the
        # transaction below (PLS-7 #2: close the TOCTOU window).
        confirm_key = (request.data.get('confirm_key') or '').strip()
        confirm_item_count = request.data.get('confirm_item_count')
        try:
            confirm_item_count = int(confirm_item_count)
        except (TypeError, ValueError):
            confirm_item_count = None

        actor_id = getattr(request.user, 'id', None)
        actor_email = getattr(request.user, 'email', None)
        project_key = project.key

        from .services.storage import attachment_storage

        # (2)+(3)+(4) all happen inside ONE transaction with the project row LOCKED.
        with transaction.atomic():
            # PLS-7 #2: lock the project row. This blocks a concurrent unarchive
            # (which UPDATEs this row) and makes the archived-check + item_count
            # authoritative at delete time — the earlier unlocked check could be
            # invalidated by a concurrent unarchive/create between check and delete.
            project = Project.objects.select_for_update().get(pk=project.pk)

            # PLS-7 #5: re-verify delete authority against the LOCKED row. org_id
            # is writable via ProjectSerializer, so a concurrent PATCH/worker move
            # could reassign the project to another org between the initial
            # get_object() guard and this lock — without re-checking, the ORIGINAL
            # org's admin could still hard-delete a project now owned elsewhere.
            if not (request.user.is_staff or self._is_org_admin_or_owner(request.user, project)):
                return Response(
                    {'error': 'Permanent deletion is restricted to organization admins/owners (or staff). '
                              'Project managers can archive but not hard-delete.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # (2) archive-first, re-checked under the lock.
            if not project.is_archived:
                return Response(
                    {'error': 'Project must be archived before it can be permanently deleted. '
                              'Archive it first, then retry.'},
                    status=status.HTTP_409_CONFLICT,
                )

            # (3) explicit confirmation, matched against the locked state. The
            # count is returned in the error so a UI/agent can show
            # "permanently delete <KEY> and its N items?" and retry.
            item_count = Item.objects.filter(project=project).count()
            if confirm_key != project.key or confirm_item_count != item_count:
                return Response(
                    {
                        'error': 'Confirmation required. Re-send confirm_key and confirm_item_count '
                                 'matching this project exactly.',
                        'expected': {'confirm_key': project.key, 'confirm_item_count': item_count},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # (5) audit BEFORE the delete (the row is about to vanish). Structured
            # and greppable; the log stream is the durable audit trail — there is
            # no project-level audit-log model, and per-item ItemActivity rows are
            # being cascaded away with the project.
            logger.warning(
                "PROJECT_HARD_DELETE project_key=%s project_id=%s org_id=%s name=%r items=%s "
                "actor_id=%s actor_email=%s",
                project_key, project.id, project.org_id, project.name, item_count,
                actor_id, actor_email,
            )

            # PLS-7 #3: project.delete() cascades the Attachment DB rows but NOT
            # the MinIO blobs (the normal attachment-destroy path deletes them via
            # attachment_storage.delete_file). Remove the file + thumbnail objects
            # for every attachment under this project's items first, so a
            # "permanent deletion" does not orphan storage. Best-effort per blob —
            # a MinIO hiccup must not abort the delete.
            for att in Attachment.objects.filter(item__project=project).only('storage_key', 'thumbnail_key'):
                try:
                    attachment_storage.delete_file(att.storage_key, att.thumbnail_key)
                except Exception as e:
                    logger.warning(
                        "PLS-7: failed to delete attachment blob storage_key=%s for %s: %s",
                        att.storage_key, project_key, e,
                    )

            # PLS-7 #4: WebhookKey.default_project is SET_NULL, so a webhook routed
            # to this project would survive hard-delete and keep accepting alerts
            # as project-less. Deactivate every webhook key routed here.
            deactivated_webhooks = WebhookKey.objects.filter(default_project=project).update(is_active=False)

            # PLS-7 #7: ProjectTeamLead is keyed by (org_id, project_key), NOT a
            # Project FK, so project.delete() can't cascade it. Leaving the rows
            # means a future project reusing this key in this org would inherit the
            # deleted project's sticky lead election. Remove them in the same txn.
            # PLS-7 #7 (review-2): WorkflowService writes ProjectTeamLead.project_key
            # AFTER project_key.strip().lower() (PLS-21 case contract), while
            # project.key is uppercased at creation. A case-sensitive equality
            # filter on the raw key (e.g. "AT") would miss the stored lowercase
            # rows ("at"), leaving sticky leads behind for a future project reusing
            # the key. Match case-insensitively so every variant for this org+key
            # is cleared.
            deleted_leads, _ = ProjectTeamLead.objects.filter(
                org_id=project.org_id, project_key__iexact=project_key,
            ).delete()

            # (4) cascade delete. All child FKs are on_delete=CASCADE, so the ORM
            # removes items -> comments/attachments/links/activities/watchers/
            # worklogs plus components/versions/sprints/memberships atomically.
            # Cross-project ItemLinks are removed cleanly (only the link row goes;
            # the other project's item is untouched).
            rows_deleted, rows_by_model = project.delete()

        logger.warning(
            "PROJECT_HARD_DELETE_DONE project_key=%s total_rows=%s by_model=%s deactivated_webhooks=%s "
            "deleted_team_leads=%s actor_id=%s",
            project_key, rows_deleted, rows_by_model, deactivated_webhooks, deleted_leads, actor_id,
        )
        return Response({
            'message': f'Project {project_key} permanently deleted.',
            'project_key': project_key,
            'items_deleted': item_count,
            'rows_deleted': rows_deleted,
            'rows_by_model': rows_by_model,
            'deactivated_webhooks': deactivated_webhooks,
        }, status=status.HTTP_200_OK)


# =============================================================================
# Comment ViewSet
# =============================================================================

def _preserve_focus_refused_body(*, item, author_email, causal_actor, content,
                                 diagnostic):
    """PLS-722: keep a comment body the focus guard is about to refuse.

    Shared by BOTH refusing surfaces — the DRF `/api/comments/` path and the
    external MCP `pulse_add_comment` tool. It exists as one function rather than
    two blocks specifically so the two cannot drift: `origin_event_id` is the
    dedupe contract, and a surface that derived it differently would split the
    dedupe silently — the same author refusing the same body would produce two
    rows that differ only by which door they came through.

    Best-effort by construction. A persistence failure must never turn a refusal
    into an accepted comment, so every caller raises regardless of the outcome
    here; the diagnostic is only *enriched* on success.
    """
    from .agent_liveness import canonical_agent_email
    from .services.mechanical_comments import MechanicalCommentFactory

    refused_key = item.item_key or str(item.pk)
    # Both supported fleet-agent domains identify the same actor.  Fold the
    # address before it enters either provenance or the idempotency key so a
    # DRF refusal under ``@shizuha.com`` and an MCP retry under
    # ``@agents.shizuha.io`` cannot preserve the same body twice.
    email = canonical_agent_email(author_email)
    normalized_actor = None
    if causal_actor is not None:
        if isinstance(causal_actor, dict):
            actor_id = causal_actor.get('id')
        elif isinstance(causal_actor, (tuple, list)):
            actor_id = causal_actor[0] if causal_actor else None
        else:
            actor_id = getattr(causal_actor, 'id', None)
        normalized_actor = {'id': actor_id, 'email': email}
    try:
        preserved = MechanicalCommentFactory.create(
            item=item,
            origin_kind=Comment.ORIGIN_FOCUS_REFUSED,
            origin_event='focus_guard.comment_refused',
            template_version=1,
            structured_payload={
                'refused_item_key': refused_key,
                'refused_item_status': item.status or '',
                'refused_author': email,
                'required_task_key': str(
                    diagnostic.get('required_task_key') or ''
                ),
                'refused_body': content,
            },
            # Stable per (author, item, status, blocker root, body) so a client
            # retrying a refused write does not accumulate rows, while a genuine
            # re-refusal in a *different* context still records separately.
            origin_event_id=(
                'focus-refused:'
                + hashlib.sha256(
                    '|'.join([
                        email,
                        refused_key,
                        item.status or '',
                        str(diagnostic.get('required_task_key') or ''),
                        content,
                    ]).encode()
                ).hexdigest()[:40]
            ),
            causal_actor=normalized_actor,
        )
    except Exception:
        logger.exception(
            'PLS-722: could not preserve focus-refused comment body for item '
            '%s by %s; refusing anyway', refused_key, email,
        )
        return None

    # "Persisted" must mean READABLE, not merely written. Without a pointer in
    # the refusal the author cannot learn the body survived, and a record nobody
    # knows exists is the same loss in a different place.
    if getattr(preserved, 'pk', None):
        diagnostic['preserved_comment_id'] = preserved.pk
        diagnostic['preserved_note'] = (
            'Your comment body was NOT lost — it is preserved on '
            f'{refused_key} as comment #{preserved.pk} '
            '(focus-guard refusal record).'
        )
    return preserved


class CommentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for comment management on items.

    Comments can be added to any item (task, action, alert) within a project.
    Users can only view comments on items they have access to.
    Users can only edit/delete their own comments.
    """
    queryset = Comment.objects.all()
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CommentPagination

    def get_serializer_class(self):
        if self.action == 'create':
            return CommentCreateSerializer
        return CommentSerializer

    def get_queryset(self):
        """Filter comments to those the user can access."""
        user = self.request.user

        # For detail views (retrieve, reactions, etc.), scope to comments on items
        # the caller can access. SECURITY (PLAT-4195): this previously returned
        # Comment.objects.all(), and the detail actions (retrieve/reactions) have
        # NO object-level access check — _can_access_item is only enforced on
        # list/create — so any authenticated user could read any org's comment or
        # its reactions by id (cross-tenant read IDOR). Scoping the queryset makes
        # get_object() 404 for comments on items outside the caller's permitted
        # scope; edit/delete remain further gated by author_id in perform_*.
        if self.action in ['retrieve', 'update', 'partial_update', 'destroy', 'reactions']:
            return Comment.objects.filter(
                item_id__in=PermissionService.get_user_item_queryset(user).values('id')
            ).select_related('item')

        # For list views, require item filter
        item_id = self.request.query_params.get('item')

        if not item_id:
            # No item filter - return empty queryset
            # Comments should always be filtered by item
            return Comment.objects.none()

        # Verify user has access to this item. Resolve by numeric id OR item_key
        # (e.g. "AT-95") — a non-numeric value used to hit Item.objects.get(id=...)
        # which raises ValueError, surfacing as an uncaught HTTP 500.
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Comment.objects.none()

        # Check access to item. PLS-716: an inaccessible item must surface as a
        # 403 (consistent with the item retrieve endpoint), NOT a success-shaped
        # empty thread. An agent reading a thread it lacks access to would
        # otherwise conclude the thread is empty and act on that false ground
        # truth — the same false-negative shape that cost real time on PLAT-4813.
        if not self._can_access_item(user, item):
            raise PermissionDenied('You do not have permission to view comments on this item.')

        return Comment.objects.filter(item_id=item.pk).select_related('item')

    def list(self, request, *args, **kwargs):
        """PLS-861: discriminate the four list-branch conditions at the RESPONSE layer.

        ``get_queryset`` deliberately returns an empty queryset for no-filter,
        item-not-found, and permission-refused — a scope decision (PLAT-4195)
        that must never change which rows a caller can read. But a 200-empty
        collapses three structurally different outcomes into one byte-identical
        body, so the MCP layer cannot tell "you are not allowed to see this
        item's comments" from "this item has no comments" (measured: 236 rows
        rendered as "No comments." for a refused caller).

        Resolve the discriminator here, BEFORE any queryset is evaluated:
          - no ``item`` filter      -> 400 Bad Request
          - item does not exist     -> 404 Not Found
          - permission refused      -> 403 Forbidden
          - accessible (any count)  -> 200 with the normal paginated body
        No row is made readable that was not readable before; a refused caller
        still receives zero rows (the 403 carries no comment data).
        """
        item_id = request.query_params.get('item')
        if not item_id:
            # Unreachable through the MCP tool surface (the tool always supplies
            # 'item'), but the same silent branch — make it legible anyway.
            raise drf_serializers.ValidationError(
                {'item': 'Comments must be fetched for a single item. Pass ?item=<id|key>.'}
            )
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            raise NotFound(f'Item {item_id} not found.')
        if not self._can_access_item(request.user, item):
            raise PermissionDenied('You do not have access to comments on this item.')
        return super().list(request, *args, **kwargs)

    def _can_access_item(self, user, item):
        """Check if user can access/comment on the item."""
        return can_comment_on_item(user, item)

    def perform_create(self, serializer):
        """Set author info when creating comment."""
        user = self.request.user

        # Verify user can access the item
        item = serializer.validated_data['item']
        if not self._can_access_item(user, item):
            raise drf_serializers.ValidationError("You don't have access to comment on this item")

        # Native Codex/Claude/Grok MCP comments arrive here via POST /comments/.
        # A progress write on unrelated owned work must not let a stale runtime
        # ignore a runnable dependency root that is already first in its queue.
        from .services.workflow_service import WorkflowService
        if (
            # PLS-846 follow-up: the comment-path guard is an AGENT-queue
            # mechanism — "a stale runtime must not ignore its queue". A human
            # account (operator/CEO) has no runtime or heartbeat that stale-
            # ignores work, and a human recording a decision/ruling on a task
            # they own IS the deliverable. Freezing the operator's comment on
            # an unrelated owned root (e.g. "work BKSH-15 first") deadlocks
            # decision surfaces. Human comment-writes are exempt; the
            # start-work transition guard still governs agents AND humans.
            not WorkflowService._is_human_account(getattr(user, 'email', None))
            and WorkflowService._emails_same_agent_identity(
                getattr(user, 'email', None),
                item.assignee_email,
            )
            # An urgent root can arrive after an agent has already begun a
            # review/QA/merge/implementation stage. Preserve its settlement
            # evidence; the queue focus guard still prevents the next pickup
            # or a no-op resume. Blocking this write loses paid work and can
            # leave an Origin verdict without its authoritative Pulse record.
            # PLS-737: guard non-fire on classification unavailable.
            and WorkflowService._slot_kind_for_guard(item.status) != 'active'
        ):
            focus = WorkflowService._higher_impact_owned_blocker(
                item,
                getattr(user, 'email', None),
                visible_queryset=PermissionService.get_user_item_queryset(user),
            )
            if focus is not None:
                _code, diagnostic = WorkflowService._focus_guard_diagnostic(
                    item,
                    getattr(user, 'email', None),
                    focus,
                    # PLS-709: no transition happens here; say so.
                    surface='comment',
                )
                # PLS-722: the guard still refuses — the caller gets its 400 and
                # no work is started — but the body is PRESERVED rather than
                # destroyed. The refusal used to take the author's content with
                # it, so an investigation finished against a gated task had
                # nowhere to go and the stall itself went unrecorded.
                #
                # Written as a mechanical FOCUS_REFUSED comment: immutable by the
                # perform_update/perform_destroy allow-lists below, carrying the
                # refused body, the blocker root, and the item's status at the
                # moment of refusal so a degraded-branch refusal is auditable.
                #
                # Best-effort by construction: a persistence failure must never
                # convert a refusal into an accepted comment, so the guard fires
                # regardless of what happens here.
                _preserve_focus_refused_body(
                    item=item,
                    author_email=getattr(user, 'email', None),
                    causal_actor=user,
                    content=serializer.validated_data.get('content', ''),
                    diagnostic=diagnostic,
                )
                raise drf_serializers.ValidationError(diagnostic)

        serializer.save(
            author_id=user.id,
            author_email=user.email,
            author_name=getattr(user, 'full_name', '') or getattr(user, 'username', ''),
            origin_kind=Comment.ORIGIN_USER_AUTHORED,
        )

        logger.info(f"Comment created on item {item.id} by user {user.id}")

        # Send comment notification
        try:
            from .services.notifications import notification_service
            comment = serializer.instance
            author_name = getattr(user, 'username', '') or user.email
            notification_service.notify_comment_added(
                item, comment_text=comment.content[:200], author_name=author_name,
                exclude_user_id=user.id
            )
        except Exception as e:
            logger.error(f'Comment notification error on item {item.id}: {e}')

    def perform_update(self, serializer):
        """Only allow updating own comments."""
        comment = self.get_object()
        user = self.request.user

        if comment.origin_kind not in (
            Comment.ORIGIN_USER_AUTHORED,
            Comment.ORIGIN_LEGACY_UNTYPED,
        ):
            raise drf_serializers.ValidationError(
                "Mechanical comments and their provenance are immutable"
            )

        if comment.author_id != user.id and not user.is_staff:
            raise drf_serializers.ValidationError("You can only edit your own comments")

        serializer.save(is_edited=True)
        logger.info(f"Comment {comment.id} updated by user {user.id}")

    def perform_destroy(self, instance):
        """Only allow deleting own comments."""
        user = self.request.user

        if instance.origin_kind not in (
            Comment.ORIGIN_USER_AUTHORED,
            Comment.ORIGIN_LEGACY_UNTYPED,
        ):
            raise drf_serializers.ValidationError(
                "Mechanical comments are immutable audit records"
            )

        if instance.author_id != user.id and not user.is_staff:
            raise drf_serializers.ValidationError("You can only delete your own comments")

        item_id = instance.item_id
        comment_id = instance.id
        instance.delete()
        logger.info(f"Comment {comment_id} on item {item_id} deleted by user {user.id}")

    @action(detail=True, methods=['get', 'post', 'delete'])
    def reactions(self, request, pk=None):
        """Manage reactions on a comment."""
        comment = self.get_object()

        if request.method == 'GET':
            reactions = CommentReaction.objects.filter(comment=comment)
            serializer = CommentReactionSerializer(reactions, many=True)

            # Group by reaction type with counts
            from django.db.models import Count
            summary = reactions.values('reaction').annotate(count=Count('id'))
            reaction_summary = {r['reaction']: r['count'] for r in summary}

            return Response({
                'reactions': serializer.data,
                'summary': reaction_summary
            })

        elif request.method == 'POST':
            serializer = CommentReactionCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            reaction_type = serializer.validated_data['reaction']

            # Check if user already has this reaction
            existing = CommentReaction.objects.filter(
                comment=comment,
                user_id=request.user.id,
                reaction=reaction_type
            ).first()

            if existing:
                return Response({
                    'message': 'You already have this reaction',
                    'reaction': CommentReactionSerializer(existing).data
                })

            # Create new reaction
            reaction = CommentReaction.objects.create(
                comment=comment,
                user_id=request.user.id,
                user_email=request.user.email,
                reaction=reaction_type
            )

            return Response({
                'message': 'Reaction added',
                'reaction': CommentReactionSerializer(reaction).data
            }, status=status.HTTP_201_CREATED)

        elif request.method == 'DELETE':
            reaction_type = request.query_params.get('reaction')
            if not reaction_type:
                return Response(
                    {'error': 'reaction query parameter is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            deleted, _ = CommentReaction.objects.filter(
                comment=comment,
                user_id=request.user.id,
                reaction=reaction_type
            ).delete()

            if deleted:
                return Response({'message': 'Reaction removed'})
            else:
                return Response({'message': 'Reaction not found'}, status=status.HTTP_404_NOT_FOUND)


# =============================================================================
# Attachment ViewSet
# =============================================================================

class AttachmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for file attachments.

    Supports:
    - Listing attachments for an item
    - Uploading new attachments
    - Deleting attachments
    - Getting download URLs
    """
    queryset = Attachment.objects.all()
    serializer_class = AttachmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Filter attachments by item for list, allow all for detail views."""
        # For detail views (retrieve, destroy, actions), allow access to any attachment
        if self.action in ['retrieve', 'destroy', 'download', 'serve', 'thumbnail']:
            return Attachment.objects.all()
        # For list view, require item filter (accepts numeric PK or item_key like "AT-83")
        item_id = self.request.query_params.get('item')
        if item_id:
            try:
                item = _resolve_item(item_id)
            except Item.DoesNotExist:
                return Attachment.objects.none()
            # Match item/comment visibility.  Returning an empty list avoids
            # disclosing whether an inaccessible private item has attachments.
            if not self._can_access_item(self.request.user, item):
                return Attachment.objects.none()
            qs = Attachment.objects.filter(item_id=item.pk)
            # By default, exclude comment-embedded images from the attachments list
            # Pass ?source=all to include everything
            source_filter = self.request.query_params.get('source', 'direct')
            if source_filter != 'all':
                qs = qs.exclude(source=Attachment.SOURCE_COMMENT)
            return qs
        return Attachment.objects.none()

    def get_object(self):
        """Apply object-level item access to retrieve/download/delete actions."""
        attachment = super().get_object()
        if not self._can_access_item(self.request.user, attachment.item):
            raise PermissionDenied('You do not have access to this attachment')
        return attachment

    def create(self, request, *args, **kwargs):
        """Handle file upload."""
        file = request.FILES.get('file')
        item_id = request.data.get('item')

        if not file:
            return Response(
                {'error': 'No file provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not item_id:
            return Response(
                {'error': 'item is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get and verify access to item (accepts numeric PK or item_key like "AT-83")
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response(
                {'error': 'Item not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        # Check permission
        if not self._can_access_item(request.user, item):
            return Response(
                {'error': 'You do not have access to this item'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Upload to MinIO
        try:
            from .services.storage import attachment_storage

            storage_key, file_size, image_meta = attachment_storage.upload_file(
                file_data=file,
                filename=file.name,
                content_type=file.content_type,
                item_id=item.id,
                user_id=request.user.id,
            )

            # Create attachment record
            source = request.data.get('source', Attachment.SOURCE_DIRECT)
            if source not in dict(Attachment.SOURCE_CHOICES):
                source = Attachment.SOURCE_DIRECT

            attachment = Attachment.objects.create(
                item=item,
                storage_key=storage_key,
                filename=file.name,
                file_type=file.content_type,
                file_size=file_size,
                is_image=image_meta is not None,
                width=image_meta.get('width') if image_meta else None,
                height=image_meta.get('height') if image_meta else None,
                thumbnail_key=image_meta.get('thumbnail_key', '') if image_meta else '',
                uploaded_by_id=request.user.id,
                uploaded_by_email=request.user.email,
                source=source,
            )

            serializer = AttachmentSerializer(attachment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Failed to upload attachment: {e}")
            return Response(
                {'error': f'Failed to upload file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def destroy(self, request, *args, **kwargs):
        """Delete attachment from both database and MinIO."""
        attachment = self.get_object()

        # Check permission: uploader, item assignee/creator, or staff
        user_id = request.user.id
        item = attachment.item
        # PLS-1351: uploaded_by_id is the PK recorded at create time; comparing
        # it against a freshly re-resolved request.user.id breaks whenever the
        # seat identity re-keys (token re-mint / ID-side user re-key / PK-space
        # flip between auth classes) — the 09-24 8/8 canary 403 signature. The
        # email is the stable cross-mint, cross-PK-space identity, and it is
        # stored on the row at create time, so accept an exact email match as
        # uploader evidence too. Never widen beyond the same actor: the email
        # is captured from the SAME authenticated principal that performed the
        # upload, not client-supplied.
        uploader_email = (
            getattr(request.user, 'email', None) or ''
        )
        can_delete = (
            attachment.uploaded_by_id == user_id or
            bool(
                uploader_email
                and attachment.uploaded_by_email
                and attachment.uploaded_by_email == uploader_email
            ) or
            is_item_participant(request.user, item) or
            request.user.is_staff
        )
        if not can_delete:
            return Response(
                {'error': 'You do not have permission to delete this attachment'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Delete from MinIO
        try:
            from .services.storage import attachment_storage
            attachment_storage.delete_file(
                attachment.storage_key,
                attachment.thumbnail_key
            )
        except Exception as e:
            logger.warning(f"Failed to delete file from MinIO: {e}")

        # Delete from database
        attachment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """Get a signed URL for the safe attachment download proxy."""
        attachment = self.get_object()

        # Never expose a MinIO presigned URL that replays uploader-controlled
        # object metadata. The proxy below owns the response type/disposition.
        return Response({
            'download_url': AttachmentSerializer(attachment).data['download_url'],
            'filename': attachment.filename,
            'file_type': attachment.file_type,
            'file_size': attachment.file_size,
        })

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
    def serve(self, request, pk=None):
        """
        Serve the attachment file directly (proxy from MinIO).
        This avoids exposing internal MinIO URLs to the browser.

        Browser image/download navigation may not carry the API bearer header,
        so serializers issue a short-lived signed URL.  Authenticated callers
        may also access the bytes through the canonical item predicate.
        """
        from django.http import StreamingHttpResponse, Http404

        try:
            attachment = Attachment.objects.get(pk=pk)
        except Attachment.DoesNotExist:
            raise Http404("Attachment not found")

        if not self._can_serve_attachment(request, attachment, 'serve'):
            return Response(
                {'error': 'You do not have access to this attachment'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            from .services.storage import attachment_storage

            # Get the file from MinIO
            response = attachment_storage.client.get_object(
                attachment_storage.bucket,
                attachment.storage_key
            )

            # Stream the response
            def file_iterator():
                try:
                    for chunk in response.stream(32 * 1024):  # 32KB chunks
                        yield chunk
                finally:
                    response.close()
                    response.release_conn()

            # Original uploads are arbitrary untrusted bytes. Force download
            # with a server-selected inert type; never replay the claimed MIME
            # type or render an original upload inline on the Pulse origin.
            http_response = StreamingHttpResponse(
                file_iterator(),
                content_type='application/octet-stream',
            )
            http_response['Content-Length'] = attachment.file_size
            http_response['Content-Disposition'] = content_disposition_header(
                True,
                self._safe_download_filename(attachment.filename),
            )
            self._apply_capability_security_headers(http_response)
            # Defense in depth if a user agent ignores attachment disposition.
            http_response['Content-Security-Policy'] = "sandbox; default-src 'none'"
            http_response['X-Frame-Options'] = 'DENY'

            return http_response

        except Exception as e:
            return Response(
                {'error': f'Failed to serve file: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
    def thumbnail(self, request, pk=None):
        """
        Serve the thumbnail for an image attachment.

        Access follows the signed/authenticated serve contract above.
        """
        from django.http import StreamingHttpResponse, Http404

        try:
            attachment = Attachment.objects.get(pk=pk)
        except Attachment.DoesNotExist:
            raise Http404("Attachment not found")

        if not self._can_serve_attachment(request, attachment, 'thumbnail'):
            return Response(
                {'error': 'You do not have access to this attachment'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not attachment.is_image or not attachment.thumbnail_key:
            return Response(
                {'error': 'No thumbnail available'},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            from .services.storage import attachment_storage

            # Get the thumbnail from MinIO
            response = attachment_storage.client.get_object(
                attachment_storage.bucket,
                attachment.thumbnail_key
            )

            # Stream the response
            def file_iterator():
                try:
                    for chunk in response.stream(32 * 1024):
                        yield chunk
                finally:
                    response.close()
                    response.release_conn()

            # Determine content type from thumbnail key
            content_type = 'image/jpeg' if attachment.thumbnail_key.endswith('.jpg') else 'image/png'

            http_response = StreamingHttpResponse(
                file_iterator(),
                content_type=content_type
            )
            safe_extension = '.jpg' if content_type == 'image/jpeg' else '.png'
            http_response['Content-Disposition'] = content_disposition_header(
                False,
                f'thumbnail-{attachment.id}{safe_extension}',
            )
            self._apply_capability_security_headers(http_response)
            http_response['Cross-Origin-Resource-Policy'] = 'same-origin'

            return http_response

        except Exception as e:
            return Response(
                {'error': f'Failed to serve thumbnail: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _can_access_item(self, user, item):
        """Use the canonical item/comment authorization predicate."""
        return can_comment_on_item(user, item)

    def _can_serve_attachment(self, request, attachment, kind):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and getattr(user, 'is_authenticated', False)
            and self._can_access_item(user, attachment.item)
        ):
            return True
        return validate_attachment_serve_token(
            request.query_params.get('token'),
            attachment.id,
            kind,
        )

    @staticmethod
    def _apply_capability_security_headers(response):
        """Prevent capability responses from becoming executable/shared data."""
        response['X-Content-Type-Options'] = 'nosniff'
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'

    @staticmethod
    def _safe_download_filename(filename):
        """Normalize uploader metadata before RFC 6266 header formatting."""
        basename = str(filename or '').replace('\\', '/').rsplit('/', 1)[-1]
        basename = re.sub(r'[\x00-\x1f\x7f]+', '_', basename).strip()
        return basename[:255] or 'attachment'


# =============================================================================
# Item Link ViewSet
# =============================================================================

class ItemLinkViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing item links.
    Primarily used for deleting links by ID.
    """
    queryset = ItemLink.objects.none()
    serializer_class = ItemLinkSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'delete']

    def get_queryset(self):
        visible_items = PermissionService.get_user_item_queryset(self.request.user)
        return ItemLink.objects.filter(
            source_item__in=visible_items,
            target_item__in=visible_items,
        ).select_related('source_item', 'target_item')

    def destroy(self, request, *args, **kwargs):
        """Delete a link."""
        link = self.get_object()

        # Only creator or staff can delete
        if link.created_by_id != request.user.id and not request.user.is_staff:
            return Response(
                {'error': 'You can only delete links you created'},
                status=status.HTTP_403_FORBIDDEN
            )

        # PLS-458 (review P1 #2): route through the single guarded deletion
        # boundary so the SecurityGateState guard is enforced transactionally and
        # identically for EVERY deletion path.
        from .services.workflow_service import (
            WorkflowService, SecurityGateLinkProtectedError,
        )
        try:
            deleted = WorkflowService.delete_link_guarded(link)
        except SecurityGateLinkProtectedError as e:
            return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)
        if deleted is False:
            return Response(
                {'error': 'link deletion was rejected by the workflow guard'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


# =============================================================================
# Sprint ViewSet
# =============================================================================

class SprintViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Sprint management.

    Users can:
    - List sprints for projects they have access to
    - Create sprints in projects where they are members
    - Start and complete sprints
    - View sprint velocity and burndown data
    """
    queryset = Sprint.objects.all()
    serializer_class = SprintSerializer
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        if self.action == 'create':
            return SprintCreateSerializer
        elif self.action == 'list':
            return SprintListSerializer
        return SprintSerializer

    def get_queryset(self):
        """Filter sprints to those in projects the user can access."""
        user = self.request.user
        queryset = Sprint.objects.all()

        if not user.is_staff:
            # Get projects user has access to
            from .services.permissions import PermissionService
            accessible_projects = PermissionService.get_user_projects(
                user.id, user_email=user.email
            )
            queryset = queryset.filter(project__in=accessible_projects)

        # Filter by project
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        # Filter by status
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        return queryset.select_related('project')

    def perform_create(self, serializer):
        """Create sprint with creator info."""
        # Verify user has access to the project
        project = serializer.validated_data['project']
        if not self.request.user.is_staff:
            from .services.permissions import PermissionService
            # PLS-72: PermissionService has no `can_create_items` (it never did —
            # this raised AttributeError → 500 on every sprint create). Use the
            # canonical project-create check, matching ItemViewSet.perform_create:
            # can_access_project(user_id, project, permission='create', user_email).
            if not PermissionService.can_access_project(
                self.request.user.id,
                project,
                permission='create',
                user_email=self.request.user.email,
                user=self.request.user,
            ):
                raise drf_serializers.ValidationError({
                    'project': 'You do not have permission to create sprints in this project'
                })

        serializer.save(
            created_by_id=self.request.user.id,
            created_by_email=self.request.user.email
        )

    def create(self, request, *args, **kwargs):
        """Override create to return full SprintSerializer with id."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Return full sprint data using SprintSerializer (includes id)
        response_serializer = SprintSerializer(serializer.instance)
        headers = self.get_success_headers(response_serializer.data)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
            headers=headers
        )

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Start a sprint."""
        sprint = self.get_object()

        if sprint.status != 'planning':
            return Response(
                {'error': 'Can only start sprints in planning status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if another sprint is active in this project
        active_sprint = Sprint.objects.filter(
            project=sprint.project,
            status='active'
        ).first()
        if active_sprint:
            return Response(
                {'error': f'Sprint "{active_sprint.name}" is already active. Complete it first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            sprint.start()
            serializer = SprintSerializer(sprint)
            return Response(serializer.data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Complete a sprint."""
        sprint = self.get_object()

        if sprint.status != 'active':
            return Response(
                {'error': 'Can only complete active sprints'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Option to move incomplete items to next sprint or backlog
        move_to = request.data.get('move_incomplete_to')  # 'backlog' or sprint_id

        with transaction.atomic():
            # Get incomplete items
            incomplete_items = sprint.items.filter(
                mode='task',
                status__in=['pending', 'in_progress', 'blocked']
            )

            if move_to == 'backlog':
                # Move to backlog (remove sprint assignment)
                incomplete_items.update(sprint=None)
            elif move_to:
                # Move to specified sprint
                try:
                    next_sprint = Sprint.objects.get(
                        id=move_to,
                        project=sprint.project,
                        status='planning'
                    )
                    incomplete_items.update(sprint=next_sprint)
                except Sprint.DoesNotExist:
                    return Response(
                        {'error': 'Target sprint not found or not in planning status'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            sprint.complete()

        serializer = SprintSerializer(sprint)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def burndown(self, request, pk=None):
        """Get burndown chart data for a sprint."""
        sprint = self.get_object()
        burndown_data = sprint.get_burndown_data()
        # Transform 'actual' to 'remaining' for frontend compatibility
        for point in burndown_data:
            point['remaining'] = point.pop('actual', None)
        # Show effective committed points: cached field or live story-point / task-count fallback
        committed = sprint.committed_points
        if committed == 0:
            from django.db.models import Sum as _Sum
            committed = sprint.items.filter(mode='task').aggregate(
                total=_Sum('story_points'))['total'] or sprint.items.filter(mode='task').count()
        return Response({
            'sprint_id': sprint.id,
            'sprint_name': sprint.name,
            'committed_points': committed,
            'burndown': burndown_data
        })

    @action(detail=False, methods=['get'])
    def velocity(self, request):
        """
        Get velocity data across completed sprints.
        Returns points completed per sprint for a project.
        """
        project_id = request.query_params.get('project')
        if not project_id:
            return Response(
                {'error': 'project parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        completed_sprints = self.get_queryset().filter(
            project_id=project_id,
            status='completed'
        ).order_by('completed_at')[:10]  # Last 10 sprints

        velocity_data = []
        for sprint in completed_sprints:
            velocity_data.append({
                'sprint_id': sprint.id,
                'sprint_name': sprint.name,
                'committed_points': sprint.committed_points,
                'completed_points': sprint.get_velocity(),
                'start_date': sprint.start_date,
                'end_date': sprint.end_date
            })

        # Calculate average velocity
        total_completed = sum(s['completed_points'] for s in velocity_data)
        if velocity_data:
            avg_velocity = total_completed / len(velocity_data)
        else:
            avg_velocity = 0

        return Response({
            'project_id': int(project_id),
            'average_velocity': round(avg_velocity, 1),
            'total_points_completed': total_completed,
            'sprints_completed': len(velocity_data),
            'sprints': velocity_data
        })

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get all items in a sprint."""
        sprint = self.get_object()
        items = sprint.items.filter(mode='task')

        # Filter by status
        status_param = request.query_params.get('status')
        if status_param:
            items = items.filter(status=status_param)

        serializer = ItemListSerializer(items, many=True)
        return Response({
            'count': items.count(),
            'items': serializer.data
        })

    @action(detail=False, methods=['get'])
    def active(self, request):
        """Get the active sprint for a project."""
        project_id = request.query_params.get('project')
        if not project_id:
            return Response(
                {'error': 'project parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        active_sprint = self.get_queryset().filter(
            project_id=project_id,
            status='active'
        ).first()

        if not active_sprint:
            return Response({'sprint': None})

        serializer = SprintSerializer(active_sprint)
        return Response({'sprint': serializer.data})


# =============================================================================
# Internal API Views (for service-to-service communication)
# =============================================================================

def _lifecycle_email_variants(email):
    """Return canonical/company + agent-domain aliases for a lifecycle email."""
    if not email:
        return set()
    raw = str(email).strip().lower()
    variants = {raw} if raw else set()
    if raw.endswith('@shizuha.com'):
        variants.add(raw[:-len('@shizuha.com')] + '@agents.shizuha.io')
    elif raw.endswith('@agents.shizuha.io'):
        variants.add(raw[:-len('@agents.shizuha.io')] + '@shizuha.com')
    return variants


def _unassign_items_for_user(user_id, email=None):
    """Clear Pulse assignees for a Shizuha ID user, with audit activity.

    Prefer the lifecycle event's canonical email because Pulse's cross-service
    identity is email-first. Keep an ID fallback only for old rows where the
    email column was never populated.
    """
    email_variants = _lifecycle_email_variants(email)
    if email_variants:
        q = Q()
        for variant in email_variants:
            q |= Q(assignee_email__iexact=variant)
        q |= Q(assignee_id=user_id, assignee_email__isnull=True)
        q |= Q(assignee_id=user_id, assignee_email='')
    else:
        q = Q(assignee_id=user_id)

    assigned_items = Item.objects.select_for_update().filter(q)
    count = assigned_items.count()

    # Unassign all items through the atomic setter so assignee_id/email cannot
    # be half-cleared by a bulk update (PLAT-192).
    for item in assigned_items.iterator():
        # ID-39: cross-service lifecycle cleanup must be auditable. Attribute
        # the assignee clear to the system consumer so ItemActivity records it.
        item._activity_actor = (0, 'system@pulse')
        item.unassign(save=True)
    return count


class InternalUserLifecycleEventView(APIView):
    """Signed ID user-lifecycle event consumer.

    POST /api/internal/user-lifecycle-events/ with JSON:
      {event_id, event_type, user_id, ...}

    Auth: HMAC SHA-256 over ``<timestamp>.<raw_body>`` using the shared
    PULSE_USER_LIFECYCLE_CONSUMER_SECRET. Redelivery is idempotent by event_id.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ok, error = _verify_id_lifecycle_signature(request)
        if not ok:
            return error

        event_id = request.data.get('event_id')
        event_type = request.data.get('event_type')
        user_id = request.data.get('user_id')
        if not event_id or not event_type or user_id is None:
            return Response(
                {'error': 'event_id, event_type, and user_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            event_uuid = uuid.UUID(str(event_id))
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return Response({'error': 'Malformed event_id or user_id'}, status=status.HTTP_400_BAD_REQUEST)
        if event_type not in dict(UserLifecycleEventReceipt.EVENT_CHOICES):
            return Response({'error': f'Unsupported event_type {event_type!r}'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            existing = UserLifecycleEventReceipt.objects.filter(event_id=event_uuid).first()
            if existing:
                return Response({
                    'success': True,
                    'duplicate': True,
                    'event_id': str(existing.event_id),
                    'event_type': existing.event_type,
                    'user_id': existing.user_id,
                    'items_unassigned': existing.items_unassigned,
                })

            items_unassigned = 0
            if event_type in {
                UserLifecycleEventReceipt.EVENT_DEACTIVATED,
                UserLifecycleEventReceipt.EVENT_DELETED,
            }:
                items_unassigned = _unassign_items_for_user(
                    user_id_int,
                    email=request.data.get('email') or request.data.get('user_email'),
                )

            UserLifecycleEventReceipt.objects.create(
                event_id=event_uuid,
                event_type=event_type,
                user_id=user_id_int,
                payload=dict(request.data),
                items_unassigned=items_unassigned,
            )

        logger.info(
            'Processed ID lifecycle event %s type=%s user=%s items_unassigned=%s',
            event_id, event_type, user_id_int, items_unassigned,
        )
        return Response({
            'success': True,
            'duplicate': False,
            'event_id': str(event_uuid),
            'event_type': event_type,
            'user_id': user_id_int,
            'items_unassigned': items_unassigned,
        })


class InternalUserUnassignView(APIView):
    """Legacy signed endpoint to unassign all tasks from a user.

    Prefer InternalUserLifecycleEventView; this path remains for older internal
    callers but is no longer unauthenticated.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request, user_id):
        ok, error = _verify_id_lifecycle_signature(request)
        if not ok:
            return error
        with transaction.atomic():
            count = _unassign_items_for_user(int(user_id))

        logger.info(f"Unassigned {count} items from user {user_id}")
        return Response({
            'success': True,
            'user_id': user_id,
            'items_unassigned': count,
        })


class InternalUserProjectMembershipsView(APIView):
    """
    Internal endpoint: return a user's active Pulse project memberships.

    GET /api/internal/users/{user_id}/project-memberships/
    Optional query param: ?email=user@example.com

    Returns the same shape as admin's InternalUserProjectMembershipsView so
    shizuha-admin can proxy here and shizuha-id sees a consistent contract:
      {
        "project_memberships": {"<project_id>": "<role>", ...},
        "project_ids": [<int>, ...]
      }

    Secured by network — not exposed through nginx externally (SCS-31).
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, user_id):
        # PLAT-5343: measure the serve duration — the tail is the fault, and an
        # unmeasured endpoint cannot name it. try/finally so the fail-closed
        # 503 path is histogrammed too.
        import time as _time
        _started = _time.monotonic()
        try:
            return self._resolve_projection(request, user_id)
        finally:
            _latency_ms = (_time.monotonic() - _started) * 1000.0
            try:
                from .services.project_membership_metrics import record_projection_latency_ms
                record_projection_latency_ms(_latency_ms)
            except Exception:  # noqa: BLE001 — telemetry must never break the projection
                pass

    def _resolve_projection(self, request, user_id):
        user_email = request.query_params.get('email') or None
        q = Q(user_id=user_id, is_active=True)
        if user_email:
            # SCS-41: historical and explicitly managed ProjectMembership rows may
            # use an agent-domain alias (e.g. 'zen@agents.shizuha.io'), while
            # shizuha-id queries with the canonical company-domain identity. Expand
            # both aliases so an explicit grant has one stable identity projection.
            email_variants = {user_email}
            if user_email.endswith('@shizuha.com'):
                email_variants.add(user_email[:-len('@shizuha.com')] + '@agents.shizuha.io')
            elif user_email.endswith('@agents.shizuha.io'):
                email_variants.add(user_email[:-len('@agents.shizuha.io')] + '@shizuha.com')
            q |= Q(user_email__in=email_variants, is_active=True)

        from .services.permissions import PermissionService, ProjectMembershipUnavailable
        from .services.project_membership_metrics import record_projection_outcome
        rows = list(
            ProjectMembership.objects
            .filter(q)
            .select_related('project')
            .values_list('project_id', 'project__org_id', 'role')
        )
        # PLS-728: the org-membership filter is authorization-relevant — a row
        # dropped here disappears from the JWT claim. Use the strict check so an
        # unreachable shizuha-admin (which the non-strict path reads as "not a
        # member") fails the request closed instead of serving a short projection
        # as if it were authoritative.
        try:
            allowed_org_ids = {
                org_id
                for org_id in {row[1] for row in rows}
                if PermissionService.identity_belongs_to_organization_strict(
                    user_id,
                    org_id,
                    user_email,
                )
            }
        except ProjectMembershipUnavailable as exc:
            record_projection_outcome('unavailable')
            logger.error(
                'PLS-728 project-membership projection unavailable for user %s (%s); '
                'refusing to answer with a non-authoritative projection',
                user_id, exc,
            )
            response = Response(
                {
                    'error': 'project_membership_unavailable',
                    'detail': (
                        'Pulse is the source of truth for project membership but '
                        'could not confirm the caller\'s organization membership '
                        '(shizuha-admin unreachable). Refusing to answer with an '
                        'empty or partial projection. Retry.'
                    ),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
            response['Retry-After'] = '1'
            return response
        priority = {'owner': 3, 'member': 2, 'viewer': 1}
        dedup: dict = {}
        for proj_id, org_id, role in rows:
            if org_id not in allowed_org_ids:
                continue
            existing = dedup.get(proj_id)
            if existing is None or priority.get(role, 0) > priority.get(existing, 0):
                dedup[proj_id] = role

        # PLS-728: distinguish a genuine empty (Pulse holds no memberships) from
        # every failure mode at the source where it is load-bearing.
        record_projection_outcome('authoritative_empty' if not dedup else 'served')
        return Response({
            'project_memberships': {str(k): v for k, v in dedup.items()},
            'project_ids': sorted(dedup.keys()),
        })


class AgentOutputAttributionAuditView(APIView):
    """
    Org-scoped aggregate audit path for EVOL-44/EVOL-22.

    Returns per-agent output counts without exposing per-item/comment bodies, so
    the daily inspection can see whole-fleet attribution without per-agent
    permission-hidden gaps. The endpoint is read-only, explicit-org scoped, and
    aggregates only comments + status transitions under projects in that org.
    """
    permission_classes = [permissions.IsAuthenticated]
    DEFAULT_WINDOW_HOURS = 24
    MAX_WINDOW_HOURS = 24 * 14

    def _window_start(self, request, now):
        raw_since = (request.query_params.get('since') or '').strip()
        if raw_since:
            try:
                parsed = datetime.fromisoformat(raw_since.replace('Z', '+00:00'))
            except (TypeError, ValueError) as exc:
                raise ValueError('since must be a valid ISO-8601 timestamp') from exc
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            earliest = now - timedelta(hours=self.MAX_WINDOW_HOURS)
            if parsed < earliest:
                raise ValueError(f'since cannot be older than {self.MAX_WINDOW_HOURS} hours')
            return parsed
        try:
            hours = int(request.query_params.get('window_hours', self.DEFAULT_WINDOW_HOURS))
        except (TypeError, ValueError):
            hours = self.DEFAULT_WINDOW_HOURS
        hours = min(max(1, hours), self.MAX_WINDOW_HOURS)
        return now - timedelta(hours=hours)

    def get(self, request):
        org_id = _ensure_explicit_org_membership(request)
        if org_id is None:
            return Response(
                {'error': 'organization_id is required for the audit aggregate path'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = request.user
        has_global_audit_scope = bool(
            getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)
        )
        if not has_global_audit_scope:
            memberships = getattr(user, 'organization_memberships', {}) or {}
            role = memberships.get(org_id) or memberships.get(str(org_id))
            if role not in {'owner', 'admin', 'lead', 'audit', 'auditor'}:
                return Response(
                    {'error': 'audit aggregate requires org admin/owner/lead/audit role'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        now = timezone.now()
        try:
            since = self._window_start(request, now)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        project_ids = list(
            Project.objects
            .filter(org_id=org_id)
            .values_list('id', flat=True)
        )

        rows = {}
        # TeamRoster and AgentAvailability are global fleet tables, not
        # org-scoped. Only globally-privileged staff/superuser audit callers may
        # use them to seed zero-output rows or availability hints; org-local audit
        # roles get only actors who produced output in their requested org.
        roster_counts = _active_roster_identity_counts() if has_global_audit_scope else {}
        inactive = {
            _normalize_audit_email(row.email): row
            for row in AgentAvailability.objects.filter(active=False)
        } if has_global_audit_scope else {}

        def ensure(email):
            normalized = _normalize_audit_email(email)
            if not normalized:
                normalized = 'unknown@pulse.local'
            row = rows.setdefault(normalized, {
                'email': normalized,
                'display_name': normalized.split('@')[0],
                'is_active_agent': normalized not in inactive,
                'is_shared_service_identity': _is_shared_service_identity(normalized, roster_counts),
                'counts': {
                    'comments': 0,
                    'transitions': 0,
                    'total_output': 0,
                },
                'latest_output_at': None,
                'user_id': None,  # EVOL-58: shizuha auth user id = Cortex cost join key
            })
            availability = inactive.get(normalized)
            if availability:
                row['availability_status'] = availability.status
                row['availability_reason'] = availability.reason
            return row

        # Include every active roster identity even if it produced zero output.
        for email in roster_counts.keys():
            if email not in inactive:
                ensure(email)

        comment_counts = (
            Comment.objects
            .filter(item__project_id__in=project_ids, created_at__gte=since)
            .values('author_email')
            .annotate(count=Count('id'), latest=Max('created_at'), uid=Max('author_id'))
        )
        for entry in comment_counts:
            row = ensure(entry.get('author_email'))
            row['counts']['comments'] = int(entry.get('count') or 0)
            row['latest_output_at'] = entry.get('latest')
            if row.get('user_id') is None:
                row['user_id'] = entry.get('uid')  # EVOL-58 join key

        transition_counts = (
            ItemActivity.objects
            .filter(item__project_id__in=project_ids, action='status_changed', created_at__gte=since)
            .values('user_email')
            .annotate(count=Count('id'), latest=Max('created_at'), uid=Max('user_id'))
        )
        for entry in transition_counts:
            row = ensure(entry.get('user_email'))
            row['counts']['transitions'] = int(entry.get('count') or 0)
            if row.get('user_id') is None:
                row['user_id'] = entry.get('uid')  # EVOL-58 join key
            latest = entry.get('latest')
            if latest and (row['latest_output_at'] is None or latest > row['latest_output_at']):
                row['latest_output_at'] = latest

        for row in rows.values():
            row['counts']['total_output'] = row['counts']['comments'] + row['counts']['transitions']
            if row['latest_output_at'] is not None:
                row['latest_output_at'] = row['latest_output_at'].isoformat()

        # EVOL-58: LEFT-JOIN per-user Cortex cost -> cost-to-output ratio, keyed on
        # user_id (the shizuha auth id shared by Pulse output + Cortex UsageRecord).
        # Fail-soft (STRIDE-lite D5): a Cortex error yields cost=null, never a 500.
        # The service token authorizes Pulse<->Cortex only; who SEES cost is still
        # gated solely by the EVOL-44 org-role check above (D4).
        from .cortex_cost import fetch_cortex_cost_by_user
        window_days = max(1, int(round((now - since).total_seconds() / 86400)))
        cost_by_user, cost_provenance = fetch_cortex_cost_by_user(window_days)
        for row in rows.values():
            uid = row.get('user_id')
            crow = cost_by_user.get(uid) if uid is not None else None
            if crow is not None:
                total_output = row['counts']['total_output']
                cost_usd = crow.get('cost_usd') or 0
                row['cost'] = {
                    'cost_microdollars': crow.get('cost_microdollars') or 0,
                    'cost_usd': cost_usd,
                    'requests': crow.get('requests') or 0,
                }
                # No invented estimate: ratio is null when there is no attributable output.
                row['cost_to_output_ratio'] = (cost_usd / total_output) if total_output else None
            else:
                row['cost'] = None
                row['cost_to_output_ratio'] = None

        ordered = sorted(
            rows.values(),
            key=lambda r: (-r['counts']['total_output'], r['email']),
        )
        return Response({
            'organization_id': org_id,
            'since': since.isoformat(),
            'until': now.isoformat(),
            'source': 'pulse.aggregate.comments_and_status_transitions',
            'privacy': 'aggregate counts only; no comment bodies or task descriptions',
            'cost_source': 'cortex.usage.by_user (privileged, IsSuperUser); ratio = cost_usd / total_output',
            'cost_provenance': cost_provenance,
            'shared_service_identity_rule': {
                'known_service_identities': sorted(KNOWN_SHARED_SERVICE_IDENTITIES),
                'duplicate_identity_source': 'TeamRoster email occurrence count',
            },
            'agents': ordered,
        })


class AgentProjectMembershipListView(APIView):
    """
    Authenticated API: return active, non-expired agent×project affinity
    memberships for EVOL-20/EVOL-42 dashboard telemetry.

    GET /api/agent-project-memberships/
    Optional filters: ?agent_email=..., ?agent_user_id=..., ?project_id=...

    The team-throughput exporter consumes this endpoint to distinguish primary
    vs secondary/overflow project assignments without reading the DB directly.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        now = timezone.now()
        qs = (
            AgentProjectMembership.objects
            .select_related('project')
            .filter(active=True)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .order_by('agent_user_email', 'project__key', 'project_id')
        )

        agent_email = request.query_params.get('agent_email') or None
        if agent_email:
            qs = qs.filter(agent_user_email__iexact=agent_email)

        agent_user_id = request.query_params.get('agent_user_id') or None
        if agent_user_id:
            qs = qs.filter(agent_user_id=agent_user_id)

        project_id = request.query_params.get('project_id') or None
        if project_id:
            qs = qs.filter(project_id=project_id)

        memberships = []
        for row in qs:
            project = row.project
            memberships.append({
                'id': row.pk,
                'agent_user_id': row.agent_user_id,
                'agent_user_email': row.agent_user_email,
                'project': {
                    'id': project.id,
                    'key': project.key,
                    'name': project.name,
                },
                'project_id': project.id,
                'project_key': project.key,
                'project_name': project.name,
                'tier': row.tier,
                'is_lead': row.is_lead,
                'active': row.active,
                'expires_at': row.expires_at.isoformat() if row.expires_at else None,
                'created_at': row.created_at.isoformat() if row.created_at else None,
                'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            })

        return Response(memberships)


class AgentSelfAvailabilityView(APIView):
    """SCLI-92: Agents report capacity signals to Pulse (not fleet identity SoT).

    PATCH /api/agent-availability/self/
    Auth: agent JWT (IsAuthenticated). Updates or creates the AgentAvailability
    row for request.user.email. Called by bridges on capacity exhaustion
    (active=false) and on startup / recovery (active=true).

    Hive remains source of truth for what the agent *is* (model, execution
    method, enablement). Self-latches are capacity signals only and must not
    claim a provider the seat does not run (e.g. claude-token-pool-exhausted on
    a Cortex/GLM gateway agent).
    """
    permission_classes = [permissions.IsAuthenticated]

    # Provider-class self-disable reasons must match the seat's execution method.
    _CLAUDE_CAPACITY_REASONS = frozenset({
        'claude-token-pool-exhausted',
    })
    _CLAUDE_METHODS = frozenset({
        'claude_code_server', 'claude', 'claude-bridge',
    })

    def patch(self, request):
        active = request.data.get('active', True)
        reason = str(request.data.get('reason', '') or '').strip()
        # Optional: bridges declare their harness so we refuse cross-provider latches.
        method = str(
            request.data.get('execution_method')
            or request.headers.get('X-Shizuha-Execution-Method')
            or ''
        ).strip().lower()
        if not bool(active):
            reason_l = reason.lower()
            is_claude_reason = (
                reason_l in self._CLAUDE_CAPACITY_REASONS
                or 'claude-token-pool' in reason_l
                or reason_l.startswith('claude')
            )
            if is_claude_reason and method and method not in self._CLAUDE_METHODS:
                return Response(
                    {
                        'error': (
                            'claude capacity self-disable is not valid for '
                            f'execution_method={method!r}; Hive is SoT for seat identity'
                        ),
                        'code': 'cross_provider_self_disable_rejected',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        previous_active = (
            AgentAvailability.objects
            .filter(email__iexact=request.user.email)
            .values_list('active', flat=True)
            .first()
        )
        obj, _ = AgentAvailability.objects.update_or_create(
            email=request.user.email,
            defaults={
                'active': bool(active),
                'status': 'active' if active else 'inactive',
                'reason': reason,
                'updated_by': f'self:{request.user.email}',
            },
        )
        if not bool(active) and previous_active is not False:
            # Provider/token-pool exhaustion is already a definitive bridge
            # event.  Reconcile this owner's same-team work immediately after
            # the durable availability row commits; the */10 global rebalance
            # remains a backstop instead of the load-bearing failover path.
            from .tasks import sweep_rebalance_pods

            email = obj.email
            transaction.on_commit(lambda: sweep_rebalance_pods.apply_async(
                kwargs={'only_agent_email': email},
            ))
        elif bool(active):
            # The inverse edge is equally important: an ownerless team queue
            # must become visible to a concrete agent as soon as a compatible
            # seat reports healthy.  Without this event, urgent review work
            # waits for sweep_assign_group_queued's periodic backstop. A
            # successful work report also clears this identity's stale
            # provider-down cooldown. A durable ``active=True`` row can outlive
            # an on-demand pod, so ``previous_active`` alone cannot distinguish
            # a duplicate heartbeat from a newly live seat. Always dispatch the
            # agent-scoped drain after a healthy authenticated report. The
            # drain's row locks, current-owner recheck, org/capability fences,
            # and WIP reservation make repeated reports idempotent.
            from .tasks import refresh_agent_health_and_assign_group_queue
            from .services.workflow_service import WorkflowService

            email = obj.email

            def recover_and_drain_group_queue():
                WorkflowService.mark_agent_provider_recovered(email)
                refresh_agent_health_and_assign_group_queue.apply_async(
                    kwargs={'only_agent_email': email},
                )

            transaction.on_commit(recover_and_drain_group_queue)
        return Response({'email': obj.email, 'active': obj.active, 'status': obj.status})


# =============================================================================
# Phase 3: Component ViewSet
# =============================================================================

class ComponentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Component management.
    Components organize items by functional areas within a project.
    """
    queryset = Component.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import ComponentSerializer, ComponentCreateSerializer
        if self.action == 'create':
            return ComponentCreateSerializer
        return ComponentSerializer

    def get_queryset(self):
        """Filter by project if specified."""
        queryset = Component.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        )
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.select_related('project')

    def perform_create(self, serializer):
        serializer.save()

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get items assigned to this component."""
        component = self.get_object()
        items = Item.objects.filter(component=component).order_by('-created_at')

        # Apply status filter
        status_param = request.query_params.get('status')
        if status_param:
            items = items.filter(status=status_param)

        serializer = ItemListSerializer(items, many=True)
        return Response({
            'count': items.count(),
            'items': serializer.data
        })


# =============================================================================
# Phase 3: Version ViewSet
# =============================================================================

class VersionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Version management (releases)
    Versions represent releases or milestones within a project.
    """
    queryset = Version.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import VersionSerializer, VersionCreateSerializer, VersionListSerializer
        if self.action == 'create':
            return VersionCreateSerializer
        elif self.action == 'list':
            return VersionListSerializer
        return VersionSerializer

    def get_queryset(self):
        """Filter by project and status if specified."""
        queryset = Version.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        )
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        # Option to exclude archived
        exclude_archived = self.request.query_params.get('exclude_archived', 'false').lower() == 'true'
        if exclude_archived:
            queryset = queryset.exclude(status='archived')

        return queryset.select_related('project')

    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        """Mark a version as released."""
        version = self.get_object()
        if version.status == 'released':
            return Response({
                'message': 'Version is already released',
                'released_at': version.released_at
            })

        version.release()
        return Response({
            'message': 'Version released successfully',
            'version_id': version.id,
            'name': version.name,
            'released_at': version.released_at
        })

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get items targeting this version (fix_version)."""
        version = self.get_object()
        items = version.fix_items.all().order_by('-created_at')

        # Apply status filter
        status_param = request.query_params.get('status')
        if status_param:
            items = items.filter(status=status_param)

        serializer = ItemListSerializer(items, many=True)
        return Response({
            'version': version.name,
            'count': items.count(),
            'items': serializer.data
        })

    @action(detail=True, methods=['get'])
    def affected_items(self, request, pk=None):
        """Get items that affect this version (affects_versions)."""
        version = self.get_object()
        items = version.affected_items.all().order_by('-created_at')

        serializer = ItemListSerializer(items, many=True)
        return Response({
            'version': version.name,
            'count': items.count(),
            'items': serializer.data
        })


# =============================================================================
# Phase 3: WorkLog ViewSet
# =============================================================================

class _ItemChildMutationAuthorizationMixin:
    """Authorize child-row writes against the parent item's edit policy.

    Child endpoints use POST/DELETE for their own CRUD, but all of those writes
    mutate item-owned state.  Evaluate them as an item PATCH (edit) rather than
    accidentally treating a child DELETE as authority to delete the item.
    """

    class _MethodOverrideRequest:
        def __init__(self, request, method):
            self._request = request
            self.method = method

        def __getattr__(self, name):
            return getattr(self._request, name)

    def _require_item_edit(self, item):
        permission_request = self._MethodOverrideRequest(self.request, 'PATCH')
        if not ItemProjectPermission().has_object_permission(
                permission_request, self, item):
            # Do not disclose whether a client-selected foreign parent exists.
            raise NotFound('Item not found')

    def _preflight_submitted_item(self, request):
        """Gate a client-selected parent before serializer validators run.

        This prevents uniqueness validators (notably CustomFieldValue's
        item/custom_field pair) from becoming a foreign-parent existence oracle.
        """
        if 'item' not in request.data:
            return
        try:
            item = _resolve_item(request.data.get('item'))
        except Item.DoesNotExist:
            raise NotFound('Item not found')
        self._require_item_edit(item)

    def create(self, request, *args, **kwargs):
        self._preflight_submitted_item(request)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._preflight_submitted_item(request)
        return super().update(request, *args, **kwargs)


class WorkLogViewSet(_ItemChildMutationAuthorizationMixin, viewsets.ModelViewSet):
    """
    ViewSet for WorkLog management (time tracking).
    Work logs track time spent on items.
    """
    queryset = WorkLog.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import WorkLogSerializer, WorkLogCreateSerializer
        if self.action in ['create', 'update', 'partial_update']:
            return WorkLogCreateSerializer
        return WorkLogSerializer

    def get_queryset(self):
        """Filter by item, user, or date range."""
        queryset = WorkLog.objects.all()

        # Filter by item (accepts numeric PK or item_key like "AT-83")
        item_id = self.request.query_params.get('item')
        if item_id:
            try:
                item = _resolve_item(item_id)
                queryset = queryset.filter(item_id=item.pk)
            except Item.DoesNotExist:
                return WorkLog.objects.none()

        # Filter by user
        user_id = self.request.query_params.get('user_id')
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        elif not self.request.user.is_staff:
            # Non-staff can only see their own work logs
            queryset = queryset.filter(user_id=self.request.user.id)

        # Filter by date range
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(worked_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(worked_date__lte=date_to)

        # SECURITY (PLAT-4195): parent-scope every result.  Ownership of a work
        # log is not authority to read its parent: a pre-existing/illicit log on
        # a foreign item would otherwise expose item_key/item_title forever.
        if not self.request.user.is_staff:
            accessible = PermissionService.get_user_item_queryset(self.request.user).values('id')
            queryset = queryset.filter(item_id__in=accessible)

        return queryset.select_related('item')

    def perform_create(self, serializer):
        """Set user info from authenticated user."""
        self._require_item_edit(serializer.validated_data['item'])
        serializer.save(
            user_id=self.request.user.id,
            user_email=getattr(self.request.user, 'email', '')
        )

    def perform_update(self, serializer):
        # WorkLogCreateSerializer makes item immutable, but re-check both the
        # persisted and submitted parent as defense in depth.
        self._require_item_edit(serializer.instance.item)
        self._require_item_edit(
            serializer.validated_data.get('item', serializer.instance.item))
        serializer.save()

    def perform_destroy(self, instance):
        # Deleting a work log adjusts the item's time_spent counter: item edit.
        self._require_item_edit(instance.item)
        instance.delete()

    @action(detail=False, methods=['get'])
    def my_logs(self, request):
        """Get current user's work logs."""
        logs = WorkLog.objects.filter(user_id=request.user.id).select_related('item')
        if not request.user.is_staff:
            accessible = PermissionService.get_user_item_queryset(request.user).values('id')
            logs = logs.filter(item_id__in=accessible)

        # Date filter
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            logs = logs.filter(worked_date__gte=date_from)
        if date_to:
            logs = logs.filter(worked_date__lte=date_to)

        from .serializers import WorkLogSerializer
        serializer = WorkLogSerializer(logs, many=True)
        total_minutes = sum(log.duration_minutes for log in logs)

        return Response({
            'count': logs.count(),
            'total_minutes': total_minutes,
            'total_hours': round(total_minutes / 60, 2),
            'work_logs': serializer.data
        })

    @action(detail=False, methods=['get'])
    def by_item(self, request):
        """Get work logs grouped by item (for reports)."""
        item_id = request.query_params.get('item')
        if not item_id:
            return Response(
                {'error': 'item parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Accepts numeric PK or item_key like "AT-83"
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response(
                {'error': 'Item not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # SECURITY (PLAT-4195): confirm the caller may access this item before
        # returning its work logs — previously any item's logs were readable
        # cross-tenant by id/key.
        if not request.user.is_staff and not PermissionService.get_user_item_queryset(
                request.user).filter(pk=item.pk).exists():
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        logs = WorkLog.objects.filter(item_id=item.pk).select_related('item')
        from .serializers import WorkLogSerializer
        serializer = WorkLogSerializer(logs, many=True)
        total_minutes = sum(log.duration_minutes for log in logs)

        return Response({
            'item_id': item.pk,
            'count': logs.count(),
            'total_minutes': total_minutes,
            'total_hours': round(total_minutes / 60, 2),
            'work_logs': serializer.data
        })


# =============================================================================
# Phase 3: ProjectWorkflow ViewSet
# =============================================================================

class ProjectWorkflowViewSet(viewsets.ModelViewSet):
    """
    ViewSet for ProjectWorkflow management (custom workflows).
    Each project can have its own workflow with custom statuses.
    """
    queryset = ProjectWorkflow.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import ProjectWorkflowSerializer
        return ProjectWorkflowSerializer

    def get_queryset(self):
        """Filter by project."""
        queryset = ProjectWorkflow.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        )
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        # ProjectWorkflow has no ``transitions`` relation (transitions belong
        # to Workflow).  Prefetching that nonexistent path made every detail
        # action fail with 500 before object permissions could run.
        return queryset.select_related('project')

    @action(detail=True, methods=['post'])
    def add_status(self, request, pk=None):
        """Add a new status to the workflow."""
        from .serializers import WorkflowStatusSerializer
        workflow = self.get_object()

        serializer = WorkflowStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data

        # Check for duplicate
        existing_names = [s['name'] for s in workflow.statuses]
        if new_status['name'] in existing_names:
            return Response(
                {'error': f"Status '{new_status['name']}' already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        workflow.statuses.append(new_status)
        workflow.save()

        return Response({
            'message': 'Status added successfully',
            'statuses': workflow.statuses
        })

    @action(detail=True, methods=['post'])
    def remove_status(self, request, pk=None):
        """Remove a status from the workflow."""
        workflow = self.get_object()
        status_name = request.data.get('name')

        if not status_name:
            return Response(
                {'error': 'status name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Don't allow removing initial status
        if status_name == workflow.initial_status:
            return Response(
                {'error': 'Cannot remove the initial status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if any items use this status
        items_with_status = Item.objects.filter(
            project=workflow.project,
            status=status_name
        ).count()

        if items_with_status > 0:
            return Response(
                {'error': f'{items_with_status} items use this status. Migrate them first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Remove the status
        workflow.statuses = [s for s in workflow.statuses if s['name'] != status_name]
        workflow.save()

        # Also remove related transitions
        WorkflowTransition.objects.filter(
            workflow=workflow
        ).filter(
            Q(from_status=status_name) | Q(to_status=status_name)
        ).delete()

        return Response({
            'message': 'Status removed successfully',
            'statuses': workflow.statuses
        })

    @action(detail=True, methods=['post'])
    def reorder_statuses(self, request, pk=None):
        """Reorder statuses in the workflow."""
        workflow = self.get_object()
        new_order = request.data.get('statuses', [])

        if len(new_order) != len(workflow.statuses):
            return Response(
                {'error': 'All statuses must be included in reorder'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify all status names are valid
        current_names = set(s['name'] for s in workflow.statuses)
        new_names = set(s['name'] for s in new_order)
        if current_names != new_names:
            return Response(
                {'error': 'Status names do not match'},
                status=status.HTTP_400_BAD_REQUEST
            )

        workflow.statuses = new_order
        workflow.save()

        return Response({
            'message': 'Statuses reordered successfully',
            'statuses': workflow.statuses
        })

    @action(detail=True, methods=['get'])
    def available_transitions(self, request, pk=None):
        """Get available transitions from a given status."""
        workflow = self.get_object()
        from_status = request.query_params.get('from_status')

        if not from_status:
            return Response(
                {'error': 'from_status parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        transitions = workflow.transitions.filter(
            Q(from_status=from_status) | Q(from_status='*')
        )

        from .serializers import WorkflowTransitionSerializer
        serializer = WorkflowTransitionSerializer(transitions, many=True)

        return Response({
            'from_status': from_status,
            'transitions': serializer.data
        })


# =============================================================================
# BoardColumn ViewSet
# =============================================================================

class BoardColumnViewSet(viewsets.ModelViewSet):
    """
    ViewSet for BoardColumn management.
    Board columns define Kanban/Sprint board layouts.
    """
    queryset = BoardColumn.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import BoardColumnSerializer, BoardColumnCreateSerializer
        if self.action == 'create':
            return BoardColumnCreateSerializer
        return BoardColumnSerializer

    def get_queryset(self):
        """Filter by project, scoped to the user's org-accessible projects."""
        # HIVE-1223 (HIVE-327 tenant isolation): scope board columns to the user's
        # accessible, org-scoped projects — same cross-org breach as BoardViewSet
        # (columns belong to a project/org). Staff/superusers bypass; an explicit
        # X-Organization-ID / ?organization narrows to that org.
        organization_id = _ensure_explicit_org_membership(self.request)
        visible_projects = PermissionService.get_user_projects(
            user=self.request.user, organization_id=organization_id,
        )
        queryset = BoardColumn.objects.filter(project__in=visible_projects)
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.select_related('project')

    @action(detail=False, methods=['post'])
    def reorder(self, request):
        """Reorder board columns."""
        from .serializers import BoardColumnReorderSerializer
        serializer = BoardColumnReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        column_ids = serializer.validated_data['column_ids']

        # Resolve the complete batch through the caller-scoped queryset.  A
        # mixed own/foreign list must not partially mutate foreign columns.
        columns = list(self.get_queryset().filter(id__in=column_ids))
        if len(columns) != len(set(column_ids)):
            raise NotFound('One or more board columns were not found')
        projects = {column.project_id: column.project for column in columns}
        if any(
            not RelatedProjectPermission._project_permission(
                request, project, permission='edit',
            )
            for project in projects.values()
        ):
            raise PermissionDenied('Edit permission is required to reorder board columns.')
        by_id = {column.id: column for column in columns}
        for position, column_id in enumerate(column_ids):
            column = by_id[column_id]
            column.position = position
        BoardColumn.objects.bulk_update(columns, ['position'])

        return Response({
            'message': 'Columns reordered successfully',
            'order': [str(cid) for cid in column_ids]
        })

    @action(detail=False, methods=['post'])
    def create_defaults(self, request):
        """Create default board columns for a project."""
        project_id = request.data.get('project')
        if not project_id:
            return Response(
                {'error': 'project is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            project = _require_accessible_project(request.user, project_id, permission='edit')
        except (Project.DoesNotExist, NotFound, PermissionDenied):
            return Response(
                {'error': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check if columns already exist
        if BoardColumn.objects.filter(project=project).exists():
            return Response(
                {'error': 'Board columns already exist for this project'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create default columns
        defaults = BoardColumn.get_default_columns()
        created = []
        for col_data in defaults:
            col = BoardColumn.objects.create(
                project=project,
                **col_data
            )
            created.append(col)

        from .serializers import BoardColumnSerializer
        serializer = BoardColumnSerializer(created, many=True)

        return Response({
            'message': 'Default columns created',
            'columns': serializer.data
        }, status=status.HTTP_201_CREATED)


# =============================================================================
# Phase 3: SavedFilter ViewSet
# =============================================================================

class SavedFilterViewSet(viewsets.ModelViewSet):
    """
    ViewSet for SavedFilter management (JQL saved queries).
    Users can save and share JQL queries.
    """
    queryset = SavedFilter.objects.none()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return SavedFilterCreateSerializer
        if self.action in ('update', 'partial_update'):
            return SavedFilterUpdateSerializer
        return SavedFilterSerializer

    def get_queryset(self):
        """
        Users see:
        - Their own filters
        - Shared filters for projects they can access
        """
        user = self.request.user
        project_id = self.request.query_params.get('project')

        if user.is_staff:
            queryset = SavedFilter.objects.all()
        else:
            accessible_projects = _accessible_projects_for_user(user)
            queryset = SavedFilter.objects.filter(
                Q(user_id=user.id) |
                Q(is_shared=True, project__in=accessible_projects)
            )

        if project_id:
            queryset = queryset.filter(
                Q(project_id=project_id) | Q(project__isnull=True)
            )

        return queryset.select_related('project')

    def perform_create(self, serializer):
        """Set user info from authenticated user."""
        serializer.save(
            user_id=self.request.user.id,
            user_email=getattr(self.request.user, 'email', '')
        )

    def create(self, request, *args, **kwargs):
        """Create a filter and return the full representation (id, user, etc.)."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        # Re-serialize through the read serializer so the response carries
        # id/user_id/project_key (the create serializer only exposes writable
        # fields).
        read_serializer = SavedFilterSerializer(
            serializer.instance, context=self.get_serializer_context()
        )
        headers = self.get_success_headers(read_serializer.data)
        return Response(
            read_serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def _assert_owner(self, saved_filter):
        """Only the owner (or staff) may modify/delete a saved filter."""
        # PLS-985: JWT user_id is a string claim ("user_id": "123") while
        # SavedFilter.user_id is an IntegerField; compare as ints so the
        # owner check does not fail with `123 != '123'`.
        try:
            owner_id = int(saved_filter.user_id)
            request_user_id = int(self.request.user.id)
        except (TypeError, ValueError):
            owner_id = saved_filter.user_id
            request_user_id = self.request.user.id
        if owner_id != request_user_id and not self.request.user.is_staff:
            raise PermissionDenied(
                "Only the filter owner can edit or delete this saved filter."
            )

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        instance.delete()

    @action(detail=True, methods=['post'])
    def toggle_share(self, request, pk=None):
        """Toggle the shared status of a filter."""
        saved_filter = self.get_object()

        # Only owner can toggle share
        # PLS-985: compare user ids as ints (JWT user_id is a string claim).
        try:
            _owner_id = int(saved_filter.user_id)
            _req_id = int(request.user.id)
        except (TypeError, ValueError):
            _owner_id = saved_filter.user_id
            _req_id = request.user.id
        if _owner_id != _req_id and not request.user.is_staff:
            return Response(
                {'error': 'Only the filter owner can change sharing'},
                status=status.HTTP_403_FORBIDDEN
            )

        saved_filter.is_shared = not saved_filter.is_shared
        saved_filter.save(update_fields=['is_shared', 'updated_at'])

        return Response({
            'message': f"Filter {'shared' if saved_filter.is_shared else 'unshared'}",
            'is_shared': saved_filter.is_shared
        })

    @action(detail=True, methods=['get'])
    def execute(self, request, pk=None):
        """Execute a saved filter and return results."""
        saved_filter = self.get_object()

        # Import JQL service (will be created next)
        try:
            from .services.jql_parser import JQLParser
            parser = JQLParser()
            q_object = parser.parse(saved_filter.jql, request.user)

            items = PermissionService.get_user_item_queryset(request.user).filter(
                q_object
            ).order_by('-created_at')

            # Apply project scope if filter is project-scoped
            if saved_filter.project:
                items = items.filter(project=saved_filter.project)

            serializer = ItemListSerializer(items[:100], many=True)  # Limit results

            return Response({
                'filter_name': saved_filter.name,
                'jql': saved_filter.jql,
                'count': items.count(),
                'items': serializer.data
            })
        except Exception as e:
            return Response(
                {'error': f'Failed to execute filter: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )


# =============================================================================
# Notification ViewSet — In-app notification history
# =============================================================================

class NotificationViewSet(viewsets.ModelViewSet):
    """
    In-app notification history for the authenticated user.
    Notifications are created automatically when push/Discord/Telegram
    notifications are dispatched.
    """
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Notification.objects.filter(user_id=self.request.user.id)
        # Optional filters
        is_read = self.request.query_params.get('is_read')
        if is_read is not None:
            qs = qs.filter(is_read=is_read.lower() == 'true')
        ntype = self.request.query_params.get('type')
        if ntype:
            qs = qs.filter(notification_type=ntype)
        return qs.select_related('item')

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Return the number of unread notifications."""
        count = Notification.objects.filter(
            user_id=request.user.id, is_read=False
        ).count()
        return Response({'count': count})

    @action(detail=True, methods=['post'])
    def read(self, request, pk=None):
        """Mark a single notification as read."""
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=['is_read', 'read_at'])
        return Response(NotificationSerializer(notification).data)

    @action(detail=False, methods=['post'])
    def read_all(self, request):
        """Mark all notifications as read."""
        updated = Notification.objects.filter(
            user_id=request.user.id, is_read=False
        ).update(is_read=True, read_at=timezone.now())
        return Response({'updated': updated})

    def destroy(self, request, pk=None):
        """Delete a single notification."""
        notification = self.get_object()
        notification.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'])
    def clear_all(self, request):
        """Delete all notifications for the authenticated user."""
        deleted, _ = Notification.objects.filter(
            user_id=request.user.id
        ).delete()
        return Response({'deleted': deleted})

    @action(detail=False, methods=['post'])
    def clear_read(self, request):
        """Delete all read notifications for the authenticated user."""
        deleted, _ = Notification.objects.filter(
            user_id=request.user.id, is_read=True
        ).delete()
        return Response({'deleted': deleted})


# =============================================================================
# Workflow System — Statuses, Workflows, Transitions, Schemes
# =============================================================================

class StatusViewSet(viewsets.ModelViewSet):
    """
    Global status definitions.
    Statuses are shared across workflows and categorized as todo/in_progress/done.
    """
    queryset = Status.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import StatusSerializer
        return StatusSerializer

    def get_queryset(self):
        queryset = Status.objects.all()
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)
        return queryset.order_by('position')

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            return Response(
                {'error': 'System statuses cannot be deleted'},
                status=status.HTTP_400_BAD_REQUEST
            )
        # Check if status is used in any workflow
        if instance.workflow_memberships.exists():
            return Response(
                {'error': 'Status is used in one or more workflows. Remove it from workflows first.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)


class ResolutionViewSet(viewsets.ModelViewSet):
    """
    Resolution types.
    Resolutions are set when items move to 'done' category statuses.
    """
    queryset = Resolution.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import ResolutionSerializer
        return ResolutionSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            return Response(
                {'error': 'System resolutions cannot be deleted'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)


def _lock_workflow_for_definition_write(workflow):
    """HIVE-1153 P1(b).4 (thin view-side delegate): serialize an EXISTING-workflow
    definition edit on the parent Workflow row against the capability-evidence commit
    fence. Real implementation lives in services.workflow_service so both the views
    and the service backstop share one lock primitive; imported lazily here because
    views imports workflow_service lazily throughout (a top-level import cycles).
    MUST be called inside transaction.atomic()."""
    from .services.workflow_service import lock_workflow_for_definition_write
    return lock_workflow_for_definition_write(workflow)


class WorkflowViewSet(viewsets.ModelViewSet):
    """
    Workflow management.
    Workflows define statuses and transitions, independent of projects.
    Assigned to projects via WorkflowSchemes.
    """
    queryset = Workflow.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import (
            WorkflowDetailSerializer, WorkflowListSerializer, WorkflowCreateSerializer
        )
        if self.action == 'create':
            return WorkflowCreateSerializer
        if self.action == 'list':
            return WorkflowListSerializer
        return WorkflowDetailSerializer

    def get_queryset(self):
        queryset = Workflow.objects.all()
        active_only = self.request.query_params.get('active')
        if active_only == 'true':
            queryset = queryset.filter(is_active=True)
        return queryset.prefetch_related(
            'workflow_statuses__status',
            'transitions',
        ).select_related('initial_status')

    def get_object(self):
        """Resolve by UUID, slug, or exact name.

        Task detail stores ``workflow_name`` as the slug (``simple``) or a
        display name. The task-page diagram used to list ``GET /workflows/``
        (default page_size=10) and match in memory — once the catalogue grew
        past one page the match missed, the map button toggled highlight, and
        the modal never mounted.
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        obj = self._resolve_workflow(queryset, lookup)
        if obj is None:
            raise NotFound('Workflow not found')
        self.check_object_permissions(self.request, obj)
        return obj

    @staticmethod
    def _resolve_workflow(queryset, lookup):
        if lookup is None or str(lookup).strip() == '':
            return None
        key = str(lookup).strip()
        try:
            pk = uuid.UUID(key)
        except (ValueError, AttributeError, TypeError):
            pk = None
        if pk is not None:
            found = queryset.filter(pk=pk).first()
            if found is not None:
                return found
        found = queryset.filter(slug__iexact=key).first()
        if found is not None:
            return found
        return queryset.filter(name__iexact=key).first()

    def perform_update(self, serializer):
        # HIVE-1153 P1(b).4: PATCH/PUT /workflows/{id}/ mutates an EXISTING workflow's
        # definition (name/initial_status/is_active); initial_status in particular
        # ROTATES the policy envelope — serialize on the Workflow row against the
        # capability-evidence commit fence.
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(serializer.instance)
            serializer.save()

    def perform_destroy(self, instance):
        # HIVE-1153 P1(b).4: deleting a whole workflow cascades its statuses/
        # transitions; serialize on the Workflow row against the commit fence.
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(instance)
            instance.delete()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            return Response(
                {'error': 'System workflows cannot be deleted'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def add_status(self, request, pk=None):
        """Add a status to this workflow."""
        workflow = self.get_object()
        status_id = request.data.get('status_id')
        status_slug = request.data.get('status_slug')
        position = request.data.get('position', 0)

        try:
            if status_id:
                status_obj = Status.objects.get(id=status_id)
            elif status_slug:
                status_obj = Status.objects.get(slug=status_slug)
            else:
                return Response(
                    {'error': 'status_id or status_slug is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Status.DoesNotExist:
            return Response(
                {'error': 'Status not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # HIVE-1153 P1(b).4: serialize this workflow-DEFINITION mutation on the
        # parent Workflow row (see _lock_workflow_for_definition_write).
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(workflow)
            ws, created = WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=status_obj,
                defaults={'position': position}
            )

        if not created:
            return Response(
                {'error': 'Status already in this workflow'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .serializers import WorkflowStatusInlineSerializer
        return Response(WorkflowStatusInlineSerializer(ws).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def remove_status(self, request, pk=None):
        """Remove a status from this workflow."""
        workflow = self.get_object()
        status_slug = request.data.get('status_slug')

        if not status_slug:
            return Response(
                {'error': 'status_slug is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Can't remove the initial status
        if workflow.initial_status.slug == status_slug:
            return Response(
                {'error': 'Cannot remove the initial status from the workflow'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # HIVE-1153 P1(b).4: serialize the definition mutation on the Workflow row.
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(workflow)
            deleted, _ = WorkflowStatus.objects.filter(
                workflow=workflow,
                status__slug=status_slug,
            ).delete()

            if not deleted:
                return Response(
                    {'error': 'Status not found in this workflow'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Also clean up transitions referencing this status
            WorkflowTransition.objects.filter(
                workflow=workflow
            ).filter(
                Q(from_status=status_slug) | Q(to_status=status_slug)
            ).delete()

        return Response({'message': f'Status {status_slug} removed from workflow'})

    @action(detail=True, methods=['get'])
    def transitions(self, request, pk=None):
        """List all transitions in this workflow."""
        workflow = self.get_object()
        transitions = WorkflowTransition.objects.filter(
            workflow=workflow
        ).order_by('position')

        from .serializers import WorkflowTransitionDetailSerializer
        return Response(WorkflowTransitionDetailSerializer(transitions, many=True).data)

    @action(detail=True, methods=['post'])
    def add_transition(self, request, pk=None):
        """Add a transition to this workflow."""
        workflow = self.get_object()

        from .serializers import WorkflowTransitionDetailSerializer
        serializer = WorkflowTransitionDetailSerializer(data={
            **request.data,
            'workflow': str(workflow.id),
        })
        serializer.is_valid(raise_exception=True)
        # HIVE-1153 P1(b).4: serialize the definition mutation on the Workflow row.
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(workflow)
            serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def available_transitions(self, request, pk=None):
        """Get transitions available from a given status in this workflow."""
        workflow = self.get_object()
        from_status = request.query_params.get('from_status')

        if not from_status:
            return Response(
                {'error': 'from_status parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        transitions = WorkflowTransition.objects.filter(
            workflow=workflow
        ).filter(
            Q(from_status=from_status) | Q(is_global=True)
        ).order_by('position')

        from .serializers import WorkflowTransitionDetailSerializer
        return Response({
            'from_status': from_status,
            'transitions': WorkflowTransitionDetailSerializer(transitions, many=True).data
        })


class WorkflowTransitionViewSet(viewsets.ModelViewSet):
    """
    CRUD for individual workflow transitions.
    Transitions belong to a Workflow and define allowed status changes.
    """
    queryset = WorkflowTransition.objects.filter(workflow__isnull=False)
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import WorkflowTransitionDetailSerializer
        return WorkflowTransitionDetailSerializer

    def get_queryset(self):
        queryset = WorkflowTransition.objects.filter(workflow__isnull=False)
        workflow_id = self.request.query_params.get('workflow')
        if workflow_id:
            queryset = queryset.filter(workflow_id=workflow_id)
        return queryset.select_related('workflow').order_by('position')

    # HIVE-1153 P1(b).4: direct CRUD on an individual transition mutates the
    # definition of an EXISTING workflow (the `admin_update_transition`/PATCH path),
    # so each write serializes on the parent Workflow row against the commit fence.
    def perform_create(self, serializer):
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(serializer.validated_data.get('workflow'))
            serializer.save()

    def perform_update(self, serializer):
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(
                serializer.validated_data.get('workflow') or serializer.instance.workflow)
            serializer.save()

    def perform_destroy(self, instance):
        from django.db import transaction as _txn
        with _txn.atomic():
            _lock_workflow_for_definition_write(instance.workflow)
            instance.delete()


class WorkflowSchemeViewSet(viewsets.ModelViewSet):
    """
    Workflow Scheme management.
    Schemes map issue types to workflows for projects.
    """
    queryset = WorkflowScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import WorkflowSchemeDetailSerializer, WorkflowSchemeListSerializer
        if self.action == 'list':
            return WorkflowSchemeListSerializer
        return WorkflowSchemeDetailSerializer

    def get_queryset(self):
        return WorkflowScheme.objects.all().prefetch_related(
            'mappings__workflow',
        ).select_related('default_workflow')

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            return Response(
                {'error': 'System workflow schemes cannot be deleted'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def add_mapping(self, request, pk=None):
        """Add an item_type → workflow mapping to this scheme."""
        scheme = self.get_object()
        item_type = request.data.get('item_type')
        workflow_id = request.data.get('workflow')

        if not item_type or not workflow_id:
            return Response(
                {'error': 'item_type and workflow are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            workflow = Workflow.objects.get(id=workflow_id)
        except Workflow.DoesNotExist:
            return Response(
                {'error': 'Workflow not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        mapping, created = WorkflowSchemeMapping.objects.get_or_create(
            scheme=scheme,
            item_type=item_type,
            defaults={'workflow': workflow}
        )

        if not created:
            mapping.workflow = workflow
            mapping.save()

        from .serializers import WorkflowSchemeMappingSerializer
        return Response(
            WorkflowSchemeMappingSerializer(mapping).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def remove_mapping(self, request, pk=None):
        """Remove an item_type mapping from this scheme."""
        scheme = self.get_object()
        item_type = request.data.get('item_type')

        if not item_type:
            return Response(
                {'error': 'item_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = WorkflowSchemeMapping.objects.filter(
            scheme=scheme,
            item_type=item_type,
        ).delete()

        if not deleted:
            return Response(
                {'error': f'No mapping for item_type {item_type}'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': f'Mapping for {item_type} removed'})

    @action(detail=True, methods=['post'])
    def assign_to_project(self, request, pk=None):
        """Assign this workflow scheme to a project."""
        scheme = self.get_object()
        project_id = request.data.get('project_id')

        if not project_id:
            return Response(
                {'error': 'project_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {'error': 'Project not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        from .services.workflow_service import WorkflowService
        project_workflow = WorkflowService.get_workflow_for_project(project)
        project_workflow.workflow_scheme = scheme
        project_workflow.save(update_fields=['workflow_scheme', 'updated_at'])

        return Response({
            'message': f'Workflow scheme "{scheme.name}" assigned to project {project.key}',
            'project_key': project.key,
            'scheme': scheme.name,
            'has_workflow_scheme': True,
        })

    @action(detail=True, methods=['get'])
    def resolve_workflow(self, request, pk=None):
        """Preview which workflow would be used for a given item_type."""
        scheme = self.get_object()
        item_type = request.query_params.get('item_type')

        if not item_type:
            return Response(
                {'error': 'item_type query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        workflow = scheme.get_workflow_for_item_type(item_type)
        from .serializers import WorkflowListSerializer
        return Response({
            'item_type': item_type,
            'resolved_workflow': WorkflowListSerializer(workflow).data,
        })


def _resolve_item(item_id_or_key) -> 'Item':
    """Resolve an item by numeric ID or item_key (e.g., AT-37).

    PLS-139: tolerate a leading '#'. pulse_get_my_tasks renders numeric-id
    alert tasks as '#6507', and agents pass that string straight back into
    transition/lookup calls. Without normalization, '#6507' matches neither
    the item_key branch (no '-') nor int() (ValueError), so the item is
    reported not-found and the transitions endpoint strands it at status
    'unknown'. Item keys never start with '#', so stripping a single leading
    '#' is safe for both numeric IDs and keys.
    """
    item_str = str(item_id_or_key).strip()
    if item_str.startswith('#'):
        item_str = item_str[1:].strip()
    # Try item_key first (contains a dash, e.g., AT-37)
    if '-' in item_str and not item_str.isdigit():
        try:
            return Item.objects.get(item_key=item_str)
        except Item.DoesNotExist:
            pass
        display = re.fullmatch(r'([A-Za-z][A-Za-z0-9]*)-(\d+)$', item_str)
        if display:
            item = Item.objects.filter(
                project__key__iexact=display.group(1),
                pk=int(display.group(2)),
                item_key__isnull=True,
            ).first()
            if item:
                return item
    # Try numeric ID
    try:
        return Item.objects.get(id=int(item_str))
    except (Item.DoesNotExist, ValueError):
        pass
    raise Item.DoesNotExist(f'Item {item_id_or_key} not found')


class TransitionExecuteView(APIView):
    """
    Execute a workflow transition on an item.
    POST /api/items/{item_id}/execute-transition/

    Supports workflow transitions with:
    - Screen data (popup form values)
    - Resolution selection
    - Comment during transition
    - Post-function execution
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, item_id):
        from .serializers import TransitionExecuteSerializer
        from .services.workflow_service import WorkflowService, WorkflowValidationError

        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response(
                {'error': 'Item not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if not PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists():
            raise NotFound('Item not found')
        if not ItemProjectPermission().has_object_permission(request, self, item):
            raise PermissionDenied('You do not have permission to transition this item.')

        serializer = TransitionExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        to_status_slug = serializer.validated_data.get('to_status')
        transition_id = serializer.validated_data.get('transition_id')
        old_status = item.status  # capture before transition
        old_assignee_email = item.assignee_email  # capture to detect post-function reassignment
        screen_data = serializer.validated_data.get('screen_data', {})

        # Merge resolution and comment into screen_data
        if serializer.validated_data.get('resolution'):
            screen_data['resolution'] = serializer.validated_data['resolution']
        if serializer.validated_data.get('comment') and not screen_data.get('comment'):
            screen_data['comment'] = serializer.validated_data['comment']

        try:
            from .services.deferred_workflow import maybe_enqueue
            pending = maybe_enqueue(
                item, request.user, kind='transition', payload=serializer.validated_data,
            )
            if pending:
                return Response(pending, status=status.HTTP_202_ACCEPTED)
            item = WorkflowService.execute_transition(
                item=item,
                new_status=to_status_slug,
                user=request.user,
                screen_data=screen_data,
                transition_id=transition_id,
                focus_queryset=PermissionService.get_user_item_queryset(
                    request.user
                ),
            )

            from .services.transition_finalization import finalize_transition
            finalize_transition(
                item, old_status=old_status, user=request.user,
                payload=serializer.validated_data, screen_data=screen_data,
                transition_id=transition_id,
            )

            # The post_save signal in tasks.signals already dispatches a
            # `send_item_notification.delay(..., 'status_changed', ...)`
            # celery task when `status` changes, which ultimately calls
            # `notify_status_changed`. Calling it directly here used to
            # double-deliver to the assignee (one direct POST + one via
            # Celery) so we rely solely on the signal now.
            #
            # Post-function assignee reassignments are the exception — the
            # signal will fire on the assignee_id change too, so just let
            # it propagate naturally. No explicit call.
            if item.assignee_email and item.assignee_email != old_assignee_email:
                logger.debug(
                    "Transition reassigned %s from %r to %r — signal will notify",
                    getattr(item, 'item_key', item.pk),
                    old_assignee_email, item.assignee_email,
                )

            # Enrich the response with:
            #   1. The knowledge articles attached to the transition that was JUST
            #      executed — so callers who skipped pulse_get_available_transitions
            #      still see instructions (e.g. "Merge HUMAN ONLY — STOP here").
            #   2. The legal next-hop transitions from the new status, with THEIR
            #      knowledge articles — prevents blind-chaining multiple transitions
            #      in the same turn (the real-world bug with agents auto-firing
            #      Merge → Verify Fixed without reading the Verify-Fixed article).
            from .serializers import ItemSerializer
            try:
                payload = ItemSerializer(item).data
            except Exception:
                # PLS-523: the workflow state transition already COMMITTED in
                # execute_transition() above. If rich display serialization
                # fails afterward, never convert a durable committed transition
                # into a false-negative 500 (the TRD-9 Done symptom). Return a
                # sanitized minimal HTTP-200 receipt built from the committed
                # item's own attributes so the caller sees the authoritative
                # result; the failing enrichment layer is named for operators.
                logger.exception(
                    '%s item=%s to_status=%s — transition committed but '
                    'ItemSerializer enrichment failed',
                    TRANSITION_RECEIPT_DEGRADED_MARKER,
                    getattr(item, 'item_key', None) or getattr(item, 'pk', None),
                    getattr(item, 'status', None),
                )
                return Response({
                    'id': getattr(item, 'pk', None),
                    'item_key': getattr(item, 'item_key', None),
                    'status': getattr(item, 'status', None),
                    'workflow_status': getattr(item, 'workflow_status', None),
                    'detail': 'Transition committed; display enrichment degraded.',
                    'code': 'transition_receipt_degraded',
                    'marker': TRANSITION_RECEIPT_DEGRADED_MARKER,
                    'degraded': True,
                })
            if hasattr(item, '_escalation_receipt_payload'):
                payload['escalation_receipt'] = item._escalation_receipt_payload

            try:
                # Item.workflow_name references its workflow by NAME or slug, so use
                # the canonical Workflow.resolve (name-or-slug, save-time coercion +
                # migration share it) — a bare slug= filter silently skipped the
                # executed_transition/next_transitions enrichment for the common
                # case where workflow_name holds the display name.
                from .models import Workflow
                wf = Workflow.resolve(item.workflow_name) if item.workflow_name else None
                if wf:
                    executed_transition = None
                    if transition_id:
                        executed_transition = WorkflowTransition.objects.filter(
                            workflow=wf,
                            id=transition_id,
                        ).first()
                    effective_to_status = item.status
                    if not executed_transition:
                        executed_transition = WorkflowTransition.objects.filter(
                            workflow=wf,
                            from_status=old_status,
                            to_status=effective_to_status,
                        ).order_by('position', 'name').first()
                    if executed_transition:
                        payload['executed_transition'] = {
                            'name': executed_transition.name,
                            'from_status': executed_transition.from_status,
                            'to_status': executed_transition.to_status,
                            'description': executed_transition.description,
                            'knowledge_articles': normalize_knowledge_articles(executed_transition.knowledge_articles),
                        }

                    next_transitions = WorkflowTransition.objects.filter(
                        workflow=wf,
                        from_status=effective_to_status,
                    ).order_by('position', 'name')
                    payload['next_transitions'] = [
                        {
                            'id': str(t.id),
                            'name': t.name,
                            'to_status': t.to_status,
                            'description': t.description,
                            'screen_fields': t.screen_fields or [],
                            'knowledge_articles': normalize_knowledge_articles(t.knowledge_articles),
                            'allowed_roles': t.allowed_roles or [],
                        }
                        for t in next_transitions
                    ]
            except Exception as e:  # non-fatal enrichment
                logger.warning(f'Could not enrich transition response for {item_id}: {e}')

            return Response(payload)

        except WorkflowValidationError as e:
            error_data = {
                'error': str(e),
                'missing_fields': e.missing_fields,
            }
            if e.code:
                error_data['code'] = e.code
            if e.screen_fields:
                error_data['screen_fields'] = e.screen_fields
            if e.transition_id:
                error_data['transition_id'] = e.transition_id
            if e.details:
                error_data.update(e.details)
            if e.code == 'BLOCKER_FOCUS_REQUIRED':
                # Preserve the long-standing transition error contract; only
                # the new post-snapshot conflict uses a typed top-level error.
                error_data['error'] = str(e)
            error_status = {
                'run_now_forbidden': status.HTTP_403_FORBIDDEN,
                'stale_recurring_cycle': status.HTTP_409_CONFLICT,
                'cycle_not_scheduled': status.HTTP_409_CONFLICT,
                'expected_cycle_revision_required': status.HTTP_409_CONFLICT,
                'stale_escalation_generation': status.HTTP_409_CONFLICT,
                'idempotency_key_reused': status.HTTP_409_CONFLICT,
                'FOCUS_ROOT_CHANGED': status.HTTP_409_CONFLICT,
                'escalation_forbidden': status.HTTP_403_FORBIDDEN,
                'escalation_hold_commit_failed': status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(e.code, status.HTTP_400_BAD_REQUEST)
            return Response(error_data, status=error_status)
        except DjangoValidationError as exc:
            # Model-level admission failures can be raised at the final atomic
            # status/team/assignee save boundary. They are expected routing
            # denials, not server faults. WorkflowService's finally block has
            # already released this attempt's exact WIP reservation, so callers
            # may retry immediately without waiting for reservation expiry.
            details = (
                exc.message_dict
                if hasattr(exc, 'message_dict')
                else {'non_field_errors': list(exc.messages)}
            )
            return Response({
                'error': 'transition_validation_failed',
                'details': details,
                'item_id': item.id,
                'item_key': item.item_key,
                'transition_id': str(transition_id) if transition_id else None,
            }, status=status.HTTP_409_CONFLICT)
        except OperationalError as exc:
            # PLS-913 / PLS-912: the AgentWipCapacity row lock (select_for_update
            # in _reserve_wip_slot / _verify_wip_reservation) can be held across
            # slow capacity queries long enough to exceed the DB statement
            # timeout under concurrent transitions/sweeps. Previously this
            # propagated to the structured-error middleware as a generic
            # ``server_error`` HTTP 500, which made verified tasks look stuck
            # with no actionable retry path. Detect the DB-lock/statement-timeout
            # class and return a typed 503 Retry-After instead — the transition
            # was NOT committed (the exception surfaced before the Item save
            # boundary), so the caller may retry after the backoff.
            detail = str(exc)
            is_capacity_lock = (
                'agentwipcapacity' in detail.lower()
                or 'statement timeout' in detail.lower()
                or 'canceling statement' in detail.lower()
                or 'deadlock detected' in detail.lower()
                or 'lock timeout' in detail.lower()
                or 'could not obtain lock' in detail.lower()
            )
            if is_capacity_lock:
                logger.warning(
                    'PLS-913/912 transition %s item=%s blocked on capacity-row '
                    'lock; returning typed retryable 503: %r',
                    str(transition_id) if transition_id else to_status_slug,
                    getattr(item, 'item_key', getattr(item, 'pk', '?')),
                    detail,
                )
                # The WIP lock is engaged AFTER the Item status save in
                # _execute_transition_impl (pull-next / reservation-release /
                # capacity accounting). Re-read the committed status from the DB
                # so the retryable error never lies about whether the transition
                # actually landed.
                committed = False
                try:
                    from .models import Item as _CommittedItem
                    committed_status = (
                        _CommittedItem.objects.only('status')
                        .get(pk=item.pk)
                        .status
                    )
                    committed = (
                        committed_status
                        and old_status
                        and committed_status != old_status
                    )
                except Exception:
                    committed = False
                return Response({
                    'error': 'transition_capacity_lock_timeout',
                    'detail': (
                        'The workflow capacity lock (AgentWipCapacity) is '
                        'contended by a concurrent transition or capacity '
                        'sweep. The transition was '
                        + ('applied; the post-transition capacity accounting '
                           'failed. Verify the current status before retrying.'
                           if committed else
                           'not applied; retry after the suggested backoff.')
                    ),
                    'item_id': str(item.id),
                    'item_key': item.item_key,
                    'transition_id': str(transition_id) if transition_id else None,
                    'code': 'transition_capacity_lock_timeout',
                    'committed': committed,
                    'retry_after': 30,
                }, status=status.HTTP_503_SERVICE_UNAVAILABLE,
                   headers={'Retry-After': '30'})
            raise


class ItemReturnToLaneView(APIView):
    """
    Sanctioned "cannot-execute → return-to-lane-with-reason" self-service
    action (PLAT-8222 suggestion 3).

    POST /api/items/{item_id}/return-to-lane/
    Body: {"reason": "<why the seat cannot execute this>", "reason_class": "...",
           "destination_group": "<optional team slug; defaults to the item's lane>"}

    Lets a pulled seat return work it cannot execute WITHOUT the per-item
    lead-intervention tax (today: comment + DM + andon is the only path).
    Pairs with the deployed capability-gated sweep (ryo 840ece2b): the sweep
    skips capability-mismatched seats, so a capability-mismatch return is not
    re-pulled to the same seat.

    Authority: the CURRENT assignee only (self-service). Leads/admins already
    hold assign/transition and don't need this path. A required structured
    reason + reason class keeps the return auditable, and a per-class loop
    guard refuses the third same-class return so a genuinely stuck item
    escalates instead of ping-ponging.
    """
    permission_classes = [permissions.IsAuthenticated]

    REASON_CLASSES = {
        'capability-mismatch',   # seat lacks the capability the task's tags demand
        'missing-vantage',       # seat cannot reach the surface (cluster/CI/device)
        'access-blocked',        # seat lacks the credential/permission to act
        'wrong-lane',            # task routed to a team that doesn't own this work
    }
    MAX_SAME_CLASS_RETURNS = 2  # third same-class return is refused → escalate

    def post(self, request, item_id):
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            # PLS-1361: _resolve_item RAISES DoesNotExist for unresolvable
            # ids — it never returns None, so the old `if item is None`
            # branch below was dead code and an unresolvable id reached DRF
            # as an uncaught exception → the bare structured-500 envelope.
            # The admin-ops seat saw exactly that on item 34519 (HIVE-2523):
            # 3 failures across 3 days, retrying after the suggested 30s
            # backoff each time. A wrong id is a typed 404, not a 500.
            return Response(
                {'error': f'Item {item_id} not found — nothing to return.',
                 'item_id': str(item_id)},
                status=status.HTTP_404_NOT_FOUND)
        if item is None:
            return Response({'error': 'Item not found'},
                            status=status.HTTP_404_NOT_FOUND)
        if not PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists():
            raise NotFound('Item not found')
        if not ItemProjectPermission().has_object_permission(request, self, item):
            raise PermissionDenied('You do not have permission on this item.')

        actor_email = (getattr(request.user, 'email', '') or '').strip().lower()
        assignee_email = (item.assignee_email or '').strip().lower()
        if not assignee_email:
            return Response(
                {'error': 'Item has no concrete assignee to return it — nothing to do.',
                 'item_key': item.item_key},
                status=status.HTTP_409_CONFLICT)
        if actor_email != assignee_email:
            return Response(
                {'error': (
                    f'Only the current assignee ({assignee_email}) may return this item '
                    f'self-service. Leads/admins: use assign/transition instead.'),
                 'item_key': item.item_key,
                 'current_assignee': assignee_email},
                status=status.HTTP_403_FORBIDDEN)

        reason = str(request.data.get('reason') or '').strip()
        reason_class = str(request.data.get('reason_class') or '').strip().lower()
        destination_group = str(request.data.get('destination_group') or '').strip()
        if len(reason) < 10:
            return Response(
                {'error': 'A real reason (>= 10 chars) is required — the return must be auditable.',
                 'reason_classes': sorted(self.REASON_CLASSES)},
                status=status.HTTP_400_BAD_REQUEST)
        if reason_class not in self.REASON_CLASSES:
            return Response(
                {'error': f"reason_class must be one of: {', '.join(sorted(self.REASON_CLASSES))}"},
                status=status.HTTP_400_BAD_REQUEST)

        # Loop guard: a third same-class return means the routing fix didn't
        # hold — refuse and force escalation instead of a silent ping-pong.
        meta = dict(item.metadata or {})
        returns = meta.get('cannot_execute_returns')
        if not isinstance(returns, list):
            returns = []
        # PLAT-9119: bind the list back into meta BEFORE appending — the old
        # `meta.get(...) or []` left the first return's entry on a throwaway
        # list, so the ledger (and with it this loop guard) never persisted.
        meta['cannot_execute_returns'] = returns
        same_class = [r for r in returns if isinstance(r, dict) and r.get('reason_class') == reason_class]
        if len(same_class) >= self.MAX_SAME_CLASS_RETURNS:
            # PLS-1406: a refused third return must not leave the item pinned
            # on the seat that just refused it. Drop the assignee and exclude
            # that seat from the next pull.
            excluded = meta.get('pull_excluded_emails')
            if not isinstance(excluded, list):
                excluded = []
            if actor_email and actor_email not in excluded:
                excluded.append(actor_email)
            meta['pull_excluded_emails'] = excluded
            meta['return_cap_hold'] = {
                'reason_class': reason_class,
                'by': actor_email,
                'at': timezone.now().isoformat(),
            }
            item.metadata = meta
            item._activity_actor = (request.user.id, actor_email)
            item._substrate_write_intent = 'user'
            item.save(update_fields=['metadata', 'updated_at'])
            item.unassign(reason='return_cap_exhausted')
            return Response(
                {'error': (
                    f'This item was already returned {len(same_class)}x for reason_class '
                    f"'{reason_class}'. It is off {actor_email}'s eligible set and back "
                    f'on the owner-group queue.'),
                 'item_key': item.item_key,
                 'routed_to_group_queue': True,
                 'pull_excluded_emails': excluded,
                 'prior_returns': same_class},
                status=status.HTTP_409_CONFLICT)

        lane = item.assignment_group
        if destination_group:
            from .services.workflow_service import WorkflowService
            canonical = WorkflowService._canonical_team_name(destination_group)
            if not WorkflowService._team_registered_for_item(canonical, item):
                return Response(
                    {'error': f"Destination team '{destination_group}' is not registered for this task organization.",
                     'item_key': item.item_key},
                    status=status.HTTP_400_BAD_REQUEST)
            item.assignment_group = canonical
            lane = canonical

        returns.append({
            'at': timezone.now().isoformat(),
            'by': actor_email,
            'reason_class': reason_class,
            'reason': reason,
            'lane': lane,
        })
        item.metadata = meta
        item._activity_actor = (request.user.id, actor_email)
        item._substrate_write_intent = 'user'
        # PLS-1361: the write phase is one transaction so a failure cannot
        # strand half a return (ledger entry + destination group but the
        # assignee still held). A per-item deterministic state error is a
        # typed 409 — the seat can tell "this item's state refused it" from
        # "the route is broken" instead of reading another bare 500.
        try:
            with transaction.atomic():
                item.save(update_fields=['metadata', 'assignment_group', 'updated_at'])
                item.unassign(reason='cannot_execute_return')
        except (IntegrityError, DjangoValidationError, ValidationError) as exc:
            logger.exception('return-to-lane write failed for %s', item.item_key)
            return Response(
                {'error': (
                     'The return could not be applied to this item in its '
                     'current state — its routing state refused the clear.'),
                 'item_key': item.item_key,
                 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT)

        try:
            Comment.objects.create(
                item=item,
                author_id=request.user.id,
                author_email=actor_email,
                content=(
                    f"↩️ Returned to lane **{lane}** by {actor_email} "
                    f"(cannot-execute: `{reason_class}`) — {reason} "
                    f"[PLAT-8222 self-service return]"),
            )
        except Exception:
            logger.exception('return-to-lane audit comment failed for %s', item.item_key)

        return Response({
            'item_key': item.item_key,
            'returned_by': actor_email,
            'reason_class': reason_class,
            'lane': lane,
            'note': ('Task returned to the lane queue unassigned. The capability-gated '
                     'sweep will not re-pull it to a seat lacking the demanded capability.'),
        })


# =============================================================================
# Issue Type, Screen, Permission, SLA ViewSets
# =============================================================================


class ItemAvailableTransitionsView(APIView):
    """Get available transitions for an item based on its current status and workflow."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, item_id):
        from .services.workflow_service import WorkflowService
        from .models import Workflow, WorkflowTransition

        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)
        if not PermissionService.get_user_item_queryset(request.user).filter(pk=item.pk).exists():
            raise NotFound('Item not found')

        # Prefer item.status (canonical slug) over item.workflow_status
        # (display label like "To Do"). Some older items carry display
        # strings in workflow_status; the slug in item.status is what
        # WorkflowTransition.from_status actually stores.
        current_status = item.status or item.workflow_status

        # Build a permissive set of slug candidates so we tolerate the
        # mixed conventions in the wild — display names ("To Do"),
        # hyphen slugs ("in-progress"), spaced labels ("In Progress"),
        # and squashed forms ("todo" vs "to_do"). Transitions in the DB
        # are always lowercase, but the separator is inconsistent.
        def _slug_candidates(s):
            if not s:
                return []
            base = s.strip().lower()
            return list({
                base,
                base.replace('-', '_'),
                base.replace('-', '_').replace(' ', '_'),
                base.replace('-', '').replace(' ', ''),
                base.replace(' ', '_'),
                base.replace(' ', ''),
            })

        # Canonical slug from the Status table — looked up by slug-guess
        # or by display name — so "To Do" resolves to "todo", not "to_do".
        # Used as the canonical `current_status_normalized` feeding LEGACY
        # and the downstream filter.
        current_status_normalized = ''
        if current_status:
            from .models import Status as _StatusCanon
            _raw = current_status.strip().lower().replace('-', '_')
            _slug_guess = _raw.replace(' ', '_')
            _row = (
                _StatusCanon.objects.filter(slug__iexact=_slug_guess).first()
                or _StatusCanon.objects.filter(name__iexact=current_status).first()
            )
            current_status_normalized = _row.slug if _row else _slug_guess

        candidates = _slug_candidates(current_status)
        if item.workflow_status and item.workflow_status != current_status:
            candidates.extend(_slug_candidates(item.workflow_status))
        # Always include the canonical resolved slug in the candidate set
        # so the downstream filter matches the DB form regardless of
        # whether the display name happened to normalize cleanly.
        if current_status_normalized and current_status_normalized not in candidates:
            candidates.append(current_status_normalized)

        if current_status_normalized == 'deferred':
            return Response({
                'current_status': current_status,
                'transitions': [],
            })

        # Map legacy Pulse statuses to Jira workflow equivalents
        LEGACY_STATUS_MAP = {
            'pending': 'todo',
            'completed': 'done',
            'cancelled': 'cancelled',
        }
        legacy_mapped = LEGACY_STATUS_MAP.get(current_status_normalized)

        # Try workflow first (by workflow_name on the task).
        # workflow_name is the DISPLAY name ("Channel Message Review"),
        # whereas Workflow.slug is kebab-case ("channel-message-review").
        # Try exact name match first, then a family of slug derivations.
        if item.workflow_name:
            wf = Workflow.objects.filter(name__iexact=item.workflow_name).first()
            if not wf:
                base = item.workflow_name.lower()
                spaced_kebab = '-'.join(base.split())
                spaced_snake = '_'.join(base.split())
                # Local name — distinct from the status-slug `candidates`
                # built above, which feeds the transition filter below.
                wf_candidates = {
                    base, base.replace('-', '_'), base.replace('_', '-'),
                    spaced_kebab, spaced_snake,
                }
                for s in wf_candidates:
                    wf = Workflow.objects.filter(slug__iexact=s).first()
                    if wf:
                        break
            try:
                if not wf:
                    raise Workflow.DoesNotExist()
                # Build the from_statuses set: slug candidates + legacy map
                from_statuses = list(dict.fromkeys(candidates + [current_status_normalized]))
                if legacy_mapped and legacy_mapped not in from_statuses:
                    from_statuses.append(legacy_mapped)
                # Include global transitions (is_global=True) — those fire from
                # ANY current state and are how a workflow expresses "this
                # action is always available" (e.g., "Mark Done", "Schedule").
                transitions = WorkflowTransition.objects.filter(
                    workflow=wf,
                ).filter(
                    Q(from_status__in=from_statuses) | Q(is_global=True)
                ).order_by('position')
                # A global transition that targets the current status is normally a
                # no-op — drop it so the picker doesn't show "Mark Done" while already
                # done. EXCEPTION (operator 2026-06-23): keep a same-status transition
                # that RE-ROUTES the work — one whose post-functions reassign the owning
                # team/owner (assign_team / assign_to / assign_implementer). Reassigning
                # is a meaningful action even when the status is unchanged, e.g.
                # "Escalate to DevOps" (→ todo, assign_team: devops). The old unconditional
                # drop hid the DevOps lane on fresh `todo` blockers, so agents only saw the
                # operator-escalation lanes (→ blocked) and work funneled to the operator.
                _REROUTE_PF_TYPES = {'assign_team', 'assign_to', 'assign_implementer', 'assign'}

                def _reroutes_work(t):
                    return any(
                        isinstance(pf, dict) and pf.get('type') in _REROUTE_PF_TYPES
                        for pf in (t.post_functions or [])
                    )

                transitions = [
                    t for t in transitions
                    if (
                        t.to_status not in from_statuses or _reroutes_work(t)
                    )
                    and WorkflowService._transition_is_visible_for_item(t, item)
                ]

                # Dedupe by (from_status, name). The Simple workflow keeps
                # parallel legacy/modern paths (done→pending AND done→todo,
                # both named "Reopen") so legacy items still work; but
                # rendering both as separate buttons confuses users. Collapse
                # same-named siblings, preferring the one whose to_status
                # equals the workflow's initial status.
                initial_slug = (
                    wf.initial_status.slug if wf.initial_status_id else None
                )
                grouped = {}
                for t in transitions:
                    key = (t.from_status, t.name)
                    existing = grouped.get(key)
                    if existing is None:
                        grouped[key] = t
                        continue
                    # Prefer the transition that targets the workflow's
                    # initial status; fall back to position order.
                    if t.to_status == initial_slug and existing.to_status != initial_slug:
                        grouped[key] = t

                result = []
                for t in grouped.values():
                    from .models import Status as StatusModel
                    try:
                        target = StatusModel.objects.get(slug=t.to_status)
                        to_label = target.name
                    except StatusModel.DoesNotExist:
                        to_label = t.to_status
                    result.append({
                        'id': str(t.id),
                        'to_status': t.to_status,
                        'to_status_label': to_label,
                        'name': t.name,
                        'description': t.description,
                        'button_style': t.button_style,
                        'post_functions': t.post_functions,
                        'screen_fields': t.screen_fields,
                        'knowledge_articles': normalize_knowledge_articles(t.knowledge_articles),
                    })

                return Response({
                    'current_status': current_status,
                    'transitions': result,
                })
            except Workflow.DoesNotExist:
                pass

        # Fall back to project workflow
        if not item.project:
            return Response({'transitions': []})

        transitions = WorkflowService.get_available_transitions(
            project=item.project,
            from_status=current_status,
            item=item,
            user=request.user,
        )

        return Response({
            'current_status': current_status,
            'transitions': transitions,
        })


EXTERNAL_MCP_READ_TOOLS = {
    'pulse_get_task',
    'pulse_search_tasks',
    'pulse_list_tasks',
    'pulse_get_available_transitions',
    'pulse_list_comments',
    'pulse_get_issue_links',
}

EXTERNAL_MCP_WRITE_TOOL_SCOPES = {
    'pulse_create_task': {'task.write'},
    'pulse_update_task': {'task.write'},
    'pulse_link_issues': {'task.write'},
    'pulse_add_comment': {'comment.write'},
    'pulse_execute_transition': {'transition.execute'},
}
EXTERNAL_MCP_WRITE_TOOLS = set(EXTERNAL_MCP_WRITE_TOOL_SCOPES)


def _external_mcp_tool_catalog():
    return [
        {
            'name': 'pulse_get_task',
            'description': 'Read one Pulse task by item key or UUID/PK.',
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
            },
        },
        {
            'name': 'pulse_search_tasks',
            'description': 'Search visible tasks by title/description.',
            'input_schema': {
                'type': 'object',
                'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
                'required': ['query'],
            },
        },
        {
            'name': 'pulse_list_tasks',
            'description': 'List visible tasks, optionally filtered by project/status.',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'project_id': {'type': 'integer'},
                    'status': {'type': 'string'},
                    'limit': {'type': 'integer'},
                },
            },
        },
        {
            'name': 'pulse_get_available_transitions',
            'description': 'List workflow transitions visible for a task without executing them.',
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
            },
        },
        {
            'name': 'pulse_list_comments',
            'description': 'List comments for a visible task.',
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}, 'limit': {'type': 'integer'}},
                'required': ['task_id'],
            },
        },
        {
            'name': 'pulse_get_issue_links',
            'description': 'List issue links for a visible task.',
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
            },
        },
        {
            'name': 'pulse_create_task',
            'description': 'Create a task in an allowed project after duplicate-search review.',
            'required_scopes': ['task.write'],
            'input_schema': {
                'type': 'object',
                'properties': {
                    'project_id': {'type': 'integer'},
                    'title': {'type': 'string'},
                    'description': {'type': 'string'},
                    'workflow': {'type': 'string'},
                    'assignment_group': {'type': 'string'},
                    'priority': {'type': 'string'},
                    'duplicate_acknowledged': {'type': 'boolean'},
                },
                'required': ['project_id', 'title'],
            },
        },
        {
            'name': 'pulse_update_task',
            'description': 'Update non-status fields on a visible task.',
            'required_scopes': ['task.write'],
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
            },
        },
        {
            'name': 'pulse_link_issues',
            'description': 'Create an issue link between two visible tasks.',
            'required_scopes': ['task.write'],
            'input_schema': {
                'type': 'object',
                'properties': {
                    'source_task_id': {'type': 'string'},
                    'target_task_id': {'type': 'string'},
                    'link_type': {'type': 'string'},
                },
                'required': ['source_task_id', 'target_task_id', 'link_type'],
            },
        },
        {
            'name': 'pulse_add_comment',
            'description': 'Add a provenance-tagged comment to a visible task.',
            'required_scopes': ['comment.write'],
            'input_schema': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['task_id', 'content'],
            },
        },
        {
            'name': 'pulse_execute_transition',
            'description': 'Execute a workflow transition through Pulse validators.',
            'required_scopes': ['transition.execute'],
            'input_schema': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'to_status': {'type': 'string'},
                    'transition_id': {'type': 'string'},
                    'screen_data': {'type': 'object'},
                },
                'required': ['task_id'],
            },
        },
    ]


def _external_mcp_request_hash(payload):
    try:
        raw = json.dumps(payload or {}, sort_keys=True, separators=(',', ':'), default=str)
    except TypeError:
        raw = str(payload)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _external_mcp_audit(*, token=None, tool_name='', task_id='', request=None,
                        result_status='success', error_code=''):
    client = getattr(token, 'client', None)
    idempotency_key = ''
    request_payload = {}
    if request is not None:
        request_payload = getattr(request, 'data', {}) or {}
        arguments = request_payload.get('arguments') if isinstance(request_payload, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        idempotency_key = (
            request.headers.get('Idempotency-Key')
            or request_payload.get('idempotency_key')
            or arguments.get('idempotency_key')
            or ''
        )
    ExternalMcpCallAudit.objects.create(
        org_id=getattr(client, 'org_id', None),
        client=client,
        actor_user_id=getattr(client, 'owner_user_id', None),
        tool_name=tool_name or '',
        task_id=str(task_id or ''),
        idempotency_key=str(idempotency_key or ''),
        request_hash=_external_mcp_request_hash(request_payload),
        result_status=result_status,
        error_code=error_code or '',
    )


def _external_mcp_authorize(request):
    auth = request.headers.get('Authorization', '')
    if not auth.lower().startswith('bearer '):
        _external_mcp_audit(request=request, tool_name='auth', result_status='denied', error_code='missing_bearer')
        return None, Response({'error': 'missing_bearer_token'}, status=status.HTTP_401_UNAUTHORIZED)
    raw_token = auth.split(' ', 1)[1].strip()
    token_hash = ExternalMcpToken.hash_token(raw_token)
    token = (
        ExternalMcpToken.objects
        .select_related('client')
        .filter(token_hash=token_hash)
        .first()
    )
    if not token or not token.is_active():
        _external_mcp_audit(token=token, request=request, tool_name='auth', result_status='denied', error_code='invalid_token')
        return None, Response({'error': 'invalid_or_inactive_token'}, status=status.HTTP_401_UNAUTHORIZED)

    scopes = set(token.client.scopes or []) | set(token.scopes or [])
    if 'task.read' not in scopes:
        _external_mcp_audit(token=token, request=request, tool_name='auth', result_status='denied', error_code='missing_scope')
        return None, Response({'error': 'missing_scope', 'required': 'task.read'}, status=status.HTTP_403_FORBIDDEN)

    policy = token.client.rate_limit_policy or {}
    per_minute = int(policy.get('per_minute') or 60)
    cache_key = f'external-mcp-rate:{token.id}:{int(time.time() // 60)}'
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=90)
        count = 1
    if count > per_minute:
        _external_mcp_audit(token=token, request=request, tool_name='rate_limit', result_status='denied', error_code='rate_limited')
        return None, Response({'error': 'rate_limited'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    ExternalMcpToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token, None


def _external_mcp_scopes(token):
    return set(token.client.scopes or []) | set(token.scopes or [])


def _external_mcp_actor(token):
    class ExternalMcpActor:
        pass
    actor = ExternalMcpActor()
    actor.id = token.client.owner_user_id
    actor.pk = token.client.owner_user_id
    actor.email = token.client.owner_email or ''
    actor.username = actor.email or f'external-mcp-{token.client_id}'
    actor.is_authenticated = True
    actor.is_active = True
    actor.is_staff = False
    actor.is_superuser = False
    return actor


class ExternalMcpToolError(Exception):
    def __init__(self, error, status_code=status.HTTP_400_BAD_REQUEST, payload=None):
        super().__init__(error)
        self.error = error
        self.status_code = status_code
        self.payload = payload or {'error': error}


def _external_mcp_idempotency_key(request):
    payload = getattr(request, 'data', {}) or {}
    arguments = payload.get('arguments') if isinstance(payload, dict) else {}
    if not isinstance(arguments, dict):
        arguments = {}
    return (
        request.headers.get('Idempotency-Key')
        or payload.get('idempotency_key')
        or arguments.get('idempotency_key')
        or ''
    ).strip()


def _external_mcp_require_idempotency(request, tool):
    key = _external_mcp_idempotency_key(request)
    if not key:
        raise ExternalMcpToolError(
            'idempotency_key_required',
            status.HTTP_400_BAD_REQUEST,
            {'error': 'idempotency_key_required', 'tool': tool},
        )
    return key


def _external_mcp_idempotent_replay(token, tool, request):
    key = _external_mcp_idempotency_key(request)
    if not key:
        return None
    req_hash = _external_mcp_request_hash(getattr(request, 'data', {}) or {})
    audit = (
        ExternalMcpCallAudit.objects
        .filter(client=token.client, tool_name=tool, idempotency_key=key, result_status='success')
        .order_by('-created_at')
        .first()
    )
    if not audit:
        return None
    if audit.request_hash and audit.request_hash != req_hash:
        raise ExternalMcpToolError(
            'idempotency_key_conflict',
            status.HTTP_409_CONFLICT,
            {'error': 'idempotency_key_conflict', 'idempotency_key': key},
        )
    item = None
    if audit.task_id:
        item = _external_mcp_get_item(token, audit.task_id)
    return {'audit_id': str(audit.id), 'item': item}


def _external_mcp_provenance(token, tool, request):
    key = _external_mcp_idempotency_key(request)
    return (
        f"External MCP `{tool}` by client `{token.client.name}` "
        f"(client_id={token.client_id}, actor={token.client.owner_email or token.client.owner_user_id}, "
        f"idempotency_key={key or 'none'})."
    )


def _external_mcp_write_policy(token):
    policy = token.client.rate_limit_policy or {}
    write_policy = policy.get('write_policy') or policy.get('external_mcp_write_policy') or {}
    return write_policy if isinstance(write_policy, dict) else {}


def _external_mcp_enforce_write_policy(token, tool, arguments):
    policy = _external_mcp_write_policy(token)
    if not policy:
        return
    if policy.get('require_confirmation') and not arguments.get('confirmed'):
        raise ExternalMcpToolError(
            'confirmation_required',
            status.HTTP_428_PRECONDITION_REQUIRED,
            {
                'error': 'confirmation_required',
                'tool': tool,
                'policy': 'require_confirmation',
            },
        )
    if policy.get('draft_only') and tool == 'pulse_execute_transition':
        raise ExternalMcpToolError(
            'draft_only_policy_denied',
            status.HTTP_403_FORBIDDEN,
            {
                'error': 'draft_only_policy_denied',
                'tool': tool,
                'policy': 'draft_only',
            },
        )


def _external_mcp_add_provenance_comment(item, token, tool, request, extra=''):
    content = _external_mcp_provenance(token, tool, request)
    if extra:
        content = f"{content} {extra}"
    Comment.objects.create(
        item=item,
        author_id=token.client.owner_user_id,
        author_email=token.client.owner_email or 'external-mcp@pulse.local',
        content=content,
    )


def _external_mcp_allowed_project(token, project_id):
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        raise PermissionDenied('Invalid or missing project_id.')
    if pid not in _external_mcp_allowed_projects(token):
        raise PermissionDenied('Project not in this token scope.')
    project = Project.objects.filter(id=pid, org_id=token.client.org_id, is_active=True).first()
    if not project:
        raise PermissionDenied('Project not found in this token scope.')
    return project


def _external_mcp_allowed_projects(token):
    ids = token.client.allowed_project_ids or []
    return {int(pid) for pid in ids}


def _external_mcp_visible_items(token):
    allowed_project_ids = _external_mcp_allowed_projects(token)
    qs = Item.objects.select_related('project').filter(
        mode='task',
        project__org_id=token.client.org_id,
        project_id__in=allowed_project_ids,
    )
    return qs


def _external_mcp_get_item(token, identifier):
    normalized, _kind, _suggestion = _describe_item_identifier(identifier)
    qs = _external_mcp_visible_items(token)
    item = qs.filter(item_key__iexact=normalized).first()
    if item:
        return item
    try:
        return qs.get(pk=normalized)
    except (Item.DoesNotExist, ValueError, TypeError):
        return None


def _external_mcp_item_payload(item):
    project = item.project
    return {
        'id': str(item.id),
        'item_key': item.item_key,
        'title': item.title,
        'description': item.description,
        'status': item.status,
        'workflow_status': item.workflow_status,
        'workflow_name': item.workflow_name,
        'priority': item.priority,
        'project_id': item.project_id,
        'project_key': project.key if project else None,
        'org_id': project.org_id if project else None,
        'assignee_email': item.assignee_email,
        'assignment_group': item.assignment_group,
        'created_at': item.created_at.isoformat() if item.created_at else None,
        'updated_at': item.updated_at.isoformat() if item.updated_at else None,
    }


def _external_mcp_available_transitions(item):
    if not item.workflow_name:
        return []
    wf = Workflow.resolve(item.workflow_name)
    if not wf:
        return []
    current_status = item.status or item.workflow_status
    transitions = WorkflowTransition.objects.filter(
        workflow=wf,
    ).filter(
        Q(from_status=current_status) | Q(is_global=True)
    ).order_by('position', 'name')
    from .services.workflow_service import WorkflowService
    return [
        {
            'id': str(t.id),
            'name': t.name,
            'to_status': t.to_status,
            'description': t.description,
            'screen_fields': (
                WorkflowService._ensure_cancel_reason_category_field(t.screen_fields)
                if t.to_status == 'cancelled'
                else (t.screen_fields or [])
            ),
            'knowledge_articles': normalize_knowledge_articles(t.knowledge_articles),
        }
        for t in transitions
        if t.to_status != current_status or t.post_functions
    ]


def _external_mcp_issue_links(token, item):
    visible_ids = set(_external_mcp_visible_items(token).values_list('id', flat=True))
    outward = [
        {
            'id': str(link.id),
            'link_type': link.link_type,
            'direction': 'outward',
            'target': _external_mcp_item_payload(link.target_item),
        }
        for link in item.outward_links.select_related('target_item__project').all()
        if link.target_item_id in visible_ids
    ]
    inward = [
        {
            'id': str(link.id),
            'link_type': ItemLink.INVERSE_TYPES.get(link.link_type, link.link_type),
            'direction': 'inward',
            'target': _external_mcp_item_payload(link.source_item),
        }
        for link in item.inward_links.select_related('source_item__project').all()
        if link.source_item_id in visible_ids
    ]
    return outward + inward


class ExternalMcpToolsView(APIView):
    """External MCP discovery: tools filtered by token scope."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        token, error = _external_mcp_authorize(request)
        if error:
            return error
        scopes = _external_mcp_scopes(token)
        tools = [
            tool for tool in _external_mcp_tool_catalog()
            if set(tool.get('required_scopes') or ['task.read']).issubset(scopes)
        ]
        _external_mcp_audit(token=token, request=request, tool_name='tools.discover', result_status='success')
        return Response({
            'org_id': token.client.org_id,
            'client_id': str(token.client_id),
            'tools': tools,
        })


class ExternalMcpCallView(APIView):
    """External MCP call facade over tenant-scoped Pulse tools."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        token, error = _external_mcp_authorize(request)
        if error:
            return error
        tool = str(request.data.get('tool') or request.data.get('name') or '')
        arguments = request.data.get('arguments') or {}
        allowed_tools = EXTERNAL_MCP_READ_TOOLS | EXTERNAL_MCP_WRITE_TOOLS
        if tool not in allowed_tools:
            _external_mcp_audit(token=token, request=request, tool_name=tool, result_status='denied', error_code='tool_not_allowed')
            return Response({'error': 'tool_not_allowed', 'tool': tool}, status=status.HTTP_403_FORBIDDEN)
        scopes = _external_mcp_scopes(token)
        required = {'task.read'} if tool in EXTERNAL_MCP_READ_TOOLS else EXTERNAL_MCP_WRITE_TOOL_SCOPES[tool]
        if not required.issubset(scopes):
            _external_mcp_audit(token=token, request=request, tool_name=tool, result_status='denied', error_code='scope_denied')
            return Response({'error': 'scope_denied', 'required': sorted(required)}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = self._execute(token, tool, arguments, request=request)
        except ExternalMcpToolError as exc:
            _external_mcp_audit(token=token, request=request, tool_name=tool, result_status='denied', error_code=exc.error)
            return Response(exc.payload, status=exc.status_code)
        except PermissionDenied as exc:
            _external_mcp_audit(token=token, request=request, tool_name=tool, result_status='denied', error_code='scope_denied')
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as exc:
            logger.warning("external MCP call failed", exc_info=True)
            _external_mcp_audit(token=token, request=request, tool_name=tool, result_status='error', error_code=exc.__class__.__name__)
            return Response({'error': 'tool_call_failed', 'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task_id = ''
        if isinstance(result, dict):
            task_id = result.pop('_audit_task_id', '') or ''
        task_id = task_id or arguments.get('task_id') or arguments.get('item_key') or arguments.get('item_id') or ''
        _external_mcp_audit(token=token, request=request, tool_name=tool, task_id=task_id, result_status='success')
        return Response({'tool': tool, 'result': result},
                        status=status.HTTP_202_ACCEPTED if isinstance(result, dict) and result.get('pending') else status.HTTP_200_OK)

    def _execute(self, token, tool, arguments, request=None):
        qs = _external_mcp_visible_items(token)
        if tool == 'pulse_get_task':
            item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
            if not item:
                raise PermissionDenied('Task not found in this token scope.')
            payload = _external_mcp_item_payload(item)
            from .services.workflow_service import WorkflowService
            owner_email = (token.client.owner_email or '').strip()
            if WorkflowService._emails_same_agent_identity(
                owner_email, item.assignee_email,
            ):
                focus = WorkflowService._higher_impact_owned_blocker(
                    item,
                    owner_email,
                    visible_queryset=_external_mcp_visible_items(token),
                )
                if focus is not None:
                    payload['focus_guard'] = {
                        'required_task_key': focus.item_key or str(focus.pk),
                        'reason': (
                            'Owned runnable blocker root releases active blocked '
                            'descendants and must be worked before this task.'
                        ),
                    }
            return payload
        if tool == 'pulse_search_tasks':
            query = str(arguments.get('query') or '').strip()
            limit = min(int(arguments.get('limit') or 20), 50)
            results = qs.filter(Q(title__icontains=query) | Q(description__icontains=query)).order_by('-updated_at')[:limit]
            return [_external_mcp_item_payload(item) for item in results]
        if tool == 'pulse_list_tasks':
            if arguments.get('project_id'):
                qs = qs.filter(project_id=int(arguments['project_id']))
            if arguments.get('status'):
                qs = qs.filter(status=str(arguments['status']))
            limit = min(int(arguments.get('limit') or 20), 50)
            return [_external_mcp_item_payload(item) for item in qs.order_by('-updated_at')[:limit]]
        if tool == 'pulse_get_available_transitions':
            item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
            if not item:
                raise PermissionDenied('Task not found in this token scope.')
            return {'current_status': item.status, 'transitions': _external_mcp_available_transitions(item)}
        if tool == 'pulse_list_comments':
            item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
            if not item:
                raise PermissionDenied('Task not found in this token scope.')
            limit = min(int(arguments.get('limit') or 20), 50)
            comments = item.comments.order_by('-created_at')[:limit]
            return [
                {
                    'id': comment.id,
                    'author_email': comment.author_email,
                    'content': comment.content,
                    'created_at': comment.created_at.isoformat() if comment.created_at else None,
                }
                for comment in comments
            ]
        if tool == 'pulse_get_issue_links':
            item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
            if not item:
                raise PermissionDenied('Task not found in this token scope.')
            return _external_mcp_issue_links(token, item)
        if tool == 'pulse_create_task':
            return self._create_task(token, arguments, request)
        if tool == 'pulse_update_task':
            return self._update_task(token, arguments, request)
        if tool == 'pulse_link_issues':
            return self._link_issues(token, arguments, request)
        if tool == 'pulse_add_comment':
            return self._add_comment(token, arguments, request)
        if tool == 'pulse_execute_transition':
            return self._execute_transition(token, arguments, request)
        raise PermissionDenied('Tool not allowed.')

    def _create_task(self, token, arguments, request):
        _external_mcp_require_idempotency(request, 'pulse_create_task')
        _external_mcp_enforce_write_policy(token, 'pulse_create_task', arguments)
        replay = _external_mcp_idempotent_replay(token, 'pulse_create_task', request)
        if replay:
            return {
                'idempotent_replay': True,
                'task': _external_mcp_item_payload(replay['item']) if replay['item'] else None,
                '_audit_task_id': getattr(replay['item'], 'pk', '') if replay['item'] else '',
            }

        project = _external_mcp_allowed_project(token, arguments.get('project_id'))
        title = str(arguments.get('title') or '').strip()
        if not title:
            raise ExternalMcpToolError('title_required', status.HTTP_400_BAD_REQUEST)
        dup_query = str(arguments.get('duplicate_query') or title).strip()
        candidates = []
        if dup_query:
            candidates = list(
                _external_mcp_visible_items(token)
                .filter(project=project)
                .filter(Q(title__icontains=dup_query) | Q(description__icontains=dup_query))
                .order_by('-updated_at')[:5]
            )
        if candidates and not arguments.get('duplicate_acknowledged'):
            raise ExternalMcpToolError(
                'duplicate_candidates_found',
                status.HTTP_409_CONFLICT,
                {'error': 'duplicate_candidates_found', 'candidates': [_external_mcp_item_payload(i) for i in candidates]},
            )

        metadata = {
            'external_mcp': {
                'client_id': str(token.client_id),
                'idempotency_key': _external_mcp_idempotency_key(request),
                'duplicate_candidates': [c.item_key for c in candidates],
            }
        }
        labels = arguments.get('labels') or {}
        if not isinstance(labels, dict):
            labels = {'external_mcp_labels': labels}
        if _external_mcp_write_policy(token).get('draft_only'):
            labels = dict(labels)
            labels['external_mcp_draft'] = True
            metadata['external_mcp']['draft_only'] = True

        item = Item.objects.create(
            mode='task',
            project=project,
            item_key=project.get_next_item_key(),
            title=title,
            description=str(arguments.get('description') or ''),
            priority=str(arguments.get('priority') or 'normal'),
            workflow_name=str(arguments.get('workflow') or arguments.get('workflow_name') or 'simple'),
            assignment_group=arguments.get('assignment_group') or None,
            labels=labels,
            tags=','.join(arguments.get('tags')) if isinstance(arguments.get('tags'), list) else (arguments.get('tags') or None),
            story_points=arguments.get('story_points') or None,
            created_by_id=token.client.owner_user_id,
            created_by_email=token.client.owner_email or '',
            source='external_mcp',
            source_id=_external_mcp_idempotency_key(request),
            metadata=metadata,
        )
        _external_mcp_add_provenance_comment(item, token, 'pulse_create_task', request, 'Created task via scoped external write tool.')
        payload = _external_mcp_item_payload(item)
        if hasattr(item, '_escalation_receipt_payload'):
            payload['escalation_receipt'] = item._escalation_receipt_payload
        payload['_audit_task_id'] = item.pk
        return payload

    def _update_task(self, token, arguments, request):
        _external_mcp_require_idempotency(request, 'pulse_update_task')
        _external_mcp_enforce_write_policy(token, 'pulse_update_task', arguments)
        replay = _external_mcp_idempotent_replay(token, 'pulse_update_task', request)
        if replay:
            return {
                'idempotent_replay': True,
                'task': _external_mcp_item_payload(replay['item']) if replay['item'] else None,
                '_audit_task_id': getattr(replay['item'], 'pk', '') if replay['item'] else '',
            }
        item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
        if not item:
            raise PermissionDenied('Task not found in this token scope.')
        forbidden = {'status', 'workflow_status', 'resolution', 'resolution_date', 'completed_at'} & set(arguments)
        if forbidden:
            raise ExternalMcpToolError(
                'status_update_forbidden',
                status.HTTP_400_BAD_REQUEST,
                {
                    'error': 'status_update_forbidden',
                    'fields': sorted(forbidden),
                    'detail': 'Use pulse_execute_transition for workflow state changes.',
                },
            )
        allowed = {'title', 'description', 'priority', 'assignment_group', 'assignee_email', 'labels', 'tags', 'story_points'}
        update_fields = []
        for field in allowed:
            if field in arguments:
                setattr(item, field, arguments[field])
                update_fields.append(field)
        if update_fields:
            update_fields.append('updated_at')
            item.save(update_fields=update_fields)
            _external_mcp_add_provenance_comment(item, token, 'pulse_update_task', request, f"Updated fields: {', '.join(sorted(set(update_fields) - {'updated_at'}))}.")
        payload = _external_mcp_item_payload(item)
        payload['_audit_task_id'] = item.pk
        return payload

    def _link_issues(self, token, arguments, request):
        _external_mcp_require_idempotency(request, 'pulse_link_issues')
        _external_mcp_enforce_write_policy(token, 'pulse_link_issues', arguments)
        replay = _external_mcp_idempotent_replay(token, 'pulse_link_issues', request)
        if replay:
            return {'idempotent_replay': True, '_audit_task_id': getattr(replay['item'], 'pk', '') if replay.get('item') else ''}
        source = _external_mcp_get_item(token, arguments.get('source_task_id') or arguments.get('source') or arguments.get('task_id'))
        target = _external_mcp_get_item(token, arguments.get('target_task_id') or arguments.get('target'))
        if not source or not target:
            raise PermissionDenied('Both linked tasks must be visible in this token scope.')
        link_type = str(arguments.get('link_type') or 'relates_to')
        link, created = ItemLink.objects.get_or_create(
            source_item=source,
            target_item=target,
            link_type=link_type,
            defaults={
                'created_by_id': token.client.owner_user_id,
                'created_by_email': token.client.owner_email or '',
            },
        )
        _external_mcp_add_provenance_comment(source, token, 'pulse_link_issues', request, f"Linked `{source.item_key}` {link_type} `{target.item_key}`.")
        return {'id': str(link.id), 'created': created, 'link_type': link.link_type, '_audit_task_id': source.pk}

    def _add_comment(self, token, arguments, request):
        _external_mcp_require_idempotency(request, 'pulse_add_comment')
        _external_mcp_enforce_write_policy(token, 'pulse_add_comment', arguments)
        replay = _external_mcp_idempotent_replay(token, 'pulse_add_comment', request)
        if replay:
            return {
                'idempotent_replay': True,
                'task': _external_mcp_item_payload(replay['item']) if replay['item'] else None,
                '_audit_task_id': getattr(replay['item'], 'pk', '') if replay['item'] else '',
            }
        item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
        if not item:
            raise PermissionDenied('Task not found in this token scope.')
        content = str(arguments.get('content') or '').strip()
        if not content:
            raise ExternalMcpToolError('content_required', status.HTTP_400_BAD_REQUEST)
        from .services.workflow_service import WorkflowService
        owner_email = (token.client.owner_email or '').strip()
        if (
            # PLS-846 follow-up: same human-account exemption as the DRF
            # comment path — a human (operator/CEO) recording a decision must
            # not be frozen on an unrelated owned root. The guard is an
            # agent-queue mechanism; humans have no stale runtime.
            not WorkflowService._is_human_account(owner_email)
            and WorkflowService._emails_same_agent_identity(
                owner_email, item.assignee_email,
            )
            # PLS-737: guard non-fire on classification unavailable.
            and WorkflowService._slot_kind_for_guard(item.status) != 'active'
        ):
            focus = WorkflowService._higher_impact_owned_blocker(
                item,
                owner_email,
                visible_queryset=_external_mcp_visible_items(token),
            )
            if focus is not None:
                code, diagnostic = WorkflowService._focus_guard_diagnostic(
                    item, owner_email, focus,
                    # PLS-709: this is pulse_add_comment over external MCP --
                    # a comment, not a transition. Without this it raises 409
                    # carrying "Cannot start/resume", which is the same
                    # borrowed-reason defect on the surface external clients
                    # actually call.
                    surface='comment',
                )
                # PLS-722: this is the surface that matters — every motivating
                # incident (BKSG-49, PLAT-5106, PLAT-5384) came through the MCP
                # tool, not the DRF endpoint. Preserving on one path only would
                # ship a refusal that is accurately worded and still destroys
                # the work. Same helper as the DRF branch so the dedupe key
                # cannot drift between the two doors.
                _preserve_focus_refused_body(
                    item=item,
                    author_email=owner_email,
                    causal_actor=_external_mcp_actor(token),
                    content=content,
                    diagnostic=diagnostic,
                )
                raise ExternalMcpToolError(
                    diagnostic['error'],
                    status.HTTP_409_CONFLICT,
                    {**diagnostic, 'code': code},
                )
        comment = Comment.objects.create(
            item=item,
            author_id=token.client.owner_user_id,
            author_email=token.client.owner_email or 'external-mcp@pulse.local',
            content=f"{content}\n\n---\n{_external_mcp_provenance(token, 'pulse_add_comment', request)}",
        )
        return {'id': comment.id, 'task': _external_mcp_item_payload(item), '_audit_task_id': item.pk}

    def _execute_transition(self, token, arguments, request):
        _external_mcp_require_idempotency(request, 'pulse_execute_transition')
        _external_mcp_enforce_write_policy(token, 'pulse_execute_transition', arguments)
        replay = _external_mcp_idempotent_replay(token, 'pulse_execute_transition', request)
        if replay:
            from .models import DeferredWorkflowCommand
            from .services.deferred_workflow import operation_payload
            command = DeferredWorkflowCommand.objects.filter(
                external_token__client=token.client,
                payload___idempotency_key=_external_mcp_idempotency_key(request),
            ).order_by('-created_at').first()
            if command and replay['item']:
                return {**operation_payload(command, replay['item']),
                        'idempotent_replay': True, '_audit_task_id': replay['item'].pk}
            return {
                'idempotent_replay': True,
                'task': _external_mcp_item_payload(replay['item']) if replay['item'] else None,
                '_audit_task_id': getattr(replay['item'], 'pk', '') if replay['item'] else '',
            }
        item = _external_mcp_get_item(token, arguments.get('task_id') or arguments.get('item_id') or arguments.get('item_key'))
        if not item:
            raise PermissionDenied('Task not found in this token scope.')
        from .services.workflow_service import WorkflowService, WorkflowValidationError
        _original_mcp_status = (item.status or '').strip().lower()
        try:
            from .services.deferred_workflow import maybe_enqueue
            pending = maybe_enqueue(
                item, _external_mcp_actor(token), kind='external',
                payload={**{k: arguments[k] for k in ('to_status', 'transition_id', 'screen_data') if k in arguments},
                         '_idempotency_key': _external_mcp_idempotency_key(request)},
                external_token=token,
            )
            if pending:
                return {**pending, '_audit_task_id': item.pk}
            item = WorkflowService.execute_transition(
                item,
                arguments.get('to_status'),
                user=_external_mcp_actor(token),
                screen_data=arguments.get('screen_data') or {},
                transition_id=arguments.get('transition_id'),
                focus_queryset=_external_mcp_visible_items(token),
            )
        except WorkflowValidationError as exc:
            error_status = {
                'run_now_forbidden': status.HTTP_403_FORBIDDEN,
                'stale_recurring_cycle': status.HTTP_409_CONFLICT,
                'cycle_not_scheduled': status.HTTP_409_CONFLICT,
                'expected_cycle_revision_required': status.HTTP_409_CONFLICT,
                'stale_escalation_generation': status.HTTP_409_CONFLICT,
                'idempotency_key_reused': status.HTTP_409_CONFLICT,
                'FOCUS_ROOT_CHANGED': status.HTTP_409_CONFLICT,
                'escalation_forbidden': status.HTTP_403_FORBIDDEN,
                'escalation_hold_commit_failed': status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.code, status.HTTP_400_BAD_REQUEST)
            detail = {'error': str(exc)}
            if exc.code:
                detail['code'] = exc.code
            if exc.details:
                detail.update(exc.details)
            if exc.code == 'BLOCKER_FOCUS_REQUIRED':
                detail['error'] = str(exc)
            raise ExternalMcpToolError('transition_validation_failed', error_status, detail)
        except OperationalError as exc:
            # PLS-913 / PLS-912: the AgentWipCapacity row lock
            # (select_for_update) can be held across slow capacity queries long
            # enough to exceed the DB statement timeout under concurrent
            # transitions/sweeps. Previously this propagated to the structured
            # middleware as a generic ``server_error`` HTTP 500, which made
            # verified tasks look stuck with no actionable retry path. Surface
            # a typed retryable error (503 + Retry-After) instead, mirroring
            # the DRF TransitionExecuteView.post handler.
            detail = str(exc)
            is_capacity_lock = (
                'agentwipcapacity' in detail.lower()
                or 'statement timeout' in detail.lower()
                or 'canceling statement' in detail.lower()
                or 'deadlock detected' in detail.lower()
                or 'lock timeout' in detail.lower()
                or 'could not obtain lock' in detail.lower()
            )
            if is_capacity_lock:
                committed = False
                try:
                    from ..models import Item as _CommittedItem
                    committed_status = (
                        _CommittedItem.objects.only('status')
                        .get(pk=item.pk)
                        .status
                    )
                    committed = (
                        committed_status
                        and committed_status.strip().lower()
                        != _original_mcp_status
                    )
                except Exception:
                    committed = False
                raise ExternalMcpToolError(
                    'transition_capacity_lock_timeout',
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    {
                        'error': 'transition_capacity_lock_timeout',
                        'code': 'transition_capacity_lock_timeout',
                        'detail': (
                            'The workflow capacity lock (AgentWipCapacity) is '
                            'contended by a concurrent transition or capacity '
                            'sweep. The transition was '
                            + (
                                'applied; the post-transition capacity '
                                'accounting failed. Verify the current status '
                                'before retrying.'
                                if committed
                                else 'not applied; retry after the suggested backoff.'
                            )
                        ),
                        'item_id': str(item.pk),
                        'item_key': item.item_key,
                        'transition_id': arguments.get('transition_id'),
                        'committed': committed,
                        'retry_after': 30,
                    },
                )
            raise
        _external_mcp_add_provenance_comment(
            item, token, 'pulse_execute_transition', request,
            f"Transitioned to `{item.status}` through workflow engine.",
        )
        payload = _external_mcp_item_payload(item)
        if hasattr(item, '_escalation_receipt_payload'):
            payload['escalation_receipt'] = item._escalation_receipt_payload
        payload['_audit_task_id'] = item.pk
        return payload


class ItemRunNowView(APIView):
    """
    Manually fire the next occurrence of a recurring task that is currently
    parked in the `scheduled` gap state. Effectively the same code path as
    `fire_recurring_due` but triggered by the user instead of the timer —
    useful for "I know I want to run the weekly scan now, not next Monday".

    Requires the item to be (1) recurring and (2) currently in a status whose
    category is `scheduled` or `done` (the two states the scheduler normally
    fires from). Any other state returns 409 to avoid clobbering in-progress
    work.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, item_id):
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        from .services.recurring_cycle_state import (
            RecurringCycleError, manual_start_cycle,
        )
        data = request.data or {}
        try:
            result = manual_start_cycle(
                item.pk,
                user=request.user,
                expected_cycle_revision=data.get('expected_cycle_revision'),
                expected_generation=data.get('expected_generation'),
                expected_due_at=data.get('expected_due_at') or data.get('expected_due_boundary'),
                expected_completion_watermark=data.get('expected_completion_watermark'),
                request_event_id=data.get('recurring_event_id') or data.get('event_id'),
                authorized_override=data.get('authorized_override', False),
                reason=data.get('reason') or data.get('comment') or '',
            )
        except RecurringCycleError as exc:
            code_status = {
                'run_now_forbidden': status.HTTP_403_FORBIDDEN,
                'stale_recurring_cycle': status.HTTP_409_CONFLICT,
                'cycle_not_scheduled': status.HTTP_409_CONFLICT,
                'expected_cycle_revision_required': status.HTTP_409_CONFLICT,
                'not_recurring': status.HTTP_400_BAD_REQUEST,
                'no_next_due': status.HTTP_400_BAD_REQUEST,
            }
            # PLS-792: a stale/required error carries the current cycle snapshot so a
            # client can Refresh->Run Now without private implementation knowledge.
            body = {'error': str(exc), 'code': exc.code}
            if getattr(exc, 'current', None):
                body['current_recurring_cycle'] = exc.current
            return Response(
                body,
                status=code_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
            )

        if result.kind == 'routing_hold':
            return Response(
                {
                    'error': (
                        'The recurring template route has no eligible staffed '
                        f'owner or fallback ({result.reason}).'
                    ),
                    'code': 'recurrence_routing_hold',
                    'item_id': result.item.id,
                    'cycle_revision': result.revision,
                    'generation': result.generation,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            'item_id': result.item.id,
            'item_key': result.item.item_key,
            'status': result.item.status,
            'workflow_status': result.item.workflow_status,
            'message': (
                'idempotent replay' if result.replay
                else f'{result.old_status} -> {result.new_status}'
            ),
            'idempotent_replay': result.replay,
            'cycle_revision': result.revision,
            'generation': result.generation,
            'event_id': result.event_id,
            'next_boundary': result.next_due.isoformat() if result.next_due else None,
        })


class ItemSnoozeView(APIView):
    """
    Manually park a recurring task in the `scheduled` gap-state with a
    user-chosen next_fire_at. Inverse of Run Now: instead of waking the
    task up, you defer its next cycle to a specific time.

    Body accepts either:
      * `next_fire_at`: ISO-8601 datetime (absolute)
      * `duration_seconds`: integer (relative — e.g. 3600 for "1 hour from now")

    Industry pattern: Linear "Snooze", ServiceNow "Schedule for later".
    Scoped to recurring tasks only — non-recurring deferral should use
    a date-picker on due_date, not a state transition.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, item_id):
        from datetime import datetime, timedelta
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        if not getattr(item, 'is_recurring', False) or not item.frequency or item.frequency == 'one_time':
            return Response(
                {'error': 'Snooze is only available on recurring tasks'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve next_fire_at — either absolute or relative.
        next_fire_at = None
        iso = request.data.get('next_fire_at')
        duration = request.data.get('duration_seconds')
        if iso:
            try:
                next_fire_at = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
                if timezone.is_naive(next_fire_at):
                    next_fire_at = timezone.make_aware(next_fire_at)
            except (ValueError, TypeError):
                return Response(
                    {'error': f'Invalid next_fire_at: {iso!r}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif duration is not None:
            try:
                secs = int(duration)
                if secs <= 0:
                    raise ValueError('must be positive')
                next_fire_at = timezone.now() + timedelta(seconds=secs)
            except (ValueError, TypeError):
                return Response(
                    {'error': f'Invalid duration_seconds: {duration!r}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            return Response(
                {'error': 'Provide either next_fire_at (ISO) or duration_seconds (int)'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Don't snooze to a past time.
        if next_fire_at <= timezone.now():
            return Response(
                {'error': 'next_fire_at must be in the future'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = item.status
        item.status = 'scheduled'
        item.workflow_status = 'Scheduled'
        item.due_date = next_fire_at
        item.percent_complete = 0
        item.resolution = None
        item.resolution_date = None
        item.overdue_notified = False
        item.reminder_sent = False
        item.completed_at = None
        item._activity_actor = (
            getattr(request.user, 'id', None) or 0,
            getattr(request.user, 'email', None) or 'scheduler@pulse.shizuha.com',
        )
        item._activity_status_action = 'recurring_snoozed'
        item.save(update_fields=[
            'status', 'workflow_status', 'due_date', 'percent_complete',
            'resolution', 'resolution_date', 'overdue_notified',
            'reminder_sent', 'completed_at', 'updated_at',
        ])

        return Response({
            'item_id': item.id,
            'item_key': item.item_key,
            'status': item.status,
            'workflow_status': item.workflow_status,
            'next_fire_at': item.due_date.isoformat(),
            'message': f'{old_status} -> scheduled (snoozed until {item.due_date.isoformat()})',
        })


class ItemChangeWorkflowView(APIView):
    """
    Swap an item's workflow.

    Body: {"workflow": "<name-or-slug>", "status"?: "<target-slug>"}

    Industry pattern (Jira workflow scheme migration, Linear team move):
      1. The server auto-maps the current status into the target workflow
         via Item._coerce_invalid_status() — legacy-alias → category match
         → new workflow's initial status.
      2. Callers may override the target slug explicitly via `status` for
         cases where the auto-map doesn't pick what they want (Jira's
         scheme migration wizard does this per-issue).

    Logs an activity row attributed to the requester with semantic action
    `workflow_changed` so the history shows the old+new workflow names.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, item_id):
        try:
            item = _resolve_item(item_id)
        except Item.DoesNotExist:
            return Response({'error': 'Item not found'}, status=status.HTTP_404_NOT_FOUND)

        target_name = (request.data.get('workflow') or '').strip()
        if not target_name:
            return Response(
                {'error': 'Provide workflow name or slug'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve target workflow — try name exact, then slug derivations.
        from .models import Workflow as WorkflowModel
        wf = WorkflowModel.objects.filter(name__iexact=target_name).first()
        if not wf:
            base = target_name.lower()
            for s in {base, base.replace('-', '_'), base.replace('_', '-'),
                      '-'.join(base.split()), '_'.join(base.split())}:
                wf = WorkflowModel.objects.filter(slug__iexact=s).first()
                if wf:
                    break
        if not wf:
            return Response(
                {'error': f'Unknown workflow: {target_name!r}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # No-op if already on this workflow.
        if (item.workflow_name or '').lower() == wf.name.lower():
            return Response({
                'item_id': item.id,
                'item_key': item.item_key,
                'workflow_name': item.workflow_name,
                'status': item.status,
                'workflow_status': item.workflow_status,
                'message': 'No change — already on this workflow',
            })

        old_workflow = item.workflow_name or ''
        old_status = item.status

        # If caller explicitly provided a target status, validate it exists
        # in the new workflow; otherwise let the save-time coercion auto-map.
        requested_status = request.data.get('status')
        if requested_status:
            valid_slugs = set(
                wf.workflow_statuses.values_list('status__slug', flat=True)
            )
            if requested_status not in valid_slugs:
                return Response(
                    {
                        'error': (
                            f'{requested_status!r} is not a valid status in '
                            f'{wf.name!r}. Valid: {sorted(valid_slugs)}'
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            item.status = requested_status

        item.workflow_name = wf.name
        # Clear workflow_status so _sync_status_fields recomputes from the
        # (possibly healed) slug against the new workflow's statuses.
        item.workflow_status = None

        item._activity_actor = (
            getattr(request.user, 'id', None) or 0,
            getattr(request.user, 'email', None) or 'scheduler@pulse.shizuha.com',
        )
        item._activity_status_action = 'workflow_changed'
        item.save()  # full save so post_save signal fires with healing + sync

        return Response({
            'item_id': item.id,
            'item_key': item.item_key,
            'workflow_name': item.workflow_name,
            'status': item.status,
            'workflow_status': item.workflow_status,
            'message': f"{old_workflow!r} -> {item.workflow_name!r}; "
                       f"status {old_status!r} -> {item.status!r}",
        })


class IssueTypeViewSet(viewsets.ModelViewSet):
    """CRUD for issue types (Bug, Story, Task, Epic, etc.)."""
    queryset = IssueType.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import IssueTypeSerializer
        return IssueTypeSerializer


class IssueTypeSchemeViewSet(viewsets.ModelViewSet):
    """
    Issue Type Scheme management.
    Schemes define which issue types are available for a project.
    """
    queryset = IssueTypeScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import IssueTypeSchemeSerializer
        return IssueTypeSchemeSerializer

    def get_queryset(self):
        return IssueTypeScheme.objects.all().prefetch_related('mappings__issue_type')

    @action(detail=True, methods=['post'])
    def add_mapping(self, request, pk=None):
        """Add an issue type to this scheme."""
        scheme = self.get_object()
        issue_type_id = request.data.get('issue_type')
        position = request.data.get('position', 0)

        if not issue_type_id:
            return Response(
                {'error': 'issue_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            issue_type = IssueType.objects.get(id=issue_type_id)
        except IssueType.DoesNotExist:
            return Response(
                {'error': 'Issue type not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        mapping, created = IssueTypeSchemeMapping.objects.get_or_create(
            scheme=scheme,
            issue_type=issue_type,
            defaults={'position': position}
        )

        if not created:
            return Response(
                {'error': 'Issue type already in this scheme'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .serializers import IssueTypeSchemeMappingSerializer
        return Response(IssueTypeSchemeMappingSerializer(mapping).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'])
    def remove_mapping(self, request, pk=None):
        """Remove an issue type mapping from this scheme."""
        scheme = self.get_object()
        issue_type_id = request.data.get('issue_type')

        if not issue_type_id:
            return Response(
                {'error': 'issue_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = IssueTypeSchemeMapping.objects.filter(
            scheme=scheme,
            issue_type_id=issue_type_id,
        ).delete()

        if not deleted:
            return Response(
                {'error': 'Mapping not found in this scheme'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': 'Issue type mapping removed'})


class CustomFieldViewSet(viewsets.ModelViewSet):
    """CRUD for custom fields."""
    queryset = CustomField.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import CustomFieldSerializer
        return CustomFieldSerializer


class CustomFieldContextViewSet(viewsets.ModelViewSet):
    """CRUD for custom field contexts."""
    queryset = CustomFieldContext.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import CustomFieldContextSerializer
        return CustomFieldContextSerializer


class CustomFieldValueViewSet(_ItemChildMutationAuthorizationMixin, viewsets.ModelViewSet):
    """CRUD for custom field values, filterable by item and custom field."""
    queryset = CustomFieldValue.objects.all()
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import CustomFieldValueSerializer
        return CustomFieldValueSerializer

    def get_queryset(self):
        # SECURITY (PLAT-4195): scope to custom-field values on items the caller
        # can access. Previously this was CustomFieldValue.objects.all() filtered
        # only by the optional item_id/custom_field_id params, so a detail fetch
        # or `?item_id=<other-org item>` leaked another tenant's field values.
        queryset = CustomFieldValue.objects.all()
        if not self.request.user.is_staff:
            accessible = PermissionService.get_user_item_queryset(self.request.user).values('id')
            queryset = queryset.filter(item_id__in=accessible)
        item_id = self.request.query_params.get('item_id')
        custom_field_id = self.request.query_params.get('custom_field_id')
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        if custom_field_id:
            queryset = queryset.filter(custom_field_id=custom_field_id)
        return queryset.select_related('item', 'custom_field')

    def perform_create(self, serializer):
        self._require_item_edit(serializer.validated_data['item'])
        serializer.save()

    def perform_update(self, serializer):
        # Reparenting changes two item-owned records; require edit authority on
        # both the original and the submitted target.
        self._require_item_edit(serializer.instance.item)
        self._require_item_edit(
            serializer.validated_data.get('item', serializer.instance.item))
        serializer.save()

    def perform_destroy(self, instance):
        # Removing a field value is an item edit, not item deletion authority.
        self._require_item_edit(instance.item)
        instance.delete()


class ScreenViewSet(viewsets.ModelViewSet):
    """CRUD for screens (field layouts for create/edit/transition)."""
    queryset = Screen.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import ScreenSerializer
        return ScreenSerializer


class ScreenSchemeViewSet(viewsets.ModelViewSet):
    """CRUD for screen schemes."""
    queryset = ScreenScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import ScreenSchemeSerializer
        return ScreenSchemeSerializer


class IssueTypeScreenSchemeViewSet(viewsets.ModelViewSet):
    """CRUD for issue type screen schemes (map issue types to screen schemes)."""
    queryset = IssueTypeScreenScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import IssueTypeScreenSchemeSerializer
        return IssueTypeScreenSchemeSerializer

    def get_queryset(self):
        return IssueTypeScreenScheme.objects.all().prefetch_related('mappings')


class SecuritySchemeViewSet(viewsets.ModelViewSet):
    """
    Security Scheme management.
    Security schemes define access levels for items.
    """
    queryset = SecurityScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import SecuritySchemeSerializer
        return SecuritySchemeSerializer

    def get_queryset(self):
        return SecurityScheme.objects.all().prefetch_related('levels')

    @action(detail=True, methods=['post'])
    def add_level(self, request, pk=None):
        """Add a security level to this scheme."""
        scheme = self.get_object()
        name = request.data.get('name')
        description = request.data.get('description', '')
        position = request.data.get('position', 0)

        if not name:
            return Response(
                {'error': 'name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        slug = request.data.get('slug', '')
        members = request.data.get('members', [])

        level = SecurityLevel.objects.create(
            scheme=scheme,
            name=name,
            slug=slug or name.lower().replace(' ', '_'),
            description=description,
            position=position,
            members=members,
        )

        from .serializers import SecurityLevelSerializer
        return Response(SecurityLevelSerializer(level).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'])
    def remove_level(self, request, pk=None):
        """Remove a security level from this scheme."""
        scheme = self.get_object()
        level_id = request.data.get('level_id')

        if not level_id:
            return Response(
                {'error': 'level_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = SecurityLevel.objects.filter(
            scheme=scheme,
            id=level_id,
        ).delete()

        if not deleted:
            return Response(
                {'error': 'Security level not found in this scheme'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': 'Security level removed'})


class PermissionSchemeViewSet(viewsets.ModelViewSet):
    """
    Permission Scheme management.
    Permission schemes define who can do what on items.
    """
    queryset = PermissionScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import PermissionSchemeSerializer
        return PermissionSchemeSerializer

    def get_queryset(self):
        return PermissionScheme.objects.all().prefetch_related('grants')

    @action(detail=True, methods=['post'])
    def add_grant(self, request, pk=None):
        """Add a permission grant to this scheme."""
        scheme = self.get_object()
        permission = request.data.get('permission')
        holder_type = request.data.get('holder_type')
        holder_value = request.data.get('holder_value', '')

        if not permission or not holder_type:
            return Response(
                {'error': 'permission and holder_type are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        grant, created = PermissionGrant.objects.get_or_create(
            scheme=scheme,
            permission=permission,
            holder_type=holder_type,
            holder_value=holder_value,
        )

        if not created:
            return Response(
                {'error': 'This permission grant already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .serializers import PermissionGrantSerializer
        return Response(PermissionGrantSerializer(grant).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'])
    def remove_grant(self, request, pk=None):
        """Remove a permission grant from this scheme."""
        scheme = self.get_object()
        grant_id = request.data.get('grant_id')

        if not grant_id:
            return Response(
                {'error': 'grant_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = PermissionGrant.objects.filter(
            scheme=scheme,
            id=grant_id,
        ).delete()

        if not deleted:
            return Response(
                {'error': 'Permission grant not found in this scheme'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': 'Permission grant removed'})


class NotificationSchemeViewSet(viewsets.ModelViewSet):
    """
    Notification Scheme management.
    Notification schemes define who gets notified on which events.
    """
    queryset = NotificationScheme.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import NotificationSchemeSerializer
        return NotificationSchemeSerializer

    def get_queryset(self):
        return NotificationScheme.objects.all().prefetch_related('events')

    @action(detail=True, methods=['post'])
    def add_event(self, request, pk=None):
        """Add an event notification to this scheme."""
        scheme = self.get_object()
        event_type = request.data.get('event_type')
        recipient_type = request.data.get('recipient_type')
        recipient_value = request.data.get('recipient_value', '')

        if not event_type or not recipient_type:
            return Response(
                {'error': 'event_type and recipient_type are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        event, created = NotificationSchemeEvent.objects.get_or_create(
            scheme=scheme,
            event_type=event_type,
            recipient_type=recipient_type,
            defaults={'recipient_value': recipient_value}
        )

        if not created:
            return Response(
                {'error': 'This event notification already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .serializers import NotificationSchemeEventSerializer
        return Response(NotificationSchemeEventSerializer(event).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'])
    def remove_event(self, request, pk=None):
        """Remove an event notification from this scheme."""
        scheme = self.get_object()
        event_id = request.data.get('event_id')

        if not event_id:
            return Response(
                {'error': 'event_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = NotificationSchemeEvent.objects.filter(
            scheme=scheme,
            id=event_id,
        ).delete()

        if not deleted:
            return Response(
                {'error': 'Event notification not found in this scheme'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': 'Event notification removed'})


class SLADefinitionViewSet(viewsets.ModelViewSet):
    """CRUD for SLA definitions, filterable by project."""
    queryset = SLADefinition.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import SLADefinitionSerializer
        return SLADefinitionSerializer

    def get_queryset(self):
        queryset = SLADefinition.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        )
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.select_related('project', 'calendar')


class BusinessCalendarViewSet(viewsets.ModelViewSet):
    """CRUD for business calendars (working hours, holidays)."""
    queryset = BusinessCalendar.objects.all()
    permission_classes = [permissions.IsAuthenticated, GlobalConfigurationPermission]

    def get_serializer_class(self):
        from .serializers import BusinessCalendarSerializer
        return BusinessCalendarSerializer


class SLAInstanceViewSet(viewsets.mixins.ListModelMixin,
                         viewsets.mixins.RetrieveModelMixin,
                         viewsets.GenericViewSet):
    """Read-only access to SLA instances, filterable by item and SLA definition."""
    queryset = SLAInstance.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import SLAInstanceSerializer
        return SLAInstanceSerializer

    def get_queryset(self):
        visible_projects = _accessible_projects_for_user(self.request.user)
        queryset = SLAInstance.objects.filter(
            Q(item__project__in=visible_projects)
            | Q(item__project__isnull=True, sla_definition__project__in=visible_projects)
        )
        item_ref = self.request.query_params.get('item')
        sla_definition_id = self.request.query_params.get('sla_definition')
        if item_ref:
            # PLAT-2690: `item` accepts EITHER a numeric FK id OR an item key
            # (e.g. `PLS-237`). Callers (the MCP `pulse_list_sla_instances`
            # tool, the UI) pass the human item key; filtering `item_id=<key>`
            # tried to cast the string to the integer FK → ValueError → HTTP 500.
            # Resolve non-numeric refs by item_key instead.
            if str(item_ref).isdigit():
                queryset = queryset.filter(item_id=int(item_ref))
            else:
                queryset = queryset.filter(item__item_key=item_ref)
        if sla_definition_id:
            # `SLADefinition.id` is a UUID primary key, so preserve the
            # existing `?sla_definition=<uuid>` contract while also allowing a
            # human-readable name. Non-UUID refs must not be sent to the FK
            # field because that raises ValueError -> HTTP 500.
            try:
                queryset = queryset.filter(sla_definition_id=uuid.UUID(str(sla_definition_id)))
            except (TypeError, ValueError, AttributeError):
                queryset = queryset.filter(sla_definition__name=sla_definition_id)
        return queryset.select_related('sla_definition', 'item')


class BoardViewSet(viewsets.ModelViewSet):
    """CRUD for boards (Scrum/Kanban), filterable by project."""
    queryset = Board.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import BoardSerializer
        return BoardSerializer

    def get_queryset(self):
        # HIVE-1223 (HIVE-327 tenant isolation): scope boards to the user's
        # accessible, org-scoped projects. A board belongs to a project which
        # belongs to an org; the previous Board.objects.all() leaked boards from
        # EVERY org — a selected org with zero boards would render other orgs'
        # boards (the reported cross-org board-render breach) while the org-scoped
        # item APIs correctly returned zero. Staff/superusers bypass
        # (get_user_projects returns all); an explicit X-Organization-ID /
        # ?organization narrows to that org.
        organization_id = _ensure_explicit_org_membership(self.request)
        visible_projects = PermissionService.get_user_projects(
            user=self.request.user, organization_id=organization_id,
        )
        queryset = Board.objects.filter(project__in=visible_projects)
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.select_related('project').prefetch_related('quick_filters')

    @action(detail=True, methods=['post'])
    def add_quick_filter(self, request, pk=None):
        """Add a quick filter to this board."""
        board = self.get_object()
        name = request.data.get('name')
        jql = request.data.get('jql', '')
        description = request.data.get('description', '')
        position = request.data.get('position', 0)

        if not name:
            return Response(
                {'error': 'name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        quick_filter = BoardQuickFilter.objects.create(
            board=board,
            name=name,
            jql=jql,
            description=description,
            position=position,
        )

        from .serializers import BoardQuickFilterSerializer
        return Response(BoardQuickFilterSerializer(quick_filter).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post', 'delete'])
    def remove_quick_filter(self, request, pk=None):
        """Remove a quick filter from this board."""
        board = self.get_object()
        filter_id = request.data.get('filter_id')

        if not filter_id:
            return Response(
                {'error': 'filter_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = BoardQuickFilter.objects.filter(
            board=board,
            id=filter_id,
        ).delete()

        if not deleted:
            return Response(
                {'error': 'Quick filter not found on this board'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({'message': 'Quick filter removed'})


class BoardQuickFilterViewSet(viewsets.ModelViewSet):
    """Standalone CRUD for board quick filters."""
    queryset = BoardQuickFilter.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import BoardQuickFilterSerializer
        return BoardQuickFilterSerializer

    def get_queryset(self):
        organization_id = _ensure_explicit_org_membership(self.request)
        visible_projects = PermissionService.get_user_projects(
            user=self.request.user,
            organization_id=organization_id,
        )
        qs = BoardQuickFilter.objects.filter(
            board__project__in=visible_projects,
        ).select_related('board', 'board__project')
        board_id = self.request.query_params.get('board_id')
        if board_id:
            qs = qs.filter(board_id=board_id)
        return qs


class DashboardViewSet(viewsets.ModelViewSet):
    """CRUD for dashboards, filterable by owner."""
    queryset = Dashboard.objects.none()
    permission_classes = [permissions.IsAuthenticated, DashboardOwnerPermission]

    def get_serializer_class(self):
        from .serializers import DashboardSerializer
        return DashboardSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = _accessible_dashboards_for_user(user)
        return queryset.prefetch_related('gadgets')

    def perform_create(self, serializer):
        serializer.save(
            owner_id=self.request.user.id,
            owner_email=getattr(self.request.user, 'email', ''),
        )


class DashboardGadgetViewSet(viewsets.ModelViewSet):
    """CRUD for dashboard gadgets, filterable by dashboard."""
    queryset = DashboardGadget.objects.none()
    permission_classes = [permissions.IsAuthenticated, DashboardOwnerPermission]

    def get_serializer_class(self):
        from .serializers import DashboardGadgetSerializer
        return DashboardGadgetSerializer

    def get_queryset(self):
        queryset = DashboardGadget.objects.filter(
            dashboard__in=_accessible_dashboards_for_user(self.request.user)
        )
        dashboard_id = self.request.query_params.get('dashboard')
        if dashboard_id:
            queryset = queryset.filter(dashboard_id=dashboard_id)
        return queryset.select_related('dashboard')


class AutomationRuleViewSet(viewsets.ModelViewSet):
    """CRUD for automation rules, filterable by project."""
    queryset = AutomationRule.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import AutomationRuleSerializer
        return AutomationRuleSerializer

    def get_queryset(self):
        queryset = AutomationRule.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        )
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.select_related('project')

    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Enable or disable an automation rule."""
        rule = self.get_object()
        rule.is_enabled = not rule.is_enabled
        rule.save(update_fields=['is_enabled', 'updated_at'])
        from .serializers import AutomationRuleSerializer
        return Response(AutomationRuleSerializer(rule).data)

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """List execution logs for this automation rule."""
        rule = self.get_object()
        logs = AutomationRuleLog.objects.filter(rule=rule).order_by('-created_at')

        # Simple pagination via limit/offset
        limit = int(request.query_params.get('limit', 50))
        offset = int(request.query_params.get('offset', 0))
        logs = logs[offset:offset + limit]

        from .serializers import AutomationRuleLogSerializer
        return Response(AutomationRuleLogSerializer(logs, many=True).data)


class AutomationRuleLogViewSet(viewsets.mixins.ListModelMixin,
                               viewsets.mixins.RetrieveModelMixin,
                               viewsets.GenericViewSet):
    """Read-only access to automation rule execution logs."""
    queryset = AutomationRuleLog.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import AutomationRuleLogSerializer
        return AutomationRuleLogSerializer

    def get_queryset(self):
        queryset = AutomationRuleLog.objects.filter(
            rule__project__in=_accessible_projects_for_user(self.request.user)
        )
        rule_id = self.request.query_params.get('rule')
        if rule_id:
            queryset = queryset.filter(rule_id=rule_id)
        return queryset.select_related('rule').order_by('-created_at')


class ProjectConfigurationViewSet(viewsets.ModelViewSet):
    """
    CRUD for project configuration.
    Each project has a single configuration record.
    """
    queryset = ProjectConfiguration.objects.none()
    permission_classes = [permissions.IsAuthenticated, RelatedProjectPermission]

    def get_serializer_class(self):
        from .serializers import ProjectConfigurationSerializer
        return ProjectConfigurationSerializer

    def get_queryset(self):
        return ProjectConfiguration.objects.filter(
            project__in=_accessible_projects_for_user(self.request.user)
        ).select_related(
            'project', 'issue_type_scheme',
            'issue_type_screen_scheme',
            'security_scheme', 'permission_scheme', 'notification_scheme',
        )


class ItemBulkOperationView(APIView):
    """
    Bulk operations on items.
    POST /api/items/bulk/

    Supports: transition, assign, edit, move_sprint, delete
    """
    permission_classes = [permissions.IsAuthenticated]

    _EDIT_OPERATIONS = {'transition', 'assign', 'edit', 'move_sprint'}

    class _MethodOverrideRequest:
        """Read-through request proxy for an object's effective HTTP verb.

        The bulk endpoint itself is always POST, but item deletion is governed
        by the DELETE branch of ``ItemProjectPermission``.  Keeping the proxy
        request otherwise transparent lets the bulk path reuse the canonical
        object permission instead of growing a second, inevitably drifting
        implementation of participant/project/org rules.
        """

        def __init__(self, request, method):
            self._request = request
            self.method = method

        def __getattr__(self, name):
            return getattr(self._request, name)

    def _filter_mutation_authorized_items(self, request, items, operation):
        """Return a plain Item queryset the caller may mutate for ``operation``.

        ``PermissionService.get_user_item_queryset`` is a *visibility* union:
        browse-only project members and baseline members of open projects can
        legitimately see items.  Visibility alone is therefore not mutation
        authority.  Apply the same per-object permission used by normal item
        detail endpoints, mapping bulk delete to DELETE and every other
        supported bulk mutation to the detail-action POST/edit semantics.

        Unauthorized targets are silently omitted, preserving the endpoint's
        non-enumerating partial-batch contract.
        """
        effective_method = 'DELETE' if operation == 'delete' else 'POST'
        permission_request = self._MethodOverrideRequest(request, effective_method)
        permission = ItemProjectPermission()
        authorized_ids = [
            item.id
            for item in items.select_related('project')
            if permission.has_object_permission(permission_request, self, item)
        ]
        return Item.objects.filter(id__in=authorized_ids).exclude(project__is_archived=True)

    def post(self, request):
        operation = request.data.get('operation')
        item_ids = request.data.get('item_ids', [])
        data = request.data.get('data', {})

        if not item_ids:
            return Response({'error': 'item_ids required'}, status=status.HTTP_400_BAD_REQUEST)

        supported_operations = self._EDIT_OPERATIONS | {'delete'}
        if not isinstance(operation, str) or operation not in supported_operations:
            return Response({'error': f'Unknown operation: {operation}'}, status=status.HTTP_400_BAD_REQUEST)

        # PLAT-933: accept task KEYS (e.g. 'PLAT-921') as well as numeric PKs — the
        # same tolerance every other item endpoint has via _resolve_item. A raw
        # filter(id__in=item_ids) 500s (Postgres: invalid input syntax for type
        # integer; Django: "Field 'id' expected a number") the moment any id is a
        # non-numeric key, which is exactly what automation/MCP callers pass. Resolve
        # tolerantly and NEVER 500 on bad input: split numeric ids from keys, batch
        # both, and silently drop unresolvable ids (like the alert bulk endpoints,
        # which succeed and affect 0).
        raw_ids = item_ids if isinstance(item_ids, (list, tuple)) else [item_ids]
        numeric_pks, item_keys = set(), set()
        for raw in raw_ids:
            s_id = str(raw).strip()
            if s_id.startswith('#'):
                s_id = s_id[1:].strip()
            if s_id.isdigit():
                numeric_pks.add(int(s_id))
            elif s_id:
                item_keys.add(s_id)
        id_q = Q()
        if numeric_pks:
            id_q |= Q(id__in=numeric_pks)
        if item_keys:
            id_q |= Q(item_key__in=item_keys)
        # SECURITY (PLAT-4195): scope bulk targets to the caller's PERMITTED items.
        # This endpoint previously resolved `Item.objects.filter(id_q)` with NO
        # tenant/permission filter, so any authenticated user could transition,
        # assign, edit, move, or DELETE any other org's items by id/key
        # (cross-tenant IDOR, live-confirmed). Resolve the requested ids through
        # the same permission queryset ItemViewSet uses (superuser / org-admin /
        # project-member / owner/assignee — additive, org-scoped), then rebuild a
        # plain queryset from the permitted ids so the mutation ops below
        # (.update()/.delete()) are unaffected by any distinct()/annotations on
        # the permission queryset. Unresolvable or unauthorized ids are silently
        # dropped (endpoint contract: succeed, affect only what you may touch).
        if numeric_pks or item_keys:
            permitted_ids = set(
                PermissionService.get_user_item_queryset(request.user)
                .filter(id_q)
                .values_list('id', flat=True)
            )
            visible_items = Item.objects.filter(id__in=permitted_ids).exclude(project__is_archived=True)
            items = self._filter_mutation_authorized_items(
                request, visible_items, operation,
            )
        else:
            items = Item.objects.none()

        if operation == 'transition':
            to_status = data.get('to_status')
            count = 0
            for item in items:
                item.status = to_status
                item.save(update_fields=['status', 'updated_at'])
                count += 1
            return Response({'updated': count})
        elif operation == 'assign':
            assignee_id = data.get('assignee_id')
            assignee_email = data.get('assignee_email', '')
            resolved_email = enforce_operator_assignment_gate(
                getattr(request.user, 'email', ''),
                assignee_id,
                assignee_email,
                context='bulk assignment',
            )
            if resolved_email:
                assignee_email = resolved_email
            uid, email = Item._resolve_user_ref({'id': assignee_id, 'email': assignee_email})
            if uid is None and not email:
                return Response({'error': 'Either assignee_id or assignee_email is required'}, status=status.HTTP_400_BAD_REQUEST)
            assignee_id, assignee_email = uid, email
            assignment_items = list(items.select_related('project'))

            # Preflight the complete batch before the first write.  Business
            # teams require exact item-org roster membership, and armed routing
            # bindings reject direct writers through the same guard.  One denied
            # target rejects the whole batch; a cross-org mixed batch must never
            # partially assign before discovering the incompatible item.
            from .services.workflow_service import WorkflowService
            for item in assignment_items:
                mismatch, roster_org, task_org = WorkflowService.assignment_org_mismatch(
                    item,
                    assignee_email,
                    item.assignment_group,
                    user_id=assignee_id,
                )
                if mismatch:
                    return Response({
                        'error': (
                            'Bulk assignment blocked by the item organization/team boundary: '
                            + WorkflowService.assignment_denial_reason(
                                assignee_email, roster_org, task_org)
                        ),
                    }, status=status.HTTP_400_BAD_REQUEST)
                if item.assignment_group and not WorkflowService._email_in_current_team_active_tier(
                    item.assignment_group,
                    assignee_email,
                    item=item,
                ):
                    return Response({
                        'error': (
                            'Bulk assignment blocked: the target is not an active '
                            'authorized member for every selected task.'
                        ),
                    }, status=status.HTTP_400_BAD_REQUEST)

            count = 0
            try:
                from django.core.exceptions import ValidationError as DjangoValidationError
                with transaction.atomic():
                    for item in assignment_items:
                        assigned = item.assign(
                            {'id': assignee_id, 'email': assignee_email},
                            save=False,
                        )
                        if not assigned:
                            raise DjangoValidationError(
                                'Resolved bulk assignee could not be applied'
                            )
                        item.save(update_fields=[
                            'assignee_id', 'assignee_email', 'updated_at',
                        ])
                        count += 1
            except DjangoValidationError as exc:
                return Response(
                    {'error': f'Bulk assignment rejected: {exc}'},
                    status=status.HTTP_409_CONFLICT,
                )
            return Response({'updated': count})
        elif operation == 'edit':
            update_fields = {}
            allowed = ['priority', 'severity', 'labels', 'tags', 'due_date']
            for field in allowed:
                if field in data:
                    update_fields[field] = data[field]
            touched_items = list(items) if ({'labels', 'tags'} & set(update_fields)) else []
            if update_fields:
                items.update(**update_fields)
            if touched_items:
                try:
                    from .services.focused_qa import ensure_focused_qa_for_item
                    for touched in touched_items:
                        for field, value in update_fields.items():
                            setattr(touched, field, value)
                        ensure_focused_qa_for_item(touched, trigger='label_update')
                except Exception:
                    logger.warning("PLS-184 focused QA generation failed after bulk edit", exc_info=True)
            return Response({'updated': items.count()})
        elif operation == 'move_sprint':
            sprint_id = data.get('sprint_id')
            item_count = items.count()
            if not item_count:
                # Do not let an otherwise unauthorized batch become a sprint-ID
                # existence oracle.  No authorized item means no target lookup.
                return Response({'updated': 0})

            if sprint_id in (None, ''):
                # Clearing a sprint is project-neutral and remains valid for a
                # batch spanning multiple projects.
                items.update(sprint=None)
                return Response({'updated': item_count})

            # A Sprint is project-owned.  A client-selected foreign (or merely
            # different-project) sprint must never be attachable to an item.
            # Require every authorized item in the batch to share exactly the
            # target sprint's project; invalid/nonexistent/foreign/mixed inputs
            # intentionally receive the same non-enumerating response.
            project_ids = set(items.values_list('project_id', flat=True))
            sprint = None
            if len(project_ids) == 1 and None not in project_ids:
                try:
                    sprint = Sprint.objects.filter(
                        pk=sprint_id,
                        project_id=next(iter(project_ids)),
                    ).first()
                except (TypeError, ValueError):
                    sprint = None
            if sprint is None:
                return Response(
                    {'error': 'Invalid sprint for selected items'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            items.update(sprint=sprint)
            return Response({'updated': item_count})
        elif operation == 'delete':
            count = items.count()
            items.delete()
            return Response({'deleted': count})
        else:
            return Response({'error': f'Unknown operation: {operation}'}, status=status.HTTP_400_BAD_REQUEST)


class TeamListView(APIView):
    """List assignment teams for one authorized item/project organization.

    The owning organization is always derived from Pulse's persisted project;
    caller-selected organization parameters are rejected. Admin is optional
    display enrichment; it can never create a product Team.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if request.query_params.get('organization') or request.query_params.get('organization_slug'):
            return Response(
                {'error': 'organization scope is derived from item/project context'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item_key = (request.query_params.get('item_key') or '').strip()
        item_id = (request.query_params.get('item_id') or '').strip()
        project_id = (
            request.query_params.get('project_id')
            or request.query_params.get('project')
            or ''
        )
        if sum(bool(v) for v in (item_key, item_id, project_id)) != 1:
            return Response(
                {'error': 'exactly one of item_key, item_id, or project_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item = None
        project = None
        if item_key or item_id:
            item_qs = PermissionService.get_user_item_queryset(
                request.user, include_personal=False,
            ).select_related('project')
            try:
                if item_key:
                    item = item_qs.filter(item_key__iexact=item_key).first()
                else:
                    item = item_qs.filter(pk=int(item_id)).first()
            except (TypeError, ValueError):
                item = None
            if item is None or item.project is None:
                return Response({'error': 'context_not_found'}, status=status.HTTP_404_NOT_FOUND)
            project = item.project
        else:
            try:
                project = PermissionService.get_user_projects(
                    user=request.user,
                ).filter(pk=int(project_id), is_archived=False).first()
            except (TypeError, ValueError):
                project = None
            if project is None:
                return Response({'error': 'context_not_found'}, status=status.HTTP_404_NOT_FOUND)

        from .services.admin_service import admin_service
        organization = admin_service.get_organization(project.org_id)
        if not isinstance(organization, dict):
            return Response(
                {'error': 'organization_scope_unavailable'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            organization_id = int(organization.get('id'))
        except (TypeError, ValueError):
            organization_id = None
        organization_slug = str(organization.get('slug') or '').strip().lower()
        if organization_id != int(project.org_id) or not organization_slug:
            return Response(
                {'error': 'organization_scope_invalid'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        viewset = ItemViewSet()
        viewset.request = request
        from .services.workflow_service import WorkflowService
        # TeamRoster is the exclusive placement/catalog authority. Admin may
        # enrich a registered team for display, but an Admin-only row must never
        # materialize a routable product team.
        exact_rows = list(TeamRoster.objects.filter(
            organization_id=organization_id,
            is_active=True,
        ).order_by('team_name', 'id'))

        admin_rows = viewset._get_admin_teams(organization_id, organization_slug)
        admin_by_name = {}
        for raw in admin_rows or []:
            if not isinstance(raw, dict):
                continue
            key = WorkflowService._canonical_team_name(
                raw.get('slug') or raw.get('name') or raw.get('team_name')
            )
            if key:
                admin_by_name[key] = raw

        scoped_teams = []
        for row in exact_rows:
            canonical_name = WorkflowService._canonical_team_name(row.team_name)
            if not canonical_name:
                continue
            enrichment = admin_by_name.get(canonical_name) or {}
            admin_team_id = enrichment.get('id') or enrichment.get('team_id')
            team = {
                # Product team identity comes from the authoritative Pulse row;
                # directory ids remain optional display enrichment.
                'id': row.id,
                'team_id': row.id,
                'admin_team_id': admin_team_id,
                'name': (
                    enrichment.get('name')
                    or enrichment.get('display_name')
                    or canonical_name
                ),
                'lead_email': enrichment.get('lead_email'),
            }
            team.update({
                'organization_id': organization_id,
                'organization_slug': organization_slug,
                'organization_name': str(organization.get('name') or '').strip(),
                'shared': False,
                'is_shared_platform': False,
                'canonical_name': canonical_name,
                # ``team_key`` is the canonical routing value persisted in
                # assignment_group. ``canonical_key`` is the stable composite
                # identity used for caches/UI keys across organizations.
                'team_key': canonical_name,
                'canonical_key': f'{organization_slug}:{canonical_name}',
            })
            scoped_teams.append(team)
        return Response({
            'organization_id': organization_id,
            'organization_slug': organization_slug,
            'project_id': project.id,
            'item_key': item.item_key if item else None,
            'teams': scoped_teams,
        })


class ResolveAssigneeView(APIView):
    """GET /api/teams/resolve-assignee/?group=<team>&(item_key=KEY|organization_id=ID)

    Read-only: resolve an assignment_group to its CURRENT concrete available agent
    using the SAME canonical resolver routing uses (WorkflowService._resolve_team_assignee),
    so callers (e.g. the alert notifier routing OpsGenie-style DMs) hit no split-brain
    and never hardcode a roster. Returns {group, user_id, email, username, available}.
    Creates nothing. available=false when the team has no assignable agent right now.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        group = (request.query_params.get('group') or request.query_params.get('team') or '').strip()
        if not group:
            return Response({'error': 'group query param is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        from .services.workflow_service import WorkflowService
        item = None
        resolver_canary = None
        resolver_canary_evidence = None
        candidate_snapshot = None
        item_key = (request.query_params.get('item_key') or '').strip()
        if item_key:
            item = PermissionService.get_user_item_queryset(
                request.user,
                include_personal=False,
            ).select_related('project').filter(item_key__iexact=item_key).first()
            if item is None:
                return Response({'error': 'item_not_found'}, status=status.HTTP_404_NOT_FOUND)
            persisted_group = WorkflowService._canonical_team_name(item.assignment_group)
            if WorkflowService._canonical_team_name(group) != persisted_group:
                from .services.resolver_canary import (
                    ResolverCanaryContextError,
                    build_terminal_outage_resolver_canary,
                )
                try:
                    (
                        resolver_canary,
                        resolver_canary_evidence,
                        candidate_snapshot,
                    ) = build_terminal_outage_resolver_canary(item, group)
                except ResolverCanaryContextError as exc:
                    logger.info(
                        'PLAT-5349: terminal resolver canary rejected for %s/%s: %s',
                        item_key,
                        group,
                        exc,
                    )
                    return Response(
                        {'error': 'routing_context_conflict'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                except Exception:
                    logger.exception(
                        'PLAT-5349: terminal resolver canary failed for %s/%s',
                        item_key,
                        group,
                    )
                    return Response(
                        {'error': 'resolver_canary_unavailable'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                item = resolver_canary
            # Diagnostics are read-only: suppress the resolver's normal durable
            # HOLD comment/notification side effects for this probe.
            item._suppress_routability_holds = True
        else:
            raw_organization_id = (
                request.query_params.get('organization_id')
                or request.headers.get('X-Organization-ID')
            )
            if raw_organization_id in (None, ''):
                return Response(
                    {'error': 'item_key or organization_id is required'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not (
                getattr(request.user, 'is_staff', False)
                or getattr(request.user, 'is_superuser', False)
            ):
                return Response(
                    {'error': 'organization resolver requires a privileged service identity'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            try:
                organization_id = int(raw_organization_id)
                if organization_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return Response(
                    {'error': 'organization_id must be a positive integer'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        source = 'team_roster_compat'
        routing_source = source
        reason = ''
        binding_snapshot = None
        try:
            from .services.workflow_service import WorkflowService
            if item is not None:
                from .services.routing_authority import routing_candidate_snapshot_for_item
                if candidate_snapshot is None:
                    candidate_snapshot = routing_candidate_snapshot_for_item(group, item)
                routing_source = candidate_snapshot.get('source') or 'authority_unknown'
                source = (
                    'server_owned_outage_canary'
                    if resolver_canary_evidence is not None
                    else routing_source
                )
                if routing_source == 'routing_binding':
                    binding_snapshot = candidate_snapshot
            resolver_kwargs = {
                'item': item,
                'enforce_cap': False if item is None else True,
            }
            if item is None:
                resolver_kwargs['organization_id'] = organization_id
            else:
                resolver_kwargs['reserve_capacity'] = False
            if resolver_canary_evidence is not None:
                resolver_kwargs['assignment_group_override'] = group
            uid, email = WorkflowService._resolve_team_assignee(
                group,
                **resolver_kwargs,
            )
            if binding_snapshot is not None:
                reason = binding_snapshot.get('reason') or ''
            if resolver_canary_evidence is not None:
                from .services.resolver_canary import candidate_snapshot_fingerprint
                post_snapshot = routing_candidate_snapshot_for_item(group, item)
                if (
                    candidate_snapshot_fingerprint(post_snapshot)
                    != candidate_snapshot_fingerprint(candidate_snapshot)
                ):
                    uid, email = None, None
                    reason = 'canary_context_stale'
        except Exception as exc:
            logger.warning("ResolveAssigneeView: resolver failed for %r: %s", group, exc)
            uid, email = None, None
            reason = 'resolver_api_error'
        if item is not None and binding_snapshot is not None and uid:
            from .services.routing_authority import subject_authorized_for_item
            decision = subject_authorized_for_item(
                uid,
                item,
                team_name=group,
                expected_assignment_group=(
                    group if resolver_canary_evidence is not None else None
                ),
                expected_workflow_name=(
                    item.workflow_name
                    if resolver_canary_evidence is not None else None
                ),
            )
            reason = decision.reason
            if not decision.allowed:
                uid, email = None, None
        elif binding_snapshot is not None and not uid and not reason:
            reason = 'no_routable_candidate'
        username = None
        # Item-aware binding probes use only the local fenced projection.  The
        # legacy group-only compatibility endpoint keeps its old username lookup.
        if uid and routing_source != 'routing_binding':
            try:
                from .user_service import user_service
                info = user_service.get_user_by_id(int(uid)) or {}
                username = info.get('username')
            except Exception:
                pass
        # PLAT-1054: never hand callers a SYNTHESIZED `user_<id>` handle. When a
        # user record has no real username, the federated-identity layer falls back
        # to `user_<uid>` (authentication.py) — but that is NOT a registered Connect
        # username, so the alert-notifier's DM to it 404s and the alert silently
        # strands (observed: `user_24`). Drop it to None so callers fall back to a
        # GUARANTEED-resolvable recipient (the notifier's OPERATOR) instead of
        # DMing a dead handle. Real handles (e.g. 'shizuha', 'mika') pass through.
        _uname = str(username) if username else ''
        if _uname.startswith('user_') and _uname[len('user_'):].isdigit():
            logger.warning(
                "ResolveAssigneeView: group=%r resolved uid=%s to synthesized "
                "username=%r (no real handle) — returning null so callers fall back "
                "to a resolvable recipient (PLAT-1054)", group, uid, username
            )
            username = None
        return Response({
            'group': group,
            'item_key': item_key or None,
            'organization_id': (
                item.project.org_id if item and item.project else organization_id
            ),
            'source': source,
            'routing_source': routing_source,
            'source_version': (
                candidate_snapshot.get('source_version') if candidate_snapshot else None
            ),
            'reason': reason or ('authorized' if uid and email else 'no_routable_candidate'),
            'user_id': uid,
            'email': email,
            'username': username,
            'available': bool(uid and email),
            **({'canary': resolver_canary_evidence} if resolver_canary_evidence else {}),
        })


class RoutingAuthorityCommandCreateView(APIView):
    """Propose a payload-bound routing-authority command as the authenticated maker."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .services.routing_authority import RoutingAuthorityError, propose_command
        try:
            command = propose_command(
                actor=request.user,
                command_type=request.data.get('command_type'),
                organization_id=request.data.get('organization_id'),
                payload=request.data.get('payload') or {},
                expected_version=request.data.get('expected_version', 0),
                reason=request.data.get('reason', ''),
                idempotency_key=request.data.get('idempotency_key'),
                approval_ttl_seconds=request.data.get('approval_ttl_seconds', 900),
            )
        except (RoutingAuthorityError, TypeError, ValueError) as exc:
            code = getattr(exc, 'code', 'command_invalid')
            return Response({'error': code}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'command_id': str(command.id),
            'state': command.state,
            'payload_digest': command.payload_digest,
            'aggregate_key': command.aggregate_key,
            'approval_expires_at': command.approval_expires_at,
        }, status=status.HTTP_201_CREATED if command.state == 'proposed' else status.HTTP_200_OK)


class RoutingAuthorityCommandApproveView(APIView):
    """Apply an exact maker proposal as a distinct authenticated checker."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, command_id):
        from .services.routing_authority import (
            RoutingAuthorityError,
            approve_and_apply_command,
        )
        try:
            command = approve_and_apply_command(
                actor=request.user,
                command_id=command_id,
                payload_digest=str(request.data.get('payload_digest') or ''),
            )
        except RoutingAuthorityCommand.DoesNotExist:
            return Response({'error': 'command_not_found'}, status=status.HTTP_404_NOT_FOUND)
        except RoutingAuthorityError as exc:
            http_status = (status.HTTP_409_CONFLICT if exc.code in {
                'expected_version_conflict', 'idempotency_payload_conflict',
                'approval_payload_conflict',
            } else status.HTTP_400_BAD_REQUEST)
            return Response({'error': exc.code}, status=http_status)
        return Response({
            'command_id': str(command.id),
            'state': command.state,
            'result': command.result,
            'result_digest': command.result_digest,
        })


class CompanyStateSnapshotView(APIView):
    """Internal v1 company-state snapshot for the Executive Cycle (PLAT-1190).

    GET /api/internal/company-state/snapshot?schema_version=1&org_slug=shizuha
    """
    permission_classes = [permissions.IsAuthenticated]

    REQUIRED_SCOPE = 'pulse:company-state.read'
    LEAD_ROLES = {'owner', 'admin', 'lead', 'executive', 'ceo', 'chief-of-staff', 'chief_of_staff'}

    def get(self, request):
        schema_version = request.query_params.get('schema_version', '1')
        if str(schema_version) not in {'1', 'company_state.snapshot.v1'}:
            return Response(
                {'error': 'unsupported_schema_version', 'supported': ['1', 'company_state.snapshot.v1']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # PLAT-1197 review P1: resolve the REQUESTED org before the access
        # check so role-based authorization can be scoped to that org — a
        # lead/admin of some other org must not read this org's snapshot.
        org_slug = request.query_params.get('org_slug') or os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha')
        from .services.company_state import CompanyStateSnapshotService
        # PLAT-5899: forward the current caller's raw Bearer + canonical subject
        # + claims so the spend_proxies Books adapter can delegate the exact
        # principal to Books on every finance read (no credential substitution).
        service = CompanyStateSnapshotService(
            org_slug=org_slug,
            bearer_token=self._raw_bearer_token(request),
            subject=self._canonical_subject(request),
            claims=self._token_claims(request),
        )
        if not self._has_snapshot_access(request, org_id=service.org.id):
            return Response(
                {'detail': 'pulse:company-state.read scope, staff, or an admin/lead role '
                           'in the requested organization is required.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        snapshot = service.build_snapshot()
        etag = self._etag(snapshot)
        if request.headers.get('If-None-Match') == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED, headers={
                'ETag': etag,
                'Cache-Control': 'private, max-age=300',
            })
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE if snapshot.get('overall_state') == 'unavailable' else status.HTTP_200_OK
        return Response(snapshot, status=http_status, headers={
            'ETag': etag,
            'Cache-Control': 'private, max-age=300',
        })

    def _has_snapshot_access(self, request, org_id=None):
        """PLAT-1197 review P1: superuser/staff and the explicit
        pulse:company-state.read scope are GLOBAL grants (service tokens);
        lead/admin roles authorize ONLY for the requested, positively-resolved
        org — a lead in org 2 must not read org 1's snapshot, and role-based
        access to an unresolved org (org_id=None) is denied."""
        user = request.user
        if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
            return True
        if org_id is not None:
            memberships = getattr(user, 'organization_memberships', {}) or {}
            for member_org, role in memberships.items():
                if str(member_org).strip() != str(org_id).strip():
                    continue
                if str(role).strip().lower() in self.LEAD_ROLES:
                    return True

        token = getattr(request, 'auth', None)
        claims = {}
        if hasattr(token, 'payload'):
            claims = getattr(token, 'payload', {}) or {}
        elif isinstance(token, dict):
            claims = token
        scopes = claims.get('scope') or claims.get('scopes') or claims.get('permissions') or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return self.REQUIRED_SCOPE in set(scopes)

    @staticmethod
    def _raw_bearer_token(request) -> str | None:
        """Return the raw JWT from the Authorization header (never cached/logged)."""
        auth = request.META.get('HTTP_AUTHORIZATION', '')
        if auth.lower().startswith('bearer '):
            return auth[len('bearer '):].strip()
        return None

    @staticmethod
    def _canonical_subject(request) -> str | None:
        """Canonical subject id for LKG partitioning (user id or token user_id)."""
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'id', None) is not None:
            return str(user.id)
        token = getattr(request, 'auth', None)
        claims = getattr(token, 'payload', None) if hasattr(token, 'payload') else None
        if isinstance(claims, dict) and claims.get('user_id') is not None:
            return str(claims['user_id'])
        return None

    @staticmethod
    def _token_claims(request) -> dict:
        token = getattr(request, 'auth', None)
        if hasattr(token, 'payload'):
            return getattr(token, 'payload', {}) or {}
        if isinstance(token, dict):
            return token
        return {}

    def _etag(self, snapshot):
        parts = [
            snapshot.get('schema_version'),
            (snapshot.get('org') or {}).get('slug'),
            *[
                section.get('as_of') or ''
                for section in (snapshot.get('sections') or {}).values()
            ],
        ]
        digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode('utf-8')).hexdigest()[:24]
        return f'"{digest}"'


class OrgChartSnapshotView(APIView):
    """
    GET /api/admin/org-chart/snapshot — aggregated org state for org-chart 2.0 rendering.

    Returns a single JSON snapshot: nodes (users/agents), teams, departments,
    per-user workload counts, and per-team workload counts. Admin/staff only.

    Data sources:
    - Team hierarchy + members: shizuha-admin REST API
    - Agent availability:       Redis key pulse:agent_availability
    - Workload counts:          Pulse Item table (two aggregated DB queries)
    """
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [HiveFleetReadAuthentication, *APIView.authentication_classes]

    _TERMINAL_STATUSES = frozenset({
        'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved',
        'rejected', 'duplicate', 'wont_fix', 'merged', 'failed',
        'expired', 'deferred',
    })

    def get(self, request):
        user = request.user
        if not (getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)
                or is_hive_fleet_reader(request)):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)

        org_scope_q = str(request.query_params.get('organization_slug') or '').strip().lower()
        # Same-cluster Hive on-demand (and the Agents UI) hammer this endpoint
        # every ~10m *and* on page load. organization_slug=all fans out admin
        # team pages + full Item aggregates; concurrent cold builds routinely
        # took 4–10s+ and blew Hive's Pulse client timeout (read timeout=10),
        # aborting the entire on-demand reconciler with zero START/STOP.
        # Short TTL + single-builder stampede guard keeps payloads fresh enough
        # for lifecycle while making the hot path a cache hit.
        # Skip shared caches under the test suite — locmem would cross-pollute
        # fixtures between org-chart unit tests (same org_scope, different data).
        use_shared_cache = not getattr(settings, 'TESTING', False)
        cache_key = f'pulse:org_chart_snapshot:v3:{org_scope_q or "default"}'
        builder = False
        if use_shared_cache:
            cached_payload = cache.get(cache_key)
            if isinstance(cached_payload, dict):
                return Response(cached_payload)
            lock_key = f'{cache_key}:lock'
            builder = cache.add(lock_key, '1', timeout=45)
            if not builder:
                for _ in range(60):
                    time.sleep(0.25)
                    cached_payload = cache.get(cache_key)
                    if isinstance(cached_payload, dict):
                        return Response(cached_payload)
                # Builder stalled — fall through and compute (better slow than 504).
        t_build = time.monotonic()
        teams_raw = self._fetch_admin_teams(org_scope=org_scope_q)
        # SSOT: shizuha-admin's REST team rosters are frequently lead-only (members
        # empty), while the POPULATED, authoritative membership lives in the Pulse
        # TeamRoster — the SAME source routing uses. Merge it in so the org-chart
        # snapshot (what Hive renders) and routing agree (no display-vs-routing
        # split brain) with NO role-based guessing. Matched on team_name (ci).
        # SSOT key normalization — mirror TeamRoster canonicalization (lower,
        # _/space -> -, then alias map) so admin team names ('Customer Support')
        # resolve to the same key routing uses ('customer-support'). Without this,
        # multi-word admin teams never matched their roster (display split brain).
        def _canon_team(n):
            x = (n or '').strip().lower().replace('_', '-').replace(' ', '-')
            try:
                from .models import TeamRoster as _TR
                return _TR._CANONICAL_ALIASES.get(x, x)
            except Exception:
                return x
        # Membership authority is organization-scoped.  Keying this projection
        # by team name alone copied the Global ``accounting`` roster into every
        # other organization's same-named Admin team, making one agent appear in
        # four organizations on Hive's Agents page.
        roster_by_key = {}
        authoritative_roster_emails = set()
        roster_rows = []
        try:
            from .models import TeamRoster
            for r in TeamRoster.objects.filter(is_active=True):
                emails = set()
                for tier in (r.tiers or []):
                    for e in (tier or []):
                        if e:
                            emails.add(str(e).strip().lower())
                for e in (getattr(r, 'junior_emails', None) or []):
                    if e:
                        emails.add(str(e).strip().lower())
                roster_key = (
                    str(getattr(r, 'organization_slug', '') or 'shizuha').strip().lower(),
                    _canon_team(r.team_name),
                )
                roster_by_key[roster_key] = emails
                authoritative_roster_emails.update(emails)
                roster_rows.append(r)
        except Exception:
            roster_by_key = {}
            authoritative_roster_emails = set()
            roster_rows = []

        # SSOT (operator 2026-07-14): teams and tasks live in PULSE — the Admin
        # service must never be a GATE on which teams exist in this snapshot.
        # Admin's team catalog is ENRICHMENT only (leads, departments, display
        # casing, HR-side teams). Concretely:
        #   1. Dedupe admin rows per (org, canonical name) — the catalog carries
        #      duplicate seedings (review/sales/security ×2, PLAT-4621) which
        #      would each swallow the full roster and render twice.
        #   2. UNION in every in-scope TeamRoster team admin doesn't return —
        #      an admin outage/pagination bug/missing row must never make a
        #      routing team (and its whole workload) vanish from Hive again.
        if org_scope_q == 'all':
            _in_scope = None
        else:
            _in_scope = {org_scope_q or os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha')}
        _seen_team_keys = set()
        _deduped_teams = []
        for team in teams_raw:
            _key = (
                str(team.get('organization_slug') or '').strip().lower(),
                _canon_team(team.get('name') or str(team.get('id') or '')),
            )
            if _key in _seen_team_keys:
                continue
            _seen_team_keys.add(_key)
            _deduped_teams.append(team)
        teams_raw = _deduped_teams
        for r in roster_rows:
            _r_slug = str(getattr(r, 'organization_slug', '') or '').strip().lower()
            if _in_scope is not None and _r_slug not in _in_scope:
                continue
            _key = (_r_slug, _canon_team(r.team_name))
            if _key in _seen_team_keys:
                continue
            _seen_team_keys.add(_key)
            teams_raw.append({
                'id': f'roster:{_key[1]}',
                'name': r.team_name,
                'organization_slug': getattr(r, 'organization_slug', '') or '',
                'members': [],
            })
        avail_map = self._get_availability_map()
        # An Admin team/lead row is directory metadata, not a second placement
        # authority.  Any known fleet agent without an exact Pulse roster edge is
        # unassigned, rather than implicitly acquiring every HR/governance team
        # association that happens to mention its email.
        known_agent_emails = authoritative_roster_emails | {
            str(email or '').strip().lower() for email in avail_map.keys()
            if str(email or '').strip()
        }
        # HIVE-609: honor ?organization_slug= (specific org, or 'all' to fan out
        # across every org) so Hive team cards can show org-true workload.
        (workload_by_email, workload_by_group, workload_by_uid,
         workload_by_assignee_team, unassigned_by_group) = self._aggregate_workload(org_scope=org_scope_q)

        nodes = []
        teams_out = []
        seen_emails = set()

        for team in teams_raw:
            t_id = team.get('id') or team.get('slug') or team.get('name', '').lower().replace(' ', '-')
            t_name = team.get('name', '')
            lead_email = (team.get('lead_email') or team.get('leader_email') or '').strip().lower()
            members = list(team.get('members') or [])
            # SSOT: Pulse TeamRoster is the AUTHORITATIVE membership source. When a
            # roster exists for this exact (organization, team), it REPLACES
            # admin membership entirely so display == routing (one source, many views).
            # An empty roster is authoritative empty; it must not fall through to
            # Admin members. Admin-only teams retain human directory members, but
            # known fleet agents are excluded because Pulse is their sole placement
            # authority. Reuse Admin member dicts only for exact authoritative rows.
            _team_org = str(team.get('organization_slug') or '').strip().lower()
            if not _team_org and org_scope_q and org_scope_q != 'all':
                _team_org = org_scope_q
            _roster_key = (_team_org, _canon_team(t_name))
            _has_roster = _roster_key in roster_by_key
            _roster_emails = roster_by_key.get(_roster_key, set())
            if _has_roster:
                _admin_by_email = {(m.get('email') or m.get('user_email') or '').strip().lower(): m
                                   for m in members if isinstance(m, dict)}
                members = [(_admin_by_email.get(_e) or {'email': _e})
                           for _e in sorted(_roster_emails)]
                if lead_email not in _roster_emails:
                    lead_email = ''
            else:
                members = [
                    m for m in members
                    if not isinstance(m, dict)
                    or (m.get('email') or m.get('user_email') or '').strip().lower()
                    not in known_agent_emails
                ]
                if lead_email in known_agent_emails:
                    lead_email = ''

            # Resolve lead user_id for manager_id on non-lead members
            # PLAT-320: resolve the lead by email when shizuha-admin provides
            # lead_email, but also honor a member explicitly marked is_lead /
            # role=='lead' (some teams mark the lead in `members` without a
            # top-level lead_email) — else lead_id is null and every node loses
            # its manager_id.
            lead_uid = None
            lead_resolved_email = lead_email or None
            for m in members:
                if not isinstance(m, dict):
                    continue
                m_email = (m.get('email') or m.get('user_email') or '').strip().lower()
                if (lead_email and m_email == lead_email) or m.get('is_lead') or m.get('role') == 'lead':
                    lead_uid = m.get('user_id') or m.get('id')
                    if m_email:
                        lead_resolved_email = m_email
                    break

            member_ids = []
            for m in members:
                if not isinstance(m, dict):
                    continue
                m_email = (m.get('email') or m.get('user_email') or '').strip().lower()
                if not m_email:
                    continue
                m_uid = m.get('user_id') or m.get('id')
                # PLAT-320: a user can belong to multiple teams — always record the
                # membership in THIS team's member_ids; de-dup only the node so each
                # user appears once in `nodes`. (The old global skip dropped
                # multi-team users from every later team's member_ids/roster.)
                # PLAT-340 (Rei review): email-only members have m_uid=None; append
                # the SAME stable key the frontend nodeKey uses ('email:'+email) so
                # rosterFor's nodesById lookup resolves them instead of dropping
                # null. id-based members keep their numeric uid (unchanged).
                member_ids.append(m_uid if m_uid is not None else ('email:' + m_email))
                if m_email in seen_emails:
                    continue
                seen_emails.add(m_email)
                m_name = (
                    m.get('display_name') or m.get('full_name') or m.get('name')
                    or m.get('username')
                    # Proper-case the email-localpart fallback (roster-merged
                    # members carry only an email) so agents render as 'Banto',
                    # not 'banto'. Per-word title-case handles handles like 'yuki-jr'.
                    or '-'.join(p.capitalize() for p in m_email.split('@')[0].split('-'))
                )
                m_is_lead = bool(m.get('is_lead') or m.get('role') == 'lead' or m_email == lead_email)
                # PLAT-320: honor an explicit is_agent flag (incl. False) when the
                # roster sets it; only fall back to the @shizuha.com domain heuristic
                # when it's absent, so a human staffer on @shizuha.com isn't mislabeled
                # an AI agent.
                m_is_agent = bool(m.get('is_agent')) if 'is_agent' in m else m_email.endswith('@shizuha.com')
                avail = avail_map.get(m_email)
                avail_status = 'available' if avail is True else ('unavailable' if avail is False else 'unknown')
                nodes.append({
                    'user_id': m_uid,
                    'display_name': m_name,
                    'email': m_email,
                    'role': m.get('role') or ('lead' if m_is_lead else 'member'),
                    'team_id': t_id,
                    'manager_id': None if m_is_lead else lead_uid,
                    'is_agent': m_is_agent,
                    'model_tier': m.get('model_tier'),
                    'availability_status': avail_status,
                })

            teams_out.append({
                'id': t_id,
                'name': t_name,
                'lead_id': lead_uid,
                # PLAT-339 (aoi P2): emit the lead's email so the frontend can resolve
                # an email-only lead (lead_id=null) via the stable 'email:'+email node
                # key — robust even when the lead's node was de-duped under another team
                # (its role then reflects that team, so a teamNodes role-scan misses it).
                'lead_email': lead_resolved_email,
                'member_ids': member_ids,
                'department_id': team.get('department_id'),
                # HIVE-609: org attribution so UIs can render "ORG › TEAM".
                'organization_slug': team.get('organization_slug') or '',
                'organization_name': team.get('organization_name') or '',
            })

        # Per-user workload keyed by user_id (fallback to email for unknown users)
        email_to_uid = {n['email']: n['user_id'] for n in nodes if n['user_id']}
        workload = {}
        for email, counts in workload_by_email.items():
            key = str(email_to_uid[email]) if email in email_to_uid else email
            workload[key] = counts
        # PLAT-320: fold in id-only workloads (assignee_id present, no email) so a
        # user assigned purely by id still shows their real load (response is uid-keyed).
        for uid_key, counts in workload_by_uid.items():
            if uid_key in workload:
                for k, v in counts.items():
                    workload[uid_key][k] = workload[uid_key].get(k, 0) + v
            else:
                workload[uid_key] = counts

        # Per-team workload keyed by team id.
        # HIVE-609: prefer the ORG-QUALIFIED counts when the team carries an org
        # slug — same-named teams exist in every org (four 'Accounting's), and
        # the plain group key is the cross-org SUM, which over-reports each card.
        _org_id_by_slug = {}
        def _org_id_for(slug):
            if not slug:
                return None
            if slug not in _org_id_by_slug:
                try:
                    from .services.admin_service import admin_service
                    org = admin_service.get_organization_by_slug(slug)
                    _org_id_by_slug[slug] = org.get('id') if isinstance(org, dict) else None
                except Exception:
                    _org_id_by_slug[slug] = None
            return _org_id_by_slug[slug]

        # Single-team invariant (operator 2026-07-11): a team's counts are BY
        # CONSTRUCTION the sum of its members' workloads — the SAME per-assignee
        # counts the member cards render — plus its UNASSIGNED group queue.
        # Team card == sum(member cards) + visible queue, always. The previous
        # independent group-based aggregation drifted from member cards whenever
        # a member held cross-team/cross-org work (Accounting showed 1 blocked
        # while its member showed 2, 2026-07-11).
        nodes_by_team_id = {}
        for n in nodes:
            nodes_by_team_id.setdefault(str(n.get('team_id')), []).append(n)
        _BUCKETS = ('open', 'in_progress', 'blocked', 'in_review',
                    'scheduled', 'backlog', 'completed', 'overdue')

        # PLAT-4213 (operator 2026-07-11): sum each member's TEAM-SCOPED workload
        # (their tasks whose assignment_group == THIS team), NOT their total. A
        # member's cross-group task (e.g. a customer-support agent holding a
        # review-group item) must count under the REVIEW team, not inflate
        # customer-support's 'active' — otherwise the header disagreed with the
        # member cards (which already render team-scoped via workloadForTeamCard),
        # so 'numbers don't tally' and unrelated work showed as active.
        def _wl_for_node_team(n, team_norm):
            # Hive roster emails are @shizuha.com; Pulse may have stored the
            # identity-service alias @agents.shizuha.io. Try both spellings so
            # a Merge card keyed ryo@shizuha.com::merge is not empty while the
            # agent is loaded under the alias.
            idents = []
            raw_email = (n.get('email') or '').strip().lower()
            if raw_email:
                idents.append(raw_email)
                canon = Item._canonical_assignment_guard_email(raw_email)
                if canon and canon != raw_email:
                    idents.append(canon)
            if n.get('user_id'):
                idents.append(str(n['user_id']))
            # HIVE-2627: prefer the ORG-QUALIFIED assignee-within-team counts
            # when the team carries an org — the bare `ident::team` key is the
            # cross-org SUM in 'all' fan-out mode, which painted Digital
            # Review's header with Global review WIP. Mirrors the org-qualified
            # queue preference below (HIVE-609 pattern).
            n_oid = _org_id_for(n.get('organization_slug'))
            for ident in idents:
                if n_oid is not None:
                    wl = workload_by_assignee_team.get(f'{str(ident).strip().lower()}::org{n_oid}::{team_norm}')
                    if wl:
                        return wl
                wl = workload_by_assignee_team.get(f'{str(ident).strip().lower()}::{team_norm}')
                if wl:
                    return wl
            return {}

        team_workload = {}
        for t_out in teams_out:
            # Admin owns the human-facing label while Pulse owns routing.  Always
            # resolve the label through the same canonical alias map used for
            # roster membership and assignment_group aggregation.  Otherwise an
            # Admin team displayed as "Research" looks for `research` workload
            # even though its one authoritative roster/routes use
            # `research-analytics`, making the Hive team header falsely read zero.
            t_name_norm = _canon_team(t_out['name'])
            t_id = str(t_out['id'])
            counts = {b: 0 for b in _BUCKETS}
            for n in nodes_by_team_id.get(t_id, []):
                wl = _wl_for_node_team(n, t_name_norm)
                for b in _BUCKETS:
                    counts[b] += int(wl.get(b, 0) or 0)
            oid = _org_id_for(t_out.get('organization_slug'))
            queue = (
                (unassigned_by_group.get(f'org{oid}::{t_name_norm}') if oid is not None else None)
                or (unassigned_by_group.get(t_name_norm) if oid is None else None)
                or {}
            )
            for b in _BUCKETS:
                counts[b] += int(queue.get(b, 0) or 0)
            team_workload[t_id] = counts

        # Derive departments from teams (admin API returns department_id per team;
        # name comes from a nested 'department' dict or 'department_name' if present,
        # otherwise falls back to the id so the filter at least works). Each unique
        # department_id becomes one entry; ordering is stable (first-seen).
        depts_seen = {}
        for t_raw, t_out in zip(teams_raw, teams_out):
            d_id = t_out.get('department_id')
            if not d_id:
                continue
            key = str(d_id)
            if key not in depts_seen:
                dept_obj = t_raw.get('department')
                d_name = (
                    (isinstance(dept_obj, dict) and dept_obj.get('name'))
                    or t_raw.get('department_name')
                    or f'Department {d_id}'
                )
                depts_seen[key] = {'id': d_id, 'name': d_name}
        departments_out = list(depts_seen.values())

        # Top-level availability map keyed by email (every known agent), so the
        # fleet UI can render real provider/Pulse availability even when admin
        # team rosters (and therefore `nodes`) are empty — node-attached
        # availability_status is otherwise lost. unavailable = down/usage-limited.
        availability = {
            email: ('available' if v is True else 'unavailable' if v is False else 'unknown')
            for email, v in (avail_map or {}).items()
        }
        payload = {
            'nodes': nodes,
            'teams': teams_out,
            'departments': departments_out,
            'workload': workload,
            'team_workload': team_workload,
            # Team-scoped assignee workload lets consumers render a card inside a
            # team section without mixing in that assignee's work from other teams.
            'workload_by_assignee_team': workload_by_assignee_team,
            'workload_by_email_team': workload_by_assignee_team,
            'availability': availability,
            # Hive on-demand/lease treats assigned firing alerts as personal
            # work (wake kai instead of hibernating a seat that owns Origin-CI
            # / ops-anomaly episodes). Keyed by canonical @shizuha.com plus
            # the stored alias so either spelling matches.
            'alerts_by_email': self._aggregate_alert_workload(org_scope=org_scope_q),
        }
        if use_shared_cache:
            try:
                cache.set(cache_key, payload, timeout=45)
            except Exception:
                logger.warning('OrgChartSnapshotView: failed to cache snapshot', exc_info=True)
            finally:
                if builder:
                    cache.delete(lock_key)
        build_ms = (time.monotonic() - t_build) * 1000
        if build_ms >= 1000:
            logger.warning(
                'OrgChartSnapshotView: cold build scope=%r duration_ms=%.0f '
                '(cache_ttl=45s; Hive on-demand depends on this staying < client timeout)',
                org_scope_q or 'default',
                build_ms,
            )
        return Response(payload)

    def _fetch_admin_teams(self, org_scope=''):
        """Fetch team roster from shizuha-admin ('' = env default org; 'all' =
        every org, each row stamped with organization_slug). Returns [] on error."""
        import requests
        import jwt as _jwt

        try:
            jwt_secret = configured_service_signing_key()
            svc_token = _jwt.encode({
                'user_id': 999, 'email': 'system@shizuha.com', 'username': 'system',
                'organization_id': 1, 'is_staff': True, 'is_superuser': True,
                'exp': timezone.now() + timedelta(hours=1),
                'iat': timezone.now(),
                'token_type': 'access', 'jti': 'org-chart-snapshot',
            }, jwt_secret, algorithm='HS256')
        except Exception as e:
            logger.warning(f'OrgChartSnapshotView: JWT encode failed: {e}')
            return []

        admin_url = os.environ.get('SHIZUHA_ADMIN_API_URL', 'http://shizuha-admin:8003/api')
        # HIVE-609: fan teams out across every org when requested ('all'), and
        # stamp organization_slug/name on EVERY team row so consumers (Hive
        # Agents/Teams pages) can render an unambiguous "ORG › TEAM" label.
        if org_scope == 'all':
            # /organizations/ on admin is MEMBERSHIP-scoped (the HS256 service
            # identity only sees its own personal org) — enumerate orgs from
            # Pulse's OWN Project rows instead (org_id → admin lookup), which is
            # exactly the set of orgs that can carry workload here.
            org_slugs = []
            try:
                from .models import Project
                from .services.admin_service import admin_service
                org_ids = sorted({
                    oid for oid in Project.objects.exclude(org_id__isnull=True)
                    .values_list('org_id', flat=True).distinct()
                    if oid is not None
                })
                for oid in org_ids:
                    org = admin_service.get_organization(oid)
                    slug = (org or {}).get('slug') if isinstance(org, dict) else None
                    if slug:
                        org_slugs.append(slug)
            except Exception as e:
                logger.warning(f'OrgChartSnapshotView: org enumeration failed: {e}')
            if not org_slugs:
                org_slugs = [os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha')]
        else:
            org_slugs = [org_scope or os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha')]

        # PLAT-320: follow DRF pagination — a single-page read silently drops every
        # team/member after the first page while still reporting success. Walk `next`
        # until exhausted (bounded guard against a pathological/looping pager).
        from urllib.parse import parse_qs, urlsplit

        # Per-process short cache for admin team pages (shared with concurrent
        # snapshot builders on this worker). Admin is in-cluster but still HTTP
        # + pagination; re-fetching every org on every cold build is the main
        # multi-second cost under Hive on-demand concurrency. Disabled under
        # TESTING so unit fixtures do not see another test's admin payload.
        use_teams_cache = not getattr(settings, 'TESTING', False)
        teams_cache = getattr(OrgChartSnapshotView, '_admin_org_teams_cache', None)
        if use_teams_cache and teams_cache is None:
            teams_cache = {}
            OrgChartSnapshotView._admin_org_teams_cache = teams_cache
        now_mono = time.monotonic()
        teams = []
        for slug in org_slugs:
            if use_teams_cache and teams_cache is not None:
                cached_teams = teams_cache.get(slug)
                if cached_teams is not None and (now_mono - cached_teams[0]) < 300:
                    teams.extend(cached_teams[1])
                    continue
            base_url = f'{admin_url}/organizations/{slug}/teams/'
            page = None  # first request: no page param
            pages = 0
            slug_teams = []
            try:
                while pages < 50:
                    resp = requests.get(
                        base_url,
                        params={'page': page} if page else None,
                        headers={'Authorization': f'Bearer {svc_token}'},
                        timeout=5,
                    )
                    if resp.status_code != 200:
                        logger.warning(f'OrgChartSnapshotView: admin teams ({slug}) returned {resp.status_code}')
                        break
                    # Admin's paginator emits `next` with its EXTERNAL prefix
                    # (http://…/admin/api/…); on the in-cluster port that path
                    # 200s with the SPA's index.html, json() raises, and every
                    # team after page 1 silently vanished — qa/review/security
                    # rendered 0-0-0 in Hive team headers (operator, 2026-07-14).
                    # Follow only the page NUMBER against the configured base;
                    # fail loud on a non-JSON body instead of eating the page.
                    ctype = (resp.headers.get('content-type') or '').lower()
                    if 'json' not in ctype:
                        logger.warning(
                            f'OrgChartSnapshotView: admin teams ({slug}) page {page or 1} '
                            f'returned non-JSON content-type {ctype!r} — pagination aborted'
                        )
                        break
                    data = resp.json()
                    batch = data if isinstance(data, list) else (data.get('results') or []) if isinstance(data, dict) else []
                    for team in batch:
                        if isinstance(team, dict) and not team.get('organization_slug'):
                            # admin rows carry an explicit null here — overwrite it
                            team['organization_slug'] = slug
                    slug_teams.extend(batch)
                    page = None
                    if isinstance(data, dict):
                        nxt = data.get('next')
                        if isinstance(nxt, str) and nxt:
                            try:
                                page = (parse_qs(urlsplit(nxt).query).get('page') or [None])[0]
                            except Exception:
                                page = None
                    if not page:
                        break
                    pages += 1
                if use_teams_cache and teams_cache is not None:
                    teams_cache[slug] = (time.monotonic(), list(slug_teams))
                teams.extend(slug_teams)
            except Exception as e:
                logger.warning(f'OrgChartSnapshotView: admin teams fetch failed ({slug}): {e}')
        return teams

    def _get_availability_map(self):
        # Compatibility bridge: the implementation lives below with the roster
        # API after the TeamRoster SSOT patch. Keep the snapshot endpoint bound
        # to the method name it calls so /admin/org-chart/snapshot never 500s.
        return TeamRosterMemberView._get_availability_map(self)

    def _aggregate_workload(self, org_scope=''):
        return TeamRosterMemberView._aggregate_workload(self, org_scope=org_scope)

    def _aggregate_alert_workload(self, org_scope=''):
        return TeamRosterMemberView._aggregate_alert_workload(self, org_scope=org_scope)


class TeamRosterMemberView(APIView):
    """Read one project-org roster scope; staff may edit the global roster.

    TeamRoster is the single membership source used by Pulse routing, Pulse
    org-chart snapshots, and Hive agent team placement. Admin owns team catalog
    and employee profiles, not roster membership for this surface. Unscoped
    reads and every write remain staff-only.
    """
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _canon_team(name):
        x = (name or '').strip().lower().replace('_', '-').replace(' ', '-')
        return TeamRoster._CANONICAL_ALIASES.get(x, x)

    @staticmethod
    def _remove_email(tiers, email):
        changed = False
        out = []
        for tier in tiers or []:
            kept = []
            for member in tier or []:
                if str(member).strip().lower() == email:
                    changed = True
                    continue
                kept.append(member)
            out.append(kept)
        return out, changed

    def _require_staff(self, request):
        user = request.user
        if not (getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        return None

    def get(self, request):
        project_id = request.query_params.get('project_id')
        if project_id:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                return Response(
                    {'detail': 'project_id must be an integer.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            from .services.permissions import PermissionService
            project = PermissionService.get_user_projects(
                user=request.user,
            ).filter(pk=project_id).first()
            if project is None:
                raise NotFound('Project not found.')
            roster_queryset = TeamRoster.objects.filter(
                is_active=True,
                organization_id=project.org_id,
            )
        else:
            # The unscoped roster is an administrative fleet surface. Product
            # pages must provide an authorized project so the server—not a
            # caller-supplied org slug—derives the only visible organization.
            denied = self._require_staff(request)
            if denied:
                return denied
            roster_queryset = TeamRoster.objects.filter(is_active=True)

        email = (request.query_params.get('email') or '').strip().lower()
        # PLAT-4212: resolve org slug -> display name once so Hive renders
        # "Hritik's Workspace › Personal Accounting", not "personal-3 › …".
        _org_name_cache = {}
        def _org_name(slug):
            slug = slug or 'shizuha'
            if slug not in _org_name_cache:
                try:
                    from .services.admin_service import admin_service
                    org = admin_service.get_organization_by_slug(slug)
                    _org_name_cache[slug] = (org.get('name') if isinstance(org, dict) else '') or slug
                except Exception:
                    _org_name_cache[slug] = slug
            return _org_name_cache[slug]
        rosters = [
            {
                'team_name': self._canon_team(roster.team_name),
                'tiers': roster.tiers or [],
                'junior_emails': roster.junior_emails or [],
                # PLAT-4213/4212: org-distinct teams — consumers (Hive team
                # grouping, on-demand reconciler) render/route per org.
                'organization_id': roster.organization_id,
                'organization_slug': roster.organization_slug or 'shizuha',
                'organization_name': _org_name(roster.organization_slug),
                # PLS-745: writes are CAS commands keyed on this version; the
                # GET is the only read Hive has, so it must carry the version
                # or no client can ever construct a valid write.
                'version': int(roster.version or 0),
            }
            for roster in roster_queryset.order_by('organization_slug', 'team_name')
        ]
        if not email:
            return Response({'rosters': rosters, 'memberships': []})
        memberships = []
        for roster in roster_queryset:
            team = self._canon_team(roster.team_name)
            for tier_index, tier in enumerate(roster.tiers or []):
                if email in {str(member).strip().lower() for member in (tier or [])}:
                    memberships.append({
                        'team_name': team,
                        'tier_index': tier_index,
                        'organization_id': roster.organization_id,
                        'organization_slug': roster.organization_slug or 'shizuha',
                    })
        return Response({'email': email, 'memberships': memberships, 'rosters': rosters})

    def post(self, request):
        denied = self._require_staff(request)
        if denied:
            return denied
        action = (request.data.get('action') or '').strip().lower()
        team_name = self._canon_team(request.data.get('team_name') or '')
        organization_slug = str(request.data.get('organization_slug') or 'shizuha').strip().lower()
        try:
            organization_id = _resolve_team_roster_organization_id(
                organization_slug,
                request.data.get('organization_id'),
            )
        except DjangoValidationError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        from .services.team_roster_commands import (
            TeamRosterCommandError,
            execute_team_roster_command,
        )
        try:
            result = execute_team_roster_command(
                command_id=request.data.get('command_id') or request.headers.get('Idempotency-Key'),
                action=action,
                organization_id=organization_id,
                organization_slug=organization_slug,
                team_name=team_name,
                email=request.data.get('email') or '',
                expected_version=request.data.get('expected_version'),
                actor_email=(
                    getattr(request.user, 'email', '')
                    or str(getattr(request.user, 'id', ''))
                ),
            )
        except TeamRosterCommandError as exc:
            conflict_codes = {
                'expected_version_conflict', 'idempotency_payload_conflict',
            }
            return Response(
                {'error': exc.code, 'detail': exc.detail},
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.code in conflict_codes
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        return Response(result, status=status.HTTP_200_OK)

    def _get_availability_map(self):
        """Read pulse:agent_availability from Redis. Returns {} on error."""
        import json
        try:
            from .services.workflow_service import WorkflowService
            raw = WorkflowService._redis_client().get('pulse:agent_availability')
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f'OrgChartSnapshotView: availability read failed: {e}')
        return {}

    def _aggregate_alert_workload(self, org_scope=''):
        """Firing/acknowledged Pulse alerts per assignee email.

        Hive `alerts_by_email` is the KEEP/WAKE signal for assigned incidents.
        Count both the stored email and the PLAT-533 canonical @shizuha.com
        form so a seat keyed `kai@shizuha.com` sees `kai@agents.shizuha.io`
        assignments.
        """
        from django.db.models import Count

        # Alerts are org-global for Hive wake: ops-anomaly rows often have
        # project_id=None, and a firing incident must wake the assignee in
        # every org snapshot rather than vanish from `alerts_by_email`.
        qs = Item.objects.filter(
            mode='alert',
            status__in=('firing', 'acknowledged'),
        ).exclude(assignee_email__isnull=True).exclude(assignee_email='')
        counts = {}
        for row in qs.values('assignee_email').annotate(c=Count('id')):
            raw = str(row.get('assignee_email') or '').strip().lower()
            if not raw:
                continue
            n = int(row['c'] or 0)
            counts[raw] = counts.get(raw, 0) + n
            canon = Item._canonical_assignment_guard_email(raw)
            if canon and canon != raw:
                counts[canon] = counts.get(canon, 0) + n
        return counts

    def _aggregate_workload(self, org_scope=''):
        """
        Aggregated DB queries → workload_by_email, workload_by_group,
        workload_by_uid, workload_by_assignee_team.
        Each maps to {open, in_progress, blocked, in_review, scheduled, backlog} counts.
        Non-terminal statuses are bucketed; unknown statuses fall into 'open'.
        org_scope: '' = env default org; 'all' = fan out across every org; else slug.
        """
        # PLAT-320: bucket configured (non-hardcoded) statuses by their seeded
        # coarse category — e.g. in_qa / awaiting_triage / design carry category
        # 'in_progress' (migration 0023) and must count as in_progress, not fall
        # through to 'open'. Fine-grained slug checks run first (so blocked /
        # in_review keep their own bucket); category is the fallback.
        status_cat = dict(Status.objects.values_list('slug', 'category'))
        # PLAT-320: a status's seeded coarse CATEGORY (migration 0023) maps to a
        # workload bucket, so configured statuses NOT in the hard-coded slug lists
        # (in_qa, awaiting_triage, design, and any custom blocked/review states) are
        # counted correctly instead of all falling through to 'open'. Fine-grained
        # slug checks run first (exact well-known slugs); category is the fallback.
        _CAT_BUCKET = {
            'todo': 'open', 'open': 'open', 'backlog': 'backlog',
            'in_progress': 'in_progress',
            'blocked': 'blocked',
            'in_review': 'in_review', 'review': 'in_review',
        }

        def _bucket(s):
            # 'backlog' is intentionally-parked work (decompose-on-pull when a team
            # has capacity) — NOT actionable. It gets its own bucket so the UI does
            # not read a correctly-drained agent's parked items as "open" work
            # (aoi showed 37 "open" that were all backlog, 2026-06-24).
            if s == 'backlog':
                return 'backlog'
            if s in ('open', 'todo', 'pending', 'unstarted'):
                return 'open'
            if s in ('blocked', 'waiting', 'on_hold', 'stalled', 'waiting_external') or (
                isinstance(s, str) and s.startswith('waiting')
            ):
                # waiting_external is todo-category (PLS-733: non-terminal hold)
                # but it is awareness-only for owner queues. Counting it as
                # open made Hive grant DeepSeek seats to idle agents (hiro/rei
                # 2026-08-21) while SCLI heartbeat correctly reported
                # queue_empty.
                return 'blocked'
            if s in (
                'in_review', 'review', 'awaiting_merge', 'awaiting_review',
                'awaiting_deploy', 'awaiting_verification', 'in_review_architect',
                'verification', 'deploying',
            ):
                return 'in_review'
            if s in ('in_progress', 'started', 'active', 'doing'):
                return 'in_progress'
            if s == 'scheduled' or status_cat.get(s) == 'scheduled':
                return 'scheduled'  # HIVE-609: parked/future work gets its own column
            return _CAT_BUCKET.get(status_cat.get(s), 'open')

        def _empty():
            # 'scheduled' = open-status tasks whose due_date is in the FUTURE
            # (recurring/future-dated work that is not movable yet). Splitting it
            # out of 'open' stops the UI from implying an idle agent is ignoring
            # actionable work when its only open tasks aren't due yet. (PLAT/HIVE.)
            return {'open': 0, 'in_progress': 0, 'blocked': 0, 'in_review': 0,
                    'scheduled': 0, 'backlog': 0, 'completed': 0, 'overdue': 0}

        # PLAT/HIVE: canonicalize assignment_group once and reuse it for both
        # team totals and assignee-within-team totals. The latter is what lets
        # Hive show a card under "Admin Ops" without counting Product/Governance
        # work that happens to be assigned to the same agent.
        def _canon_group(g):
            return (g or '').strip().lower().replace(' ', '-').replace('_', '-')

        def _add_count(target, key, bucket, count):
            if not key:
                return
            if key not in target:
                target[key] = _empty()
            target[key][bucket] += count

        def _assignee_team_key(assignee_key, group):
            g = _canon_group(group)
            if not assignee_key or not g:
                return ''
            return f'{str(assignee_key).strip().lower()}::{g}'

        def _canon_email(e):
            # PLAT-533: @shizuha.com and @agents.shizuha.io are one agent.
            # Hive Agents cards key the roster spelling (@shizuha.com); WIP
            # and get_my_tasks already fold, but this snapshot used the raw
            # stored email, so a loaded merger (ryo@agents.shizuha.io) rendered
            # as an empty card.
            return Item._canonical_assignment_guard_email(e)

        def _rebucket(b, is_future):
            # Only OPEN-bucket work is downgraded to 'scheduled' when future-dated;
            # blocked / in_progress / in_review keep their own bucket regardless.
            return 'scheduled' if (b == 'open' and is_future) else b

        from django.db.models import Count

        # PLAT-320: scope to the snapshot org. The roster is fetched for a single
        # SHIZUHA_ORG_SLUG, but these counts otherwise span EVERY org's tasks in a
        # multi-org deployment (an assignee/group with tasks in another org would
        # be over-counted). Fail-open (unscoped) only if the org can't be resolved,
        # so the snapshot still renders when admin is unreachable.
        # PLAT-320: exclude terminal work from active workload. Derive done-category
        # slugs from Status (a custom 'done'-category status not in the hard-coded
        # set would otherwise stay in `base` and inflate counts as 'open').
        # done- AND scheduled-category statuses are inactive for active workload:
        # 'done' is terminal; 'scheduled' parks recurring/future tasks (a gap state).
        # Deriving from Status.category catches custom slugs not in the hard-coded set.
        # HIVE-609: 'scheduled'-category statuses are NOT terminal — they are
        # future/parked work that the Hive team cards render in their own
        # 'scheduled' column. Excluding them here made that column structurally
        # zero (operator 2026-07-10: Accounting showed 0-0-0 while BKSG items sat
        # in status=scheduled). Only done-category joins the terminal set; the
        # _bucket fallback below maps scheduled-category slugs to 'scheduled'.
        inactive_slugs = {slug for slug, cat in status_cat.items() if cat == 'done'}
        terminal_statuses = self._TERMINAL_STATUSES | inactive_slugs
        base = (
            Item.objects.filter(mode='task', is_archived=False)
            .exclude(project__is_archived=True)
            .exclude(status__in=terminal_statuses)
        )
        org_id = None
        try:
            from .services.admin_service import admin_service
            # HIVE-609: callers may scope to any org or fan out across ALL orgs
            # ('all'); default stays the primary org for back-compat.
            org_slug = org_scope or os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha')
            if org_slug == 'all':
                org_id = 'all'
            else:
                org = admin_service.get_organization_by_slug(org_slug)
                org_id = org.get('id') if isinstance(org, dict) else None
            if org_id == 'all':
                pass  # fan-out: no org filter
            elif org_id is not None:
                base = base.filter(project__org_id=org_id)
            else:
                logger.warning('OrgChartSnapshotView: org_id unresolved for slug '
                               f'{org_slug!r}; workload counts are NOT org-scoped')
        except Exception as e:
            logger.warning('OrgChartSnapshotView: org scope resolution failed '
                           f'({e}); workload counts are NOT org-scoped')

        from django.db.models import Q, Case, When, Value, BooleanField

        _now = timezone.now()
        # is_future: due_date strictly in the future (a future-dated/recurring task
        # not yet movable). NULL due_date is treated as actionable (not future).
        _future_case = Case(
            When(due_date__gt=_now, then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        )

        user_rows = (
            base
            .exclude(assignee_email__isnull=True)
            .exclude(assignee_email='')
            .annotate(is_future=_future_case)
            .values('assignee_email', 'assignment_group', 'status', 'is_future',
                    'project__org_id')
            .annotate(cnt=Count('id'))
        )
        workload_by_email = {}
        workload_by_assignee_team = {}
        for row in user_rows:
            email = _canon_email(row['assignee_email'])
            if not email:
                continue
            b = _rebucket(_bucket(row['status']), row['is_future'])
            _add_count(workload_by_email, email, b, row['cnt'])
            _add_count(
                workload_by_assignee_team,
                _assignee_team_key(email, row.get('assignment_group')),
                b,
                row['cnt'],
            )
            # HIVE-2627: org-qualified assignee-within-team key alongside the
            # bare one. In 'all' fan-out mode the bare `email::review` key is
            # the cross-org SUM, so every org's Review team card summed every
            # other org's review WIP (Digital Review painted with Global's
            # 14 active / 15 blocked). Same pattern as the HIVE-609
            # workload_by_group qualification below.
            row_oid = row.get('project__org_id')
            if row_oid is not None:
                _add_count(
                    workload_by_assignee_team,
                    f'{email}::org{row_oid}::{_canon_group(row.get("assignment_group"))}',
                    b,
                    row['cnt'],
                )

        # Count every concrete assignment by id as well as by email. The
        # assignment invariant now requires the canonical pair, so limiting
        # this map to legacy id-only rows would make workload_by_uid empty for
        # every valid assignment.
        id_rows = (
            base
            .exclude(assignee_id__isnull=True)
            .annotate(is_future=_future_case)
            .values('assignee_id', 'assignment_group', 'status', 'is_future',
                    'project__org_id')
            .annotate(cnt=Count('id'))
        )
        workload_by_uid = {}
        for row in id_rows:
            uid = row['assignee_id']
            if uid is None:
                continue
            b = _rebucket(_bucket(row['status']), row['is_future'])
            key = str(uid)
            _add_count(workload_by_uid, key, b, row['cnt'])
            _add_count(
                workload_by_assignee_team,
                _assignee_team_key(key, row.get('assignment_group')),
                b,
                row['cnt'],
            )
            row_oid = row.get('project__org_id')
            if row_oid is not None:
                _add_count(
                    workload_by_assignee_team,
                    f'{key}::org{row_oid}::{_canon_group(row.get("assignment_group"))}',
                    b,
                    row['cnt'],
                )

        team_rows = (
            base
            .exclude(assignment_group__isnull=True)
            .exclude(assignment_group='')
            .annotate(is_future=_future_case)
            .values('assignment_group', 'status', 'is_future', 'project__org_id',
                    'assignee_email', 'assignee_id')
            .annotate(cnt=Count('id'))
        )
        workload_by_group = {}
        unassigned_by_group = {}
        for row in team_rows:
            group = _canon_group(row['assignment_group'])
            if not group:
                continue
            b = _rebucket(_bucket(row['status']), row['is_future'])
            if group not in workload_by_group:
                workload_by_group[group] = _empty()
            workload_by_group[group][b] += row['cnt']
            # Single-team invariant (operator 2026-07-11): track the UNASSIGNED
            # queue separately — team cards are member-sum + this queue, so the
            # team total always equals the sum of its member cards plus what is
            # visibly waiting in the queue.
            if not _canon_email(row.get('assignee_email')) and row.get('assignee_id') is None:
                _add_count(unassigned_by_group, group, b, row['cnt'])
            # HIVE-609: same-named teams exist in EVERY org (four 'Accounting's).
            # Emit an org-qualified key alongside the cross-org total so each
            # org's team card shows ITS work, not the fan-out sum.
            oid = row.get('project__org_id')
            if oid is not None:
                _add_count(workload_by_group, f'org{oid}::{group}', b, row['cnt'])
                if not _canon_email(row.get('assignee_email')) and row.get('assignee_id') is None:
                    _add_count(unassigned_by_group, f'org{oid}::{group}', b, row['cnt'])

        non_actionable_candidates = base.filter(
            status__in=NON_ACTIONABLE_STATUS_SLUGS,
        )
        waiting_on_implementation = non_actionable_candidates.exclude(
            pk__in=filter_active_queue_items(
                non_actionable_candidates,
            ).values('pk')
        )
        waiting_on_followups = base.filter(
            Q(owner_awareness_exists())
            | Q(pk__in=waiting_on_implementation.values('pk'))
        )

        def _move_count(target, key, source_bucket, count):
            if not key or source_bucket == 'blocked':
                return
            counts = target.setdefault(key, _empty())
            counts[source_bucket] -= count
            counts['blocked'] += count

        waiting_rows = (
            waiting_on_followups
            .annotate(is_future=_future_case)
            .values(
                'assignee_email', 'assignee_id', 'assignment_group', 'status',
                'is_future', 'project__org_id',
            )
            .annotate(cnt=Count('id'))
        )
        for row in waiting_rows:
            source_bucket = _rebucket(_bucket(row['status']), row['is_future'])
            count = row['cnt']
            email = _canon_email(row.get('assignee_email'))
            uid = row.get('assignee_id')
            group = _canon_group(row.get('assignment_group'))
            _move_count(workload_by_email, email, source_bucket, count)
            _move_count(workload_by_uid, str(uid) if uid is not None else '', source_bucket, count)
            _move_count(
                workload_by_assignee_team,
                _assignee_team_key(email, group),
                source_bucket,
                count,
            )
            _move_count(
                workload_by_assignee_team,
                _assignee_team_key(str(uid) if uid is not None else '', group),
                source_bucket,
                count,
            )
            _move_count(workload_by_group, group, source_bucket, count)
            oid = row.get('project__org_id')
            if oid is not None:
                _move_count(
                    workload_by_group,
                    f'org{oid}::{group}',
                    source_bucket,
                    count,
                )
            if not email and uid is None:
                _move_count(unassigned_by_group, group, source_bucket, count)
                if oid is not None:
                    _move_count(
                        unassigned_by_group,
                        f'org{oid}::{group}',
                        source_bucket,
                        count,
                    )

        # PLAT-359 (F4): overdue + completed buckets for the progress-at-a-glance panel.
        # overdue = non-terminal tasks past due_date (from `base`: org-scoped +
        # terminal-excluded). completed = done-category tasks closed in the last 30d
        # (bounded so the count stays meaningful), org-scoped to match `base`.
        from datetime import timedelta
        now = timezone.now()
        overdue_base = base.filter(due_date__lt=now)
        for row in (overdue_base.exclude(assignee_email__isnull=True).exclude(assignee_email='')
                    .values('assignee_email', 'assignment_group').annotate(cnt=Count('id'))):
            _e = _canon_email(row['assignee_email'])
            if _e:
                workload_by_email.setdefault(_e, _empty())['overdue'] += row['cnt']
                _add_count(
                    workload_by_assignee_team,
                    _assignee_team_key(_e, row.get('assignment_group')),
                    'overdue',
                    row['cnt'],
                )
        for row in (overdue_base.exclude(assignee_id__isnull=True)
                    .values('assignee_id', 'assignment_group').annotate(cnt=Count('id'))):
            uid_key = str(row['assignee_id'])
            workload_by_uid.setdefault(uid_key, _empty())['overdue'] += row['cnt']
            _add_count(
                workload_by_assignee_team,
                _assignee_team_key(uid_key, row.get('assignment_group')),
                'overdue',
                row['cnt'],
            )
        for row in (overdue_base.exclude(assignment_group__isnull=True).exclude(assignment_group='')
                    .values('assignment_group').annotate(cnt=Count('id'))):
            _g = _canon_group(row['assignment_group'])
            if _g:
                workload_by_group.setdefault(_g, _empty())['overdue'] += row['cnt']

        done_slugs = ({slug for slug, cat in status_cat.items() if cat == 'done'}
                      | {'completed', 'done', 'resolved', 'closed', 'merged'})
        # PLAT-359 (kei P2): window completions on resolution_date (when the item entered
        # a terminal state) with a completed_at fallback — matching the existing stats
        # contract (~L3017). Normal workflow transitions stamp resolution_date, not
        # completed_at, so a completed_at-only filter under-reports completions.
        completed_cutoff = now - timedelta(days=30)
        completed_base = Item.objects.filter(
            Q(resolution_date__gte=completed_cutoff)
            | Q(resolution_date__isnull=True, completed_at__gte=completed_cutoff),
            mode='task', status__in=done_slugs,
        )
        if org_id not in (None, 'all'):
            completed_base = completed_base.filter(project__org_id=org_id)
        for row in (completed_base.exclude(assignee_email__isnull=True).exclude(assignee_email='')
                    .values('assignee_email', 'assignment_group').annotate(cnt=Count('id'))):
            _e = _canon_email(row['assignee_email'])
            if _e:
                workload_by_email.setdefault(_e, _empty())['completed'] += row['cnt']
                _add_count(
                    workload_by_assignee_team,
                    _assignee_team_key(_e, row.get('assignment_group')),
                    'completed',
                    row['cnt'],
                )
        for row in (completed_base.exclude(assignee_id__isnull=True)
                    .values('assignee_id', 'assignment_group').annotate(cnt=Count('id'))):
            uid_key = str(row['assignee_id'])
            workload_by_uid.setdefault(uid_key, _empty())['completed'] += row['cnt']
            _add_count(
                workload_by_assignee_team,
                _assignee_team_key(uid_key, row.get('assignment_group')),
                'completed',
                row['cnt'],
            )
        for row in (completed_base.exclude(assignment_group__isnull=True).exclude(assignment_group='')
                    .values('assignment_group').annotate(cnt=Count('id'))):
            _g = _canon_group(row['assignment_group'])
            if _g:
                workload_by_group.setdefault(_g, _empty())['completed'] += row['cnt']

        return (workload_by_email, workload_by_group, workload_by_uid,
                workload_by_assignee_team, unassigned_by_group)


# ---------------------------------------------------------------------------
# HIVE-237: TeamRoster lifecycle CRUD (create/rename/delete/set-lead/members)
# ---------------------------------------------------------------------------

def _is_admin(user):
    return getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False)


def _serialize_roster(row):
    return {
        'team_name': row.team_name,
        'organization_id': row.organization_id,
        'organization_slug': row.organization_slug,
        'tiers': row.tiers,
        'junior_emails': row.junior_emails,
        'is_active': row.is_active,
        'note': row.note,
        # PLS-745/PLAT-6071: writes are CAS commands keyed on this version;
        # service consumers (Admin JIT forward) must be able to construct a
        # valid command, so the read must carry the current version.
        'version': int(row.version or 0),
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        'updated_by': row.updated_by,
    }


def _canonicalize_roster_member_payload(tiers, junior_emails=None):
    """PLS-1164 roster-write boundary: canonicalize member emails to principals.

    Returns ``(payload, error_response)`` — exactly one is None.

    - phantom emails (shizuha-id has NO principal, verified) -> ``(None, 400)``
      naming the ``phantom_roster_identity`` reason class and the offending
      addresses: an unresolvable label is never silently stored (fail-loud per
      PLS-277 §6).
    - resolution errors (authority unreachable) -> fail OPEN: the original
      emails are kept for those members and a warning is attached; an identity
      outage must never block roster operations.
    - canonicalizable variants (e.g. ``nagi@shizuha.com`` when the principal's
      canonical email is ``nagi@agents.shizuha.io``) are rewritten to the
      canonical form and reported in ``identity_warnings`` — the migration
      signal required by PLS-1164.
    """
    from .services.canonical_identity import (
        REASON_PHANTOM_ROSTER_IDENTITY,
        canonicalize_roster_members,
    )
    result = canonicalize_roster_members(tiers, junior_emails)
    if result['phantoms']:
        return None, Response(
            {
                'detail': (
                    'Roster rejected: member email(s) resolve to no shizuha-id '
                    'principal (reason class: '
                    f'{REASON_PHANTOM_ROSTER_IDENTITY}). Canonicalize or remove '
                    'them; a roster label that matches no principal is a silent '
                    'zero in every routing consumer (PLS-1154/PLS-1164).'
                ),
                'reason_class': REASON_PHANTOM_ROSTER_IDENTITY,
                'phantom_emails': result['phantoms'],
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    payload = {
        'tiers': result['tiers'],
        'junior_emails': result['junior_emails'],
        'identity_warnings': [],
    }
    if result['canonicalized']:
        payload['identity_warnings'].append({
            'reason_class': 'roster_identity_canonicalized',
            'detail': (
                'Member email(s) rewritten to the principal canonical form '
                '(shizuha-id is the identity authority; PLS-1164).'
            ),
            'canonicalized': result['canonicalized'],
        })
    if result['resolution_errors']:
        payload['identity_warnings'].append({
            'reason_class': 'identity_resolution_unavailable',
            'detail': (
                'shizuha-id was unreachable for some member emails; they were '
                'stored as-is and NOT treated as phantoms (fail-open).'
            ),
            'emails': result['resolution_errors'],
        })
    return payload, None


def _resolve_team_roster_organization_id(organization_slug, raw_organization_id=None):
    """Resolve and validate the exact Admin organization for a roster write.

    Team names are organization-local.  A slug without a persisted numeric
    identity creates a roster that the assignment guard must (correctly) reject,
    so every API write resolves the authoritative Admin organization and fails
    closed when the identity cannot be proven.

    The four original tenants retain a compatibility fallback for a transient
    Admin outage because their IDs are already an established schema invariant.
    New tenants are always resolved dynamically and never stored with NULL IDs.
    """
    organization_slug = str(organization_slug or 'shizuha').strip().lower()
    requested_id = None
    if raw_organization_id not in (None, ''):
        try:
            requested_id = int(raw_organization_id)
        except (TypeError, ValueError):
            raise DjangoValidationError({
                'organization_id': 'organization_id must be a positive integer.',
            })
        if requested_id <= 0:
            raise DjangoValidationError({
                'organization_id': 'organization_id must be a positive integer.',
            })

    resolved_id = None
    try:
        from .services.admin_service import admin_service
        organization = admin_service.get_organization_by_slug(organization_slug)
        if isinstance(organization, dict) and organization.get('id') is not None:
            resolved_id = int(organization['id'])
    except (TypeError, ValueError, requests.RequestException):
        resolved_id = None

    if resolved_id is None:
        resolved_id = {
            'shizuha': 1,
            'shizuha-digital': 2,
            'shizuha-trading': 3,
            'personal-3': 4,
        }.get(organization_slug)

    if resolved_id is None:
        raise DjangoValidationError({
            'organization_slug': (
                f'Organization {organization_slug!r} could not be resolved in Admin.'
            ),
        })
    if requested_id is not None and requested_id != resolved_id:
        raise DjangoValidationError({
            'organization_id': (
                f'{organization_slug} is organization {resolved_id}, not {requested_id}.'
            ),
        })
    return resolved_id


class InternalAgentLifecycleView(APIView):
    """POST /api/internal/agent-lifecycle/ — Hive hibernate/wake event.

    Network-secured like the other /internal/ Pulse endpoints. Hive calls this
    the moment a seat hibernates so Pulse can drain ready work onto a live
    teammate instead of waiting for the 10-minute rebalance backstop.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        from .services.workflow_service import WorkflowService
        from .tasks import sweep_rebalance_pods, refresh_agent_health_and_assign_group_queue

        status_val = str(request.data.get('status') or '').strip().lower()
        username = str(request.data.get('username') or '').strip().lower().split('@')[0]
        email = str(request.data.get('email') or '').strip().lower()
        if not email and username:
            email = f'{username}@shizuha.com'
        if not email:
            return Response(
                {'error': 'email or username required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._hibernated_emails_cache = None
        ttl = int(os.environ.get('PULSE_AVAILABILITY_TTL', '600'))
        try:
            rc = WorkflowService._redis_client()
            avail_raw = rc.get(WorkflowService._AGENT_AVAILABILITY_KEY)
            avail = json.loads(avail_raw) if avail_raw else {}
            if not isinstance(avail, dict):
                avail = {}
            reason_raw = rc.get(WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY)
            reasons = json.loads(reason_raw) if reason_raw else {}
            if not isinstance(reasons, dict):
                reasons = {}
            hib_raw = rc.get(WorkflowService._AGENT_HIBERNATED_KEY)
            hibernated = {
                str(e).strip().lower()
                for e in (json.loads(hib_raw) if hib_raw else [])
                if str(e).strip()
            }
            aliases = WorkflowService._agent_email_aliases(email)
            if status_val == 'hibernated':
                for alias in aliases or {email}:
                    avail[alias] = False
                    reasons[alias] = 'hibernated'
                    hibernated.add(alias)
                rc.setex(
                    WorkflowService._AGENT_AVAILABILITY_KEY,
                    ttl, json.dumps(avail),
                )
                rc.setex(
                    WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY,
                    ttl, json.dumps(reasons),
                )
                rc.setex(
                    WorkflowService._AGENT_HIBERNATED_KEY,
                    ttl, json.dumps(sorted(hibernated)),
                )
                sweep_rebalance_pods.apply_async(
                    kwargs={'only_agent_email': email},
                )
                return Response({
                    'ok': True, 'email': email, 'status': 'hibernated',
                    'draining': True,
                })
            if status_val in {'running', 'starting', 'provisioning'}:
                for alias in aliases or {email}:
                    hibernated.discard(alias)
                    if reasons.get(alias) == 'hibernated':
                        reasons.pop(alias, None)
                        avail[alias] = True
                rc.setex(
                    WorkflowService._AGENT_HIBERNATED_KEY,
                    ttl, json.dumps(sorted(hibernated)),
                )
                rc.setex(
                    WorkflowService._AGENT_AVAILABILITY_KEY,
                    ttl, json.dumps(avail),
                )
                rc.setex(
                    WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY,
                    ttl, json.dumps(reasons),
                )
                refresh_agent_health_and_assign_group_queue.apply_async(
                    kwargs={'only_agent_email': email},
                )
                return Response({
                    'ok': True, 'email': email, 'status': status_val,
                    'recovering': True,
                })
        except Exception:
            logger.exception(
                'internal agent-lifecycle update failed for %s status=%s',
                email, status_val,
            )
            return Response(
                {'ok': False, 'error': 'lifecycle_update_failed'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'ok': True, 'email': email, 'ignored': True, 'status': status_val})


class InternalTeamRosterView(APIView):
    """GET /api/internal/team-roster/ — read-only roster snapshot for
    service-to-service consumers (PLAT-4220: admin org-chart derives its
    agent task-team membership from this, the single source of truth).

    Network-secured/AllowAny in v1 — the same trust model as the ID
    ``/api/internal/agents/`` inventory the admin reconcile reads alongside
    this. Read-only; never mutates. A service-token gate is a noted follow-up.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        from .models import TeamRoster
        rows = TeamRoster.objects.filter(is_active=True).order_by('organization_slug', 'team_name')
        return Response({'teams': [_serialize_roster(r) for r in rows]})


class InternalTeamRosterMemberMutationView(APIView):
    """POST /api/internal/team-roster/members/ — service-to-service roster
    mutation (PLAT-6071).

    Pulse is the single authority for roster membership; Admin is the single
    authority for JIT grants. Admin validates + consumes the JIT grant, then
    forwards the exact mutation here (network-secured/AllowAny, the same trust
    model as the read-only InternalTeamRosterView). This endpoint applies the
    payload-bound CAS command and returns its durable receipt. It does NOT
    validate or consume any JIT grant — that stays in Admin so grant scope /
    TTL / idempotency / audit binding is never split across services.

    A stale ``expected_version`` returns 409 ``expected_version_conflict``
    WITHOUT mutating the roster or consuming anything — Admin leaves the grant
    approved on that path (acceptance #4). Retrying the same ``command_id`` is
    idempotent (replays the stored receipt).
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        from .services.team_roster_commands import (
            TeamRosterCommandError,
            execute_team_roster_command,
        )
        try:
            result = execute_team_roster_command(
                command_id=request.data.get('command_id')
                or request.headers.get('Idempotency-Key'),
                action=request.data.get('action') or '',
                organization_id=request.data.get('organization_id'),
                organization_slug=request.data.get('organization_slug') or 'shizuha',
                team_name=request.data.get('team_name') or '',
                email=request.data.get('email') or '',
                expected_version=request.data.get('expected_version'),
                actor_email=request.data.get('actor_email') or '',
            )
        except TeamRosterCommandError as exc:
            conflict_codes = {
                'expected_version_conflict', 'idempotency_payload_conflict',
            }
            return Response(
                {'error': exc.code, 'detail': exc.detail},
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.code in conflict_codes
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        return Response(result, status=status.HTTP_200_OK)


class InternalTeamCapabilitiesView(APIView):
    """GET /api/internal/team-capabilities/ — read-only team → capability-tags
    snapshot for service-to-service consumers (operator 2026-07-11).

    Pulse is the SoT for a team's abstract capability TAGS; Hive reads this and
    maps each tag → concrete resources (skills, MCP servers, credential scopes)
    on its own end — it must NOT re-declare which teams are privileged. Same
    network-secured/AllowAny trust model as InternalTeamRosterView; read-only.

    Response: ``{"teams": {"<team_name>": ["<cap>", ...], ...}}``.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        from .models import TeamCapabilityProfile
        rows = TeamCapabilityProfile.objects.all().order_by('team_name')
        return Response({'teams': {r.team_name: list(r.capabilities or []) for r in rows}})


class TeamRosterListCreateView(APIView):
    """GET /api/admin/team-roster/ — list all rosters.
    POST /api/admin/team-roster/ — create an empty team.
    Admin/staff only.
    """
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [HiveFleetReadAuthentication, *APIView.authentication_classes]

    def get(self, request):
        if not (_is_admin(request.user) or is_hive_fleet_reader(request)):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        from .models import TeamRoster
        rows = TeamRoster.objects.all().order_by('organization_slug', 'team_name')
        return Response({'teams': [_serialize_roster(r) for r in rows]})

    def post(self, request):
        if not _is_admin(request.user):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        from .models import TeamRoster
        from django.core.exceptions import ValidationError
        team_name = (request.data.get('team_name') or '').strip()
        normalized = team_name.lower().replace('_', '-').replace(' ', '-')
        team_name = TeamRoster._CANONICAL_ALIASES.get(normalized, normalized)
        organization_slug = str(request.data.get('organization_slug') or 'shizuha').strip().lower()
        if not team_name:
            return Response({'detail': 'team_name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            organization_id = _resolve_team_roster_organization_id(
                organization_slug,
                request.data.get('organization_id'),
            )
        except DjangoValidationError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        tiers = request.data.get('tiers', [])
        junior_emails = request.data.get('junior_emails', [])
        note = request.data.get('note', '')
        actor = getattr(request.user, 'username', '') or getattr(request.user, 'email', '')
        try:
            TeamRoster.validate_roster_structure(tiers, junior_emails)
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        # PLS-1164: canonicalize member emails against shizuha-id BEFORE the
        # row exists — phantom labels are rejected, canonicalizable variants
        # are rewritten (identity_warnings reported on success).
        canon, canon_err = _canonicalize_roster_member_payload(tiers, junior_emails)
        if canon_err is not None:
            return canon_err
        tiers = canon['tiers']
        junior_emails = canon['junior_emails']
        identity_warnings = canon['identity_warnings']
        if TeamRoster.objects.filter(
                team_name=team_name, organization_id=organization_id,
                organization_slug=organization_slug).exists():
            return Response({'detail': f'Team {team_name!r} already exists in {organization_slug!r}.'},
                            status=status.HTTP_409_CONFLICT)
        try:
            row = TeamRoster(team_name=team_name, organization_slug=organization_slug,
                             organization_id=organization_id,
                             tiers=tiers, junior_emails=junior_emails,
                             note=note, is_active=True, updated_by=actor)
            row.save()
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
                return Response({'detail': f'Team {team_name!r} already exists.'},
                                status=status.HTTP_409_CONFLICT)
            raise
        payload = _serialize_roster(row)
        if identity_warnings:
            payload['identity_warnings'] = identity_warnings
        return Response(payload, status=status.HTTP_201_CREATED)


class TeamRosterDetailView(APIView):
    """GET/PATCH/DELETE /api/admin/team-roster/<team_name>/.
    PATCH supports: team_name (rename), lead_email (set tiers[0][0]),
    tiers, junior_emails, note, is_active.
    DELETE marks is_active=False.
    Admin/staff only.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _get_row(self, team_name, organization_slug='shizuha'):
        from .models import TeamRoster
        # Canonicalize BEFORE lookup. Looking up an exact legacy tombstone first
        # allowed a stale PATCH to reactivate the retired row instead of reaching
        # the surviving team (operator-reported Research duplicate, 2026-07-16).
        canon = team_name.strip().lower().replace('_', '-').replace(' ', '-')
        canon = TeamRoster._CANONICAL_ALIASES.get(canon, canon)
        row = TeamRoster.objects.filter(
            team_name=canon, organization_slug=organization_slug,
        ).first()
        return row

    def get(self, request, team_name):
        if not _is_admin(request.user):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        row = self._get_row(
            team_name, str(request.query_params.get('organization_slug') or 'shizuha').strip().lower(),
        )
        if not row:
            return Response({'detail': f'Team {team_name!r} not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_roster(row))

    def patch(self, request, team_name):
        if not _is_admin(request.user):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        from .models import TeamRoster
        from django.core.exceptions import ValidationError
        row = self._get_row(
            team_name,
            str(request.data.get('organization_slug') or request.query_params.get('organization_slug') or 'shizuha').strip().lower(),
        )
        if not row:
            return Response({'detail': f'Team {team_name!r} not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        actor = getattr(request.user, 'username', '') or getattr(request.user, 'email', '')

        if 'lead_email' in data and 'tiers' in data:
            return Response(
                {'detail': 'Provide either lead_email or tiers, not both. '
                           'If both are sent, tiers silently overwrites the lead_email change.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # rename
        if 'team_name' in data:
            row.team_name = (data['team_name'] or '').strip()

        # set lead: place lead_email at tiers[0][0], preserving other members
        if 'lead_email' in data:
            lead = (data['lead_email'] or '').strip().lower()
            if not lead:
                return Response({'detail': 'lead_email must be a non-empty email.'}, status=status.HTTP_400_BAD_REQUEST)
            tiers = list(row.tiers) if row.tiers else []
            if not tiers:
                tiers = [[lead]]
            else:
                tier0 = [m for m in tiers[0] if m != lead]
                tiers[0] = [lead] + tier0
            row.tiers = tiers

        if 'tiers' in data or 'junior_emails' in data or 'lead_email' in data:
            # PLS-1164: canonicalize whatever member surface this PATCH touches
            # (tiers / junior_emails / lead_email) before persisting — phantom
            # labels rejected, variants rewritten, outages fail open.
            _pending_tiers = data['tiers'] if 'tiers' in data else row.tiers
            _pending_juniors = data['junior_emails'] if 'junior_emails' in data else (row.junior_emails or [])
            canon, canon_err = _canonicalize_roster_member_payload(
                _pending_tiers, _pending_juniors,
            )
            if canon_err is not None:
                return canon_err
            identity_warnings = canon['identity_warnings']
            if 'tiers' in data or 'lead_email' in data:
                row.tiers = canon['tiers']
            if 'junior_emails' in data:
                row.junior_emails = canon['junior_emails']
        else:
            identity_warnings = []
        if 'note' in data:
            row.note = data['note']
        if 'is_active' in data:
            row.is_active = bool(data['is_active'])

        row.updated_by = actor
        try:
            row.save()
        except ValidationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
                return Response({'detail': f'Team name already exists.'}, status=status.HTTP_409_CONFLICT)
            raise
        payload = _serialize_roster(row)
        if identity_warnings:
            payload['identity_warnings'] = identity_warnings
        return Response(payload)

    def delete(self, request, team_name):
        if not _is_admin(request.user):
            return Response({'detail': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)
        row = self._get_row(
            team_name, str(request.query_params.get('organization_slug') or 'shizuha').strip().lower(),
        )
        if not row:
            return Response({'detail': f'Team {team_name!r} not found.'}, status=status.HTTP_404_NOT_FOUND)
        actor = getattr(request.user, 'username', '') or getattr(request.user, 'email', '')
        row.is_active = False
        row.updated_by = actor
        row.save()
        return Response(_serialize_roster(row))


# =============================================================================
# PLS-171 — live dashboard/task updates via backend-pushed deltas (Slice 1)
# =============================================================================
class TaskEventSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = TaskEvent
        fields = [
            'seq', 'kind', 'project_id', 'item_id', 'item_key',
            'actor_user_id', 'payload', 'created_at',
        ]


class TaskEventListView(APIView):
    """GET /api/events?since=<seq> — cursor-delta feed for live frontend updates.

    Returns TaskEvents AFTER ``since`` that the caller can access, oldest-first,
    plus the new ``cursor`` so the client polls with ``?since=<cursor>`` and
    patches its loaded dashboard / list / detail cache from the deltas instead of
    forcing a refresh. Reconnect = a bounded ``?since=<last cursor>`` resync.

    Access is scoped to the same projects ``PermissionService`` grants for items
    (superuser → all; else the caller's accessible projects + personal items they
    acted on), so a caller only receives deltas for tasks they can access.
    """
    permission_classes = [permissions.IsAuthenticated]
    MAX_LIMIT = 500
    DEFAULT_LIMIT = 200

    def get(self, request):
        def _int(name, default):
            try:
                return int(request.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        since = max(0, _int('since', 0))
        limit = min(max(1, _int('limit', self.DEFAULT_LIMIT)), self.MAX_LIMIT)

        user = request.user
        qs = TaskEvent.objects.filter(seq__gt=since)

        if not getattr(user, 'is_superuser', False):
            # Mirror the canonical item resolver PermissionService.get_user_item_queryset:
            #   L1/L2 — projects the caller can access (org-admin + explicit membership)
            #   L3    — items where the caller is assignee OR creator, ALWAYS visible
            #           regardless of project/org membership (a cross-project assignee
            #           must still get live deltas for their task).
            # plus personal-item events they acted on. Never over-shares.
            uid = getattr(user, 'id', None)
            accessible_project_ids = list(
                PermissionService.get_user_projects(user=user).values_list('id', flat=True)
            )
            scope = Q(project_id__in=accessible_project_ids)

            # L3: resolve the item_ids the caller owns (assignee/creator), by id and
            # by email (incl. the PLAT-533 @shizuha.com↔@agents.shizuha.io alias), and
            # union their events in — covers cross-project assigned/created tasks.
            owner_q = Q()
            if uid is not None:
                owner_q |= Q(assignee_id=uid) | Q(created_by_id=uid)
            email = (getattr(user, 'email', '') or '').lower()
            if email:
                emails = [email]
                if email.endswith('@shizuha.com'):
                    emails.append(email[:-len('@shizuha.com')] + '@agents.shizuha.io')
                elif email.endswith('@agents.shizuha.io'):
                    emails.append(email[:-len('@agents.shizuha.io')] + '@shizuha.com')
                owner_q |= Q(assignee_email__in=emails) | Q(created_by_email__in=emails)
            if owner_q:
                owner_item_ids = list(
                    Item.objects.filter(owner_q).values_list('id', flat=True)
                )
                if owner_item_ids:
                    scope |= Q(item_id__in=owner_item_ids)

            if uid is not None:
                scope |= Q(project_id__isnull=True, actor_user_id=uid)
            qs = qs.filter(scope)

        events = list(qs.order_by('seq')[:limit])
        cursor = events[-1].seq if events else since
        return Response({
            'events': TaskEventSerializer(events, many=True).data,
            'cursor': cursor,
            'has_more': len(events) == limit,
        })


def pulse_metrics(request):
    """Prometheus text exposition for Pulse self-check metrics."""
    from django.http import HttpResponse

    from .services.recurring_invariants import (
        collect_recurring_invariant_metrics,
        render_prometheus_metrics,
    )

    body = render_prometheus_metrics(collect_recurring_invariant_metrics())
    # PLS-374: append the no-routable-candidate routability-hold counter (PLS-278
    # bake signal for the PULSE_ROUTABILITY_ENFORCE shadow->enforce flip).
    from .services.workflow_service import WorkflowService
    routability_body = WorkflowService.render_routability_hold_metrics()
    if routability_body:
        body = body.rstrip('\n') + '\n' + routability_body + '\n'
    substrate_body = WorkflowService.render_substrate_shadow_metrics()
    if substrate_body:
        body = body.rstrip('\n') + '\n' + substrate_body + '\n'
    from .services.routing_authority import render_routing_authority_metrics
    authority_body = render_routing_authority_metrics()
    if authority_body:
        body = body.rstrip('\n') + '\n' + authority_body
    from .services.escalation_routing import render_escalation_blocker_metrics
    body = body.rstrip('\n') + '\n' + render_escalation_blocker_metrics()
    # PLAT-4601: workload-identity probe outcomes use fixed labels only.  The
    # probe token, source IP, TokenReview body, and exception text never enter
    # the exposition.
    from .probe_views import render_probe_auth_metrics
    body = body.rstrip('\n') + '\n' + render_probe_auth_metrics()
    from .services.org_health_snapshot_probe import render_org_health_snapshot_metrics
    body = body.rstrip('\n') + '\n' + render_org_health_snapshot_metrics()
    # Operator directive 2026-09-17: urgent/high tasks assigned to no one —
    # charted in Grafana and alertable before they become fleet-wide blockage.
    from .services.urgent_unassigned_metrics import render_urgent_unassigned_metrics
    body = body.rstrip('\n') + '\n' + render_urgent_unassigned_metrics()
    # PLS-1416: read-only broker queue depth — backlog recurrence visible in
    # one scrape instead of via snapshot-staleness DMs (remediation 2).
    from .services.broker_backlog_metrics import render_broker_backlog_metrics
    body = body.rstrip('\n') + '\n' + render_broker_backlog_metrics()
    # PLS-483: attachment-storage detective canary — bounded phase/reason series
    # produced by the in-boundary periodic task; no object keys/creds in labels.
    from .services.attachment_canary_runner import render_attachment_canary_metrics
    body = body.rstrip('\n') + '\n' + render_attachment_canary_metrics()
    # PLS-494: durable, Redis-independent agent-liveness marker-write failure
    # counter — the queriable health surface AlertManager alerts off (a marker
    # write failing means an authenticated owner's liveness evidence is being
    # lost, which the failover drain would otherwise treat as hard-off).
    from .agent_liveness import render_agent_liveness_metrics
    body = body.rstrip('\n') + '\n' + render_agent_liveness_metrics()
    # PLAT-11804: per-principal runtime estate gauge — the converged
    # discriminator surface (ok / down / backoff) so a frozen stop is
    # classifiable instead of prose-only.
    from .runtime_estate_metrics import render_agent_runtime_estate_metrics
    body = body.rstrip('\n') + '\n' + render_agent_runtime_estate_metrics()
    # PLS-728: project-membership projection serve outcomes — the family is
    # always rendered (zero-filled) so a detector can alert on absent() as well
    # as on `unavailable` increments.
    from .services.project_membership_metrics import render_project_membership_metrics
    body = body.rstrip('\n') + '\n' + render_project_membership_metrics()
    # PLAT-5343: projection serve-duration histogram — always rendered
    # (zero-filled) so the latency tail is observable under normal load.
    from .services.project_membership_metrics import render_project_membership_latency_metrics
    body = body.rstrip('\n') + '\n' + render_project_membership_latency_metrics()
    return HttpResponse(body, content_type='text/plain; version=0.0.4; charset=utf-8')
# =============================================================================
# PLS-306 — AI Work Intake: natural-language task draft/confirm pipeline
# =============================================================================

class TaskDraftProposalView(APIView):
    """
    AI Work Intake pipeline — create, list, confirm, reject task drafts.

    POST   /api/v1/ai-work-intake/drafts/         — create a draft proposal
    GET    /api/v1/ai-work-intake/drafts/          — list drafts (filterable)
    GET    /api/v1/ai-work-intake/drafts/<id>/     — get a single draft
    POST   /api/v1/ai-work-intake/drafts/<id>/confirm/  — confirm → create task
    POST   /api/v1/ai-work-intake/drafts/<id>/reject/   — reject draft
    """
    permission_classes = [permissions.IsAuthenticated]

    def _accessible_proposals(self, request):
        if request.user.is_staff or request.user.is_superuser:
            return TaskDraftProposal.objects.filter()
        accessible_project_ids = _accessible_projects_for_user(request.user).values('id')
        return TaskDraftProposal.objects.filter(
            org_id__in=_user_organization_ids(request.user),
            requester_id=request.user.id,
            proposed_project_id__in=accessible_project_ids,
        )

    def get(self, request, pk=None):
        """GET — list drafts or get a single draft."""
        if pk:
            try:
                proposal = self._accessible_proposals(request).get(pk=pk)
            except TaskDraftProposal.DoesNotExist:
                return Response(
                    {'error': f'Draft proposal {pk} not found'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(TaskDraftProposalSerializer(proposal).data)

        # List with optional filters
        qs = self._accessible_proposals(request)
        org_id = request.query_params.get('org_id')
        if org_id:
            qs = qs.filter(org_id=org_id)
        validation_status = request.query_params.get('validation_status')
        if validation_status:
            qs = qs.filter(validation_status=validation_status)
        requester_id = request.query_params.get('requester_id')
        if requester_id:
            qs = qs.filter(requester_id=requester_id)
        limit = int(request.query_params.get('limit', 20))
        qs = qs.order_by('-created_at')[:limit]
        return Response(TaskDraftProposalSerializer(qs, many=True).data)

    def post(self, request):
        """POST — create a new draft proposal."""
        serializer = TaskDraftCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        user = request.user

        project = _require_accessible_project(user, data['proposed_project_id'], permission='create')
        org_id = project.org_id

        from .services.ai_work_intake import create_draft_proposal

        proposal = create_draft_proposal(
            org_id=org_id,
            raw_prompt=data['raw_prompt'],
            requester_id=getattr(user, 'id', None),
            requester_email=getattr(user, 'email', ''),
            source=data.get('source', 'api'),
            proposed_project_id=data['proposed_project_id'],
            proposed_workflow_slug=data['proposed_workflow_slug'],
            proposed_title=data['proposed_title'],
            proposed_description=data.get('proposed_description', ''),
            proposed_priority=data.get('proposed_priority', ''),
            proposed_assignment_group=data.get('proposed_assignment_group', ''),
            proposed_labels=data.get('proposed_labels', []),
            proposed_tags=data.get('proposed_tags', []),
            proposed_parent_item_id=data.get('proposed_parent_item_id', ''),
            model_used=data.get('model_used', ''),
        )

        response_data = TaskDraftProposalSerializer(proposal).data
        http_status = status.HTTP_201_CREATED

        if proposal.validation_status == 'duplicate':
            http_status = status.HTTP_409_CONFLICT
        elif proposal.validation_status == 'invalid':
            http_status = status.HTTP_422_UNPROCESSABLE_ENTITY

        return Response(response_data, status=http_status)


class TaskDraftConfirmView(APIView):
    """
    POST /api/v1/ai-work-intake/drafts/<id>/confirm/
    Confirm a draft proposal and create a real task.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        from .services.ai_work_intake import confirm_draft

        user = request.user
        visible_proposals = TaskDraftProposalView()._accessible_proposals(request)
        item, error = confirm_draft(
            proposal_id=pk,
            confirmed_by_id=getattr(user, 'id', 0),
            proposal_queryset=visible_proposals,
        )
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'message': f'Task {item.item_key} created from draft {pk}',
            'item_key': item.item_key,
            'item_id': item.id,
        })


class TaskDraftRejectView(APIView):
    """
    POST /api/v1/ai-work-intake/drafts/<id>/reject/
    Reject a draft proposal.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        serializer = TaskDraftRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from .services.ai_work_intake import reject_draft

        visible_proposals = TaskDraftProposalView()._accessible_proposals(request)
        error = reject_draft(
            proposal_id=pk,
            reason=serializer.validated_data.get('reason', ''),
            proposal_queryset=visible_proposals,
        )
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'message': f'Draft {pk} rejected'})


class InternalQueueLaneProviderWaitView(APIView):
    """PLAT-10083 canonical seat-scoped provider_wait surface.

    Wire contract anchored on the deployed runtime writer (shizuha-beta
    ``src/gateway/provider-wait-writer.ts`` @ ``000cccca``, source-verified in
    rei review 910927): the writer reports a terminal stop by SEAT identity —
    ``POST /api/internal/queue-lane/provider-wait/`` with ``email`` (or
    ``username``), ``signature``, ``reason``, ``set_by`` — and has no item
    identity and no item-API authentication. This surface resolves the seat's
    currently-held active items and installs the provider_wait marker on each,
    through the ONE marker authority (``ProviderWaitMarker`` — the closed
    duplicate decision is preserved; no second authority is introduced).

    Trust model: PLAT-6071 internal (network-secured, AllowAny; the
    ``/api/internal/*`` ingress guard 404-masks ingress-borne traffic).
    """

    permission_classes = [permissions.AllowAny]

    def _resolve_seat_email(self, payload):
        """Accept ``email`` or ``username`` (either-or, per the writer)."""
        seat = str(payload.get('email') or '').strip().lower()
        if seat:
            return seat, None
        username = str(payload.get('username') or '').strip()
        if not username:
            return None, "one of 'email' or 'username' is required"
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(
            username__iexact=username).first()
        if user is None or not (getattr(user, 'email', '') or '').strip():
            return None, f"unknown seat username {username!r}"
        return user.email.strip().lower(), None

    def _seat_items(self, seat_email):
        from .models import Item
        from .tasks import _is_terminal_status
        items = []
        for item in Item.objects.filter(assignee_email__iexact=seat_email):
            status = (item.status or '').strip().lower()
            if status == 'backlog' or _is_terminal_status(status):
                continue
            items.append(item)
        return items

    def post(self, request):
        from .services.provider_wait import ProviderWaitError, set_provider_wait

        payload = request.data or {}
        seat_email, err = self._resolve_seat_email(payload)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
        set_by = str(payload.get('set_by') or 'runtime')[:64]
        reason = str(payload.get('reason') or '')
        marked = []
        try:
            for item in self._seat_items(seat_email):
                marker, changed = set_provider_wait(
                    item,
                    signature_class=payload.get('signature'),
                    stop_message_id=reason,
                    source=set_by,
                )
                marked.append({
                    'item_id': item.pk,
                    'item_key': item.item_key,
                    'changed': changed,
                    'state': marker.state,
                    'signature_class': marker.signature_class,
                    'generation': marker.generation,
                })
        except ProviderWaitError as exc:
            return Response({'error': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'seat': seat_email, 'set_by': set_by,
                         'items': marked})


class InternalQueueLaneProviderWaitClearView(APIView):
    """PLAT-10083 success-only clear, seat-scoped.

    Matches the writer's ``clearProviderWaitOnSuccess``:
    ``POST /api/internal/queue-lane/provider-wait:clear/``. Reuses the ONE
    auto-clear authority (ANDON-freshness guard preserved: a failed attempt
    never clears). Clearing an inactive/absent marker is a typed no-op, so
    replay is benign.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from .models import ProviderWaitMarker
        from .services.provider_wait import auto_clear_provider_wait

        payload = request.data or {}
        seat_email, err = InternalQueueLaneProviderWaitView() \
            ._resolve_seat_email(payload)
        if err:
            return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)
        results = []
        for item in InternalQueueLaneProviderWaitView()._seat_items(seat_email):
            marker = getattr(item, 'provider_wait_marker', None)
            if marker is None:
                results.append({'item_id': item.pk, 'item_key': item.item_key,
                                'cleared': False, 'live': False})
                continue
            _marker, cleared, refused_reason = auto_clear_provider_wait(
                item, source='runtime-success')
            results.append({
                'item_id': item.pk,
                'item_key': item.item_key,
                'cleared': cleared,
                'live': _marker.state == 'active' if _marker else False,
                'refused_reason': refused_reason,
            })
        active = ProviderWaitMarker.objects.filter(
            item__assignee_email__iexact=seat_email, state='active').count()
        return Response({'seat': seat_email, 'items': results,
                         'active_remaining': active})


class InternalQueueLaneListView(APIView):
    """PLAT-10083 manager enumeration: every ACTIVE provider_wait marker,
    seat-addressed (the promised ``GET /api/internal/queue-lane/``)."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from .models import ProviderWaitMarker
        rows = []
        for marker in ProviderWaitMarker.objects.filter(
                state='active').select_related('item'):
            item = marker.item
            rows.append({
                'item_id': item.pk,
                'item_key': item.item_key,
                'seat': item.assignee_email,
                'signature_class': marker.signature_class,
                'generation': marker.generation,
                'last_set_at': marker.last_set_at,
                'activated_at': marker.activated_at,
            })
        return Response({'markers': rows, 'count': len(rows)})
