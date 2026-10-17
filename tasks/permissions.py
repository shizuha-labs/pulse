"""
Custom permission classes for user-scoped data access.

These permissions enforce strict data isolation between users,
with organization-scoped data isolation.
"""

from django.db.models import Q
from rest_framework import permissions


class GlobalConfigurationPermission(permissions.BasePermission):
    """Global Pulse catalogues are readable, but only platform staff mutate them.

    Workflow/status/screen/security/permission/notification catalogues have no
    tenant foreign key by design.  Treating that as permission for every
    authenticated principal to alter them made one tenant's write affect the
    whole service.  Reads remain available because ordinary item/workflow UIs
    resolve these catalogues; every unsafe method is platform-authority only.
    """

    message = 'Platform staff authority is required to change global configuration.'

    def has_permission(self, request, view):
        if not getattr(request.user, 'is_authenticated', False):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(
            getattr(request.user, 'is_staff', False)
            or getattr(request.user, 'is_superuser', False)
        )

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


class RelatedProjectPermission(permissions.BasePermission):
    """Apply ProjectPermission to resources owned by a project relation."""

    @staticmethod
    def _project_permission(request, project, *, permission=None):
        from .services.permissions import PermissionService

        required_permission = permission or {
            'GET': 'browse',
            'HEAD': 'browse',
            'OPTIONS': 'browse',
            'DELETE': 'delete',
            'PUT': 'edit',
            'PATCH': 'edit',
            # A POST to an existing object is a mutation, not a create.  The
            # collection-level ``create`` action is selected explicitly below.
            'POST': 'edit',
        }.get(request.method, 'browse')
        return PermissionService.can_access_project(
            getattr(request.user, 'id', None),
            project,
            permission=required_permission,
            user_email=getattr(request.user, 'email', None),
            user=request.user,
        )

    @staticmethod
    def _project_from_request(request):
        project_id = request.data.get('project') or request.data.get('project_id')
        if project_id:
            from .models import Project
            return Project.objects.filter(id=project_id).first()

        board_id = request.data.get('board') or request.data.get('board_id')
        if board_id:
            from .models import Board
            board = Board.objects.select_related('project').filter(id=board_id).first()
            return board.project if board else None
        return None

    def has_permission(self, request, view):
        if not getattr(request.user, 'is_authenticated', False):
            return False
        if getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return True
        action = getattr(view, 'action', None)
        if action in {'create', 'update', 'partial_update'}:
            project = self._project_from_request(request)
            if action == 'create' and project is None:
                return False
            if project is not None:
                permission = 'create' if action == 'create' else 'edit'
                return self._project_permission(
                    request, project, permission=permission,
                )
        return True

    def has_object_permission(self, request, view, obj):
        if getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return True
        project = getattr(obj, 'project', None)
        if project is None and getattr(obj, 'item', None) is not None:
            project = getattr(obj.item, 'project', None)
        if project is None and getattr(obj, 'rule', None) is not None:
            project = getattr(obj.rule, 'project', None)
        if project is None and getattr(obj, 'board', None) is not None:
            project = getattr(obj.board, 'project', None)
        if project is None:
            return False
        return self._project_permission(request, project)


class DashboardOwnerPermission(permissions.BasePermission):
    """Dashboards are owner-only except for explicitly public reads.

    The legacy ``project`` and ``org`` values have no corresponding project or
    organization foreign key on ``Dashboard``.  Until that ownership relation
    exists, treating those labels as shared would be an unenforceable tenant
    boundary, so they deliberately remain owner-only.
    """

    def has_permission(self, request, view):
        if not getattr(request.user, 'is_authenticated', False):
            return False
        if getattr(view, 'action', None) == 'create' and request.data.get('dashboard'):
            from .models import Dashboard
            dashboard = Dashboard.objects.filter(id=request.data.get('dashboard')).first()
            return bool(dashboard and self.has_object_permission(request, view, dashboard))
        return True

    def has_object_permission(self, request, view, obj):
        dashboard = getattr(obj, 'dashboard', obj)
        if getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return True
        owned = getattr(dashboard, 'owner_id', None) == getattr(request.user, 'id', None)
        if request.method in permissions.SAFE_METHODS:
            return owned or getattr(dashboard, 'visibility', 'private') == 'public'
        return owned


def _email_variants(email):
    """Return the email plus the known Shizuha agent/company alias, if any."""
    raw = str(email or '').strip().lower()
    if not raw:
        return []
    variants = [raw]
    if raw.endswith('@shizuha.com'):
        variants.append(raw[:-len('@shizuha.com')] + '@agents.shizuha.io')
    elif raw.endswith('@agents.shizuha.io'):
        variants.append(raw[:-len('@agents.shizuha.io')] + '@shizuha.com')
    return list(dict.fromkeys(variants))


def _participant_slot_matches(stored_email, stored_id, user_email, user_id):
    """Match one participant slot using its canonical identity first.

    PLS-23/PLS-415: Pulse-local numeric IDs can drift or collide.  A nonblank
    stored email is therefore authoritative for that slot; the numeric ID is a
    legacy fallback only when the same slot's email is absent.
    """
    canonical_email = (stored_email or '').strip().lower()
    if canonical_email:
        return canonical_email in set(_email_variants(user_email))
    return user_id is not None and stored_id == user_id


def routing_subject_can_access_item(user, item):
    """Return None for a legacy lane, otherwise the fenced binding decision.

    A routing binding never becomes TeamRoster/participant authority.  On an
    armed lane only the current assignee with an independent, fresh exact-item
    receipt entitlement qualifies through this helper; creators/reporters and
    assignment-seeded ProjectMembership rows do not.
    """
    if user is None or item is None or not getattr(item, 'project_id', None):
        return None
    try:
        from .services.routing_authority import (
            binding_candidates_for_item,
            subject_authorized_for_item,
        )
        snapshot = binding_candidates_for_item(item.assignment_group, item)
        if snapshot is None:
            return None
        user_id = getattr(user, 'id', None)
        if not user_id or not str(user_id).isdigit():
            return False
        assignee_email = (getattr(item, 'assignee_email', '') or '').strip().lower()
        if not _participant_slot_matches(
            assignee_email,
            getattr(item, 'assignee_id', None),
            getattr(user, 'email', None),
            int(user_id),
        ):
            return False
        return subject_authorized_for_item(
            int(user_id), item, team_name=item.assignment_group,
        ).allowed
    except Exception:
        return False


def is_org_admin_or_owner(user, org_id=None) -> bool:
    """
    True iff the user is owner/admin in the given org (preferred) or in
    any org (fallback for call sites without object context).

    Reads `user.organization_memberships` ({org_id_int: role}), which is
    populated by the JWT auth layer from the `organization_memberships`
    claim (with a self-heal fetch from shizuha-admin when the claim is
    absent). Superusers always pass.

    Per-org check (when `org_id` is given) is the correct, narrowly-
    scoped form. The any-org fallback exists for legacy call sites that
    don't know which object they're guarding — those should be migrated
    to pass `org_id` once we walk through the permission classes.
    """
    if getattr(user, 'is_superuser', False):
        return True
    if org_id is not None:
        return _org_role(user, org_id) in ('owner', 'admin')
    # Fallback: admin/owner in ANY org already on the token. Only safe for
    # shortcuts that have no per-object scope.
    memberships = getattr(user, 'organization_memberships', {}) or {}
    return any(role in ('owner', 'admin') for role in memberships.values())


def is_item_participant(user, item) -> bool:
    """
    True iff `user` is a PARTICIPANT of `item` — its assignee or its reporter
    (creator) — matched by canonical email/alias, with legacy ID fallback only
    when the corresponding stored email is blank.

    Participants can always read, comment on, and write fields of their OWN item
    regardless of project membership. This is the canonical participant check
    used by both the item write-permission class and the comment viewset, so the
    two can never drift (PLAT-257: comments/PATCH were denied to assignees and
    reporters on project-less items and on items in projects they don't belong
    to, because the comment path lacked this check and only matched by id).
    """
    if user is None or item is None:
        return False
    routing_access = routing_subject_can_access_item(user, item)
    if routing_access is not None:
        return routing_access
    project = getattr(item, 'project', None)
    if project is not None and not _is_org_member(
        user, getattr(project, 'org_id', None),
    ) and not is_item_team_member(user, item):
        return False
    user_id = getattr(user, 'id', None)
    user_email = getattr(user, 'email', None)
    return _participant_slot_matches(
        getattr(item, 'assignee_email', None),
        getattr(item, 'assignee_id', None),
        user_email,
        user_id,
    ) or _participant_slot_matches(
        getattr(item, 'created_by_email', None),
        getattr(item, 'created_by_id', None),
        user_email,
        user_id,
    )


def is_item_team_member(user, item) -> bool:
    """PLS-364: True iff `user` is a member of `item.assignment_group`'s roster.

    Team-routed work is often picked up by roster members who are neither the
    assignee nor project members (private projects like Ventures) — they must
    still be able to comment on the team's items. Uses the live DB-backed
    TeamRoster (all tiers), matched case-insensitively by email and by the
    known @shizuha.com / @agents.shizuha.io aliases.
    """
    if user is None or item is None:
        return False
    team = (getattr(item, 'assignment_group', None) or '').strip()
    if not team:
        return False
    user_email = (getattr(user, 'email', None) or '').strip().lower()
    if not user_email:
        return False
    try:
        from .services.workflow_service import WorkflowService
        tiers, _roster_org = WorkflowService._team_roster_entry_for_item(team, item)
        if tiers is None:
            return False
        user_variants = set(_email_variants(user_email))
        members = {
            (m or '').strip().lower()
            for m in WorkflowService._flat_roster(tiers)
        }
        return bool(user_variants & members)
    except Exception:
        # Authorization helper: fail closed on any roster-shape surprise.
        return False


def _active_project_membership(user, project):
    """Return active ProjectMembership by user id OR email alias, if present."""
    if user is None or project is None:
        return None

    q_filter = None
    try:
        int_id = int(getattr(user, 'id', None))
        q_filter = Q(user_id=int_id)
    except (TypeError, ValueError):
        pass

    for variant in _email_variants(getattr(user, 'email', None)):
        clause = Q(user_email__iexact=variant)
        q_filter = clause if q_filter is None else q_filter | clause

    if q_filter is None:
        return None

    from .models import ProjectMembership
    return ProjectMembership.objects.filter(
        q_filter,
        project=project,
        is_active=True,
    ).first()


def _org_role(user, org_id):
    if user is None or org_id is None:
        return None
    from .authentication import merge_live_membership
    return merge_live_membership(user, org_id)


def _is_org_member(user, org_id):
    if _org_role(user, org_id):
        return True
    try:
        from .services.admin_service import admin_service
        return admin_service.is_org_member(
            user_id=getattr(user, 'id', None),
            org_id=org_id,
            email=getattr(user, 'email', None),
        )
    except Exception:
        return False


def can_comment_on_item(user, item) -> bool:
    """Canonical item-comment authorization predicate (PLS-398).

    Keep comment list/create aligned with item read/link/assign permissions: the
    same actor who is allowed to handle an item must be able to publish the
    normal Pulse handoff comment, including projectless operational alerts.
    Least-privilege grants are additive and explicit.
    """
    if user is None or item is None:
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True

    # Item-scoped grants first: participants and routed-team members can always
    # comment on their own/team-routed work, even when the item is projectless or
    # the project is private and they are not ProjectMembership rows.
    if is_item_participant(user, item) or is_item_team_member(user, item):
        return True

    # On an armed routing-binding lane, neither a legacy ProjectMembership nor
    # open-project membership may resurrect participant-as-authorization.
    if routing_subject_can_access_item(user, item) is not None:
        return False

    project = getattr(item, 'project', None)

    # Projectless operational alerts are handoff artifacts, not personal
    # workspace notes. The allowed alert handlers are participants and current
    # assignment_group members (handled above); unrelated users remain denied.
    if project is None:
        return False

    # Org owner/admin for the item's project org. Use the JWT claim when present
    # (supports int or string keys) and fall back to the legacy helper.
    role = _org_role(user, getattr(project, 'org_id', None))
    if role in ('owner', 'admin') or is_org_admin_or_owner(user, org_id=getattr(project, 'org_id', None)):
        return True

    # Explicit project membership grants comment access for private/limited
    # projects; match by user id OR email alias to support agent identities.
    if _active_project_membership(user, project):
        return True

    # Open projects are visible/commentable to org members.
    if getattr(project, 'access_level', None) == 'open' and _is_org_member(user, getattr(project, 'org_id', None)):
        return True

    return False


class ServiceAccessPermission(permissions.BasePermission):
    SERVICE_SLUG = 'pulse'
    message = 'You do not have access to this service.'

    def has_permission(self, request, view):
        user = request.user
        if not hasattr(user, 'enabled_services'):
            return True  # Not a federated user (e.g., Django admin)
        if user.enabled_services is None:
            return True  # Unrestricted (staff/superuser)
        return self.SERVICE_SLUG in user.enabled_services


class IsOwnerOrStaff(permissions.BasePermission):
    """
    Object-level permission to only allow owners or staff to access objects.

    For Items (tasks/actions/alerts):
    - Owner = assignee_id matches user OR created_by_id matches user
    - Staff can access any object

    For NotificationChannels:
    - Owner = user_id matches the authenticated user
    - Staff can access any object
    """

    def has_object_permission(self, request, view, obj):
        # Staff can do anything
        if request.user.is_staff:
            return True

        # Org admins/owners — scoped to the object's org when we can resolve it
        item_org_id = getattr(getattr(obj, 'project', None), 'org_id', None)
        if is_org_admin_or_owner(request.user, org_id=item_org_id):
            return True

        routing_access = routing_subject_can_access_item(request.user, obj)
        if routing_access is not None:
            return routing_access

        # Check ownership based on object type
        if hasattr(obj, 'assignee_id') and hasattr(obj, 'created_by_id'):
            # This is an Item (task/action/alert)
            return (
                obj.assignee_id == request.user.id or
                obj.created_by_id == request.user.id
            )
        elif hasattr(obj, 'user_id'):
            # This is a UserNotificationChannel or similar user-owned object
            return obj.user_id == request.user.id

        # Default deny for unknown object types
        return False


class IsOwnerOrStaffForItems(permissions.BasePermission):
    """
    Specific permission class for Item objects.
    Users can only access items they own (assigned to OR created by).
    Staff can access all items.
    """

    def has_object_permission(self, request, view, obj):
        # Staff can do anything
        if request.user.is_staff:
            return True

        # Org admins/owners — scoped to the item's org
        item_org_id = getattr(getattr(obj, 'project', None), 'org_id', None)
        if is_org_admin_or_owner(request.user, org_id=item_org_id):
            return True

        routing_access = routing_subject_can_access_item(request.user, obj)
        if routing_access is not None:
            return routing_access

        # User must be assignee or creator
        return (
            obj.assignee_id == request.user.id or
            obj.created_by_id == request.user.id
        )


class IsOwnerOrStaffForChannels(permissions.BasePermission):
    """
    Specific permission class for UserNotificationChannel objects.
    Users can only access their own notification channels.
    Staff can access all channels.
    """

    def has_object_permission(self, request, view, obj):
        # Staff can do anything
        if request.user.is_staff:
            return True

        # User must own the channel
        return obj.user_id == request.user.id


class IsStaffOrReadOnly(permissions.BasePermission):
    """
    Permission class that allows read-only access to all authenticated users,
    but only staff can modify.
    """

    def has_permission(self, request, view):
        # Read permissions are allowed for any authenticated user
        if request.method in permissions.SAFE_METHODS:
            return True

        # Org admins/owners can write
        if is_org_admin_or_owner(request.user):
            return True

        # Write permissions only for staff
        return request.user.is_staff

    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed for any authenticated user
        if request.method in permissions.SAFE_METHODS:
            return True

        # Org admins/owners can write
        if is_org_admin_or_owner(request.user):
            return True

        # Write permissions only for staff
        return request.user.is_staff


# =============================================================================
# Organization and Project Permission Classes
# Note: Organizations are managed by shizuha-admin. Permission checks use AdminService.
# =============================================================================

class ProjectPermission(permissions.BasePermission):
    """
    Permission for project-level access based on membership and access level.

    Access levels:
    - open: All org members can access
    - limited: Only project members can create/edit
    - private: Only project members can view

    Organization owners/admins have full access to all projects in their org.
    Organization membership is checked via shizuha-admin's AdminService.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True

        user_id = request.user.id
        user_email = getattr(request.user, 'email', None)

        from .models import ProjectMembership

        # Resolve org role. Prefer the JWT claim (zero-cost read); fall back
        # to admin-service only when the claim is empty (legacy tokens).
        memberships = getattr(request.user, 'organization_memberships', None)
        org_role = _org_role(request.user, obj.org_id)
        if org_role is None and memberships is None:
            from .services.admin_service import admin_service
            membership = admin_service.check_org_membership(user_id, obj.org_id, user_email)
            if not membership or not membership.get('is_member'):
                return False
            org_role = membership.get('role')

        if org_role is None:
            return False  # not an org member at all

        # Organization owners and admins have full access to all projects
        if org_role in ('owner', 'admin'):
            return True

        # Check project access level
        if obj.access_level == 'open':
            # All org members can read
            if request.method in permissions.SAFE_METHODS:
                return True

        # Check project membership for limited/private projects (by user_id OR email)
        from django.db.models import Q
        pm_filter = Q()
        try:
            int_id = int(user_id)
            pm_filter = Q(user_id=int_id)
        except (ValueError, TypeError):
            pass
        if user_email:
            pm_filter = pm_filter | Q(user_email=user_email)

        project_membership = ProjectMembership.objects.filter(
            pm_filter,
            project=obj,
            is_active=True
        ).first() if pm_filter else None

        if obj.access_level == 'private' and not project_membership:
            return False

        if obj.access_level == 'limited':
            # Limited: project members only for non-safe methods
            if request.method not in permissions.SAFE_METHODS and not project_membership:
                return False

        # Check specific permissions based on action
        if request.method in permissions.SAFE_METHODS:
            return project_membership.can_browse if project_membership else True
        elif request.method == 'DELETE':
            return project_membership and project_membership.can_delete
        elif request.method in ['PUT', 'PATCH']:
            return project_membership and (project_membership.can_edit or project_membership.can_manage_project)
        elif request.method == 'POST':
            return project_membership and project_membership.can_create

        return False


class ItemProjectPermission(permissions.BasePermission):
    """
    Permission for item access that considers project membership.

    Access rules:
    1. Personal items (no project): Owner only (existing behavior)
    2. Project items: Based on project membership and permissions
    3. Staff: Full access

    Organization membership is checked via shizuha-admin's AdminService.

    Note on POST in has_object_permission:
    has_object_permission is only called for detail-level actions (via get_object()).
    POST at the detail level means a custom action (assign, transition, acknowledge, etc.)
    which are all edit operations on existing items — NOT item creation.
    Item creation goes through has_permission (list-level) and never reaches here.
    """

    def _get_project_membership(self, project, user_id, user_email):
        """Look up ProjectMembership by user_id or user_email."""
        from .models import ProjectMembership
        from django.db.models import Q

        q_filter = None
        try:
            int_id = int(user_id)
            q_filter = Q(user_id=int_id)
        except (ValueError, TypeError):
            pass

        if user_email:
            for variant in _email_variants(user_email):
                clause = Q(user_email__iexact=variant)
                q_filter = clause if q_filter is None else q_filter | clause

        if not q_filter:
            return None

        return ProjectMembership.objects.filter(
            q_filter,
            project=project,
            is_active=True
        ).first()

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True

        # Org admins get full access — scoped to the item's org
        item_org_id = getattr(getattr(obj, 'project', None), 'org_id', None)
        if is_org_admin_or_owner(request.user, org_id=item_org_id):
            return True

        user_id = request.user.id
        user_email = getattr(request.user, 'email', None)

        # Participants use the shared email-primary predicate and can ALWAYS
        # access their own item regardless of project membership. This MUST
        # come before any project/org membership gate.
        if is_item_participant(request.user, obj):
            return True

        # Personal item (no project): only participants (handled above).
        if not obj.project:
            return False

        # Project item — org membership. The JWT map is checked first; a miss
        # confirms against Admin once so an org created in this session works.
        # An empty map keeps the admin-service fallback used by tokens that
        # never carried the claim.
        project = obj.project
        if _org_role(request.user, project.org_id) is None:
            memberships = getattr(request.user, 'organization_memberships', None)
            if isinstance(memberships, dict) and memberships:
                return False
            from .services.admin_service import admin_service
            if not admin_service.is_org_member(user_id, project.org_id, user_email):
                return False

        # Open projects: all org members can read
        if project.access_level == 'open' and request.method in permissions.SAFE_METHODS:
            return True

        # Check project membership (by user_id OR user_email)
        project_membership = self._get_project_membership(project, user_id, user_email)

        # Private/limited projects require membership
        if not project_membership and project.access_level in ['private', 'limited']:
            return False

        # PLS-80: deleting a LINK is an EDIT on the item's relationships, not item
        # destruction. Permission it symmetrically with link CREATION (the POST
        # branch below) / assign / transition, so an authorized editor who can ADD a
        # blocker link can also REMOVE a stale one. Without this, the generic DELETE
        # branch applies item-DELETION rules (creator/assignee/can_delete only) and
        # 403s a non-creator/assignee editor even in an open project — exactly the
        # PLAT-356<->ID-1 stale-blocker trap this tool exists to clear.
        if getattr(view, 'action', None) == 'delete_link':
            if project.access_level == 'open':
                return True
            return project_membership and project_membership.can_edit

        # Check specific permissions
        if request.method in permissions.SAFE_METHODS:
            return True  # All project members can browse

        elif request.method == 'DELETE':
            # Can delete own items (created or assigned) or if has delete permission
            return project_membership and project_membership.can_delete

        elif request.method in ['PUT', 'PATCH', 'POST']:
            # POST here = custom detail action (assign, transition, etc.) = edit operation.
            # For open projects, all org members can edit (they passed org check above).
            if project.access_level == 'open':
                return True
            # Can edit own items or if has edit permission
            return project_membership and project_membership.can_edit

        return False
