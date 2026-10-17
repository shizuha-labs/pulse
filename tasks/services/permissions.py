"""
Permission service for project-based access control.

This service provides helper methods for checking user permissions
across organizations and projects, with granular project-level permissions.

Note: Organizations are now managed by shizuha-admin. This service uses
AdminService for organization membership checks.
"""
from django.db.models import Q
import os
from ..models import Project, ProjectMembership, Item


class ProjectMembershipUnavailable(Exception):
    """The authoritative org-membership source could not be reached.

    Raised by ``PermissionService.identity_belongs_to_organization_strict`` when
    shizuha-admin cannot answer, so a caller serving an authorization-relevant
    projection can fail closed instead of serving a short answer as if it were
    authoritative (PLS-728).
    """


def _is_valid_int_id(user_id):
    """
    Check if user_id can be used in database queries as an integer.

    FederatedUsers from agents have UUID user_ids which cannot be used
    in database queries for integer fields.

    Returns:
        tuple: (is_valid, int_id) - is_valid is True if usable, int_id is the converted value
    """
    if user_id is None:
        return (False, None)
    try:
        return (True, int(user_id))
    except (ValueError, TypeError):
        return (False, None)


def _parse_id_list(raw):
    """Parse a single ID, a comma-separated string, or an iterable of IDs into a list of ints.

    Returns an empty list when the input is falsy / fully non-numeric, so call
    sites can use a simple `if ids:` check to decide whether to apply a filter.
    """
    if raw is None or raw == '' or raw == []:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw).split(',')
    out = []
    for p in parts:
        try:
            out.append(int(str(p).strip()))
        except (ValueError, TypeError):
            continue
    return out


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


def _email_variant_q(fields, email):
    """Build a case-insensitive OR query for any field matching any alias."""
    q = None
    for variant in _email_variants(email):
        for field in fields:
            clause = Q(**{f'{field}__iexact': variant})
            q = clause if q is None else q | clause
    return q


def _blank_email_q(field):
    """Return the legacy-fallback predicate for one participant email slot."""
    return Q(**{f'{field}__isnull': True}) | Q(**{field: ''})


def _participant_slot_q(email_field, id_field, user_email, int_id=None, is_valid_id=False):
    """Build an email-primary queryset predicate for one participant slot."""
    q = _email_variant_q((email_field,), user_email)
    if is_valid_id:
        fallback = _blank_email_q(email_field) & Q(**{id_field: int_id})
        q = fallback if q is None else q | fallback
    return q


def _participant_slot_matches(stored_email, stored_id, user_email, user_id):
    """Object-level twin of ``_participant_slot_q`` for one slot."""
    canonical_email = (stored_email or '').strip().lower()
    if canonical_email:
        return canonical_email in set(_email_variants(user_email))
    return user_id is not None and stored_id == user_id


def _item_participant_matches(user_id, item, user_email=None):
    """Return whether assignee or reporter matches the email-primary contract."""
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


def _normalize_memberships(raw):
    """Normalize a JWT-style organization membership mapping to integer IDs."""
    if not isinstance(raw, dict):
        return {}
    normalized = {}
    for org_id, role in raw.items():
        try:
            normalized[int(org_id)] = str(role).strip().lower()
        except (TypeError, ValueError):
            continue
    return normalized


def _org_member_identity_q(members):
    """Build a ProjectMembership identity predicate from exact-org members."""
    user_ids = set()
    emails = set()
    for member in members or []:
        if not isinstance(member, dict):
            continue
        raw_user_id = member.get('user_id') or member.get('id')
        try:
            user_ids.add(int(raw_user_id))
        except (TypeError, ValueError):
            pass
        raw_email = member.get('user_email') or member.get('email')
        emails.update(_email_variants(raw_email))

    identity_q = Q(user_id__in=user_ids) if user_ids else None
    for email in sorted(emails):
        clause = Q(user_email__iexact=email)
        identity_q = clause if identity_q is None else identity_q | clause
    return identity_q


def _team_roster_bindings_for_email(user_email):
    """Return exact ``(org_id, canonical_team)`` bindings for an agent email."""
    aliases = set(_email_variants(user_email))
    if not aliases:
        return set()

    from ..models import TeamRoster
    bindings = set()
    for org_id, team_name, tiers in TeamRoster.objects.filter(
        is_active=True,
    ).values_list('organization_id', 'team_name', 'tiers'):
        matched = False
        for tier in tiers if isinstance(tiers, list) else []:
            members = tier if isinstance(tier, list) else [tier]
            if any(aliases & set(_email_variants(email)) for email in members):
                matched = True
                break
        if matched:
            bindings.add((org_id, str(team_name or '').strip().lower()))
    return bindings


def _armed_routing_q():
    """SQL predicate for project-backed lanes controlled by ADR-0041."""
    from .routing_authority import binding_team_enabled
    from ..models import RoutingTeam
    configured = {
        value.strip().lower().replace('_', '-').replace(' ', '-')
        for value in os.environ.get(
            'PULSE_ROUTING_BINDINGS_TEAMS', '').split(',') if value.strip()
    }
    pairs = list(RoutingTeam.objects.values_list('organization_id', 'key'))
    q = None
    for org_id, key in pairs:
        # Sticky containment: a materialized exact authority remains protected
        # when the candidate gate is switched off for rollback.
        clause = Q(project__org_id=org_id, assignment_group__iexact=key)
        q = clause if q is None else q | clause
    if any(binding_team_enabled(key) for key in configured - {'*'}):
        # Explicitly armed names fail closed even if their exact team row is absent.
        for key in configured - {'*'}:
            clause = Q(project__isnull=False, assignment_group__iexact=key)
            q = clause if q is None else q | clause
    return q


def _authorized_binding_item_ids(user, queryset):
    from .routing_authority import subject_authorized_for_item
    is_valid, uid = _is_valid_int_id(getattr(user, 'id', None))
    if not is_valid:
        return []
    owner_q = _participant_slot_q(
        'assignee_email',
        'assignee_id',
        getattr(user, 'email', None),
        uid,
        True,
    )
    if owner_q is None:
        return []
    allowed = []
    for item in queryset.filter(owner_q).select_related('project'):
        decision = subject_authorized_for_item(
            uid, item, team_name=item.assignment_group,
        )
        if decision.allowed:
            allowed.append(item.id)
    return allowed


class PermissionService:
    """
    Service for checking and managing permissions across organizations and projects.

    Organization membership is checked via shizuha-admin's AdminService.
    """

    @staticmethod
    def identity_belongs_to_organization(user_id, org_id, user_email=None):
        """Return whether an identity is represented in one exact organization.

        Human membership is authoritative in shizuha-admin. Agent identities
        may instead be represented by an active, organization-owned TeamRoster
        row; that projection is intentionally scoped to the row's org and does
        not make the identity a member of any similarly named team elsewhere.
        """
        from .admin_service import admin_service

        is_valid, int_id = _is_valid_int_id(user_id)
        if is_valid and admin_service.is_org_member(int_id, org_id, user_email):
            return True

        aliases = set(_email_variants(user_email))
        if not aliases:
            return False

        from ..models import TeamRoster
        for tiers in TeamRoster.objects.filter(
            organization_id=org_id,
            is_active=True,
        ).values_list('tiers', flat=True):
            for tier in tiers if isinstance(tiers, list) else []:
                members = tier if isinstance(tier, list) else [tier]
                for email in members:
                    if aliases & set(_email_variants(email)):
                        return True
        return False

    @staticmethod
    def identity_belongs_to_organization_strict(user_id, org_id, user_email=None):
        """Like ``identity_belongs_to_organization`` but fails closed on unknown.

        Raises :class:`ProjectMembershipUnavailable` when the authoritative
        org-membership source (shizuha-admin) cannot answer, instead of treating
        "unknown" as "not a member". Used by the internal project-memberships
        projection (PLS-728), where serving a short answer for an unknown org
        membership would mint an authorization claim that silently drops grants.
        """
        from .admin_service import admin_service, AdminServiceUnavailable

        is_valid, int_id = _is_valid_int_id(user_id)
        if is_valid:
            try:
                if admin_service.is_org_member_strict(int_id, org_id, user_email):
                    return True
            except AdminServiceUnavailable as exc:
                raise ProjectMembershipUnavailable(str(exc)) from exc
            # Authoritative "not a member" from admin — fall through to the
            # org-owned TeamRoster projection for agent identities.

        aliases = set(_email_variants(user_email))
        if not aliases:
            return False

        from ..models import TeamRoster
        for tiers in TeamRoster.objects.filter(
            organization_id=org_id,
            is_active=True,
        ).values_list('tiers', flat=True):
            for tier in tiers if isinstance(tiers, list) else []:
                members = tier if isinstance(tier, list) else [tier]
                for email in members:
                    if aliases & set(_email_variants(email)):
                        return True
        return False

    @staticmethod
    def get_user_organizations(user_id, user_email=None):
        """
        Get all organizations a user belongs to.

        Args:
            user_id: The user's ID (may be integer or UUID string)
            user_email: Optional user email to also match by email

        Returns:
            List of organization dicts (from shizuha-admin)
        """
        is_valid, int_id = _is_valid_int_id(user_id)
        if not is_valid:
            return []

        from .admin_service import admin_service
        return admin_service.get_user_organizations(int_id, user_email)

    @staticmethod
    def get_user_org_ids(user_id, user_email=None):
        """
        Get IDs of all organizations a user belongs to.

        Args:
            user_id: The user's ID
            user_email: Optional user email

        Returns:
            List of organization IDs
        """
        is_valid, int_id = _is_valid_int_id(user_id)
        if not is_valid:
            return []

        from .admin_service import admin_service
        return admin_service.get_user_organization_ids(int_id, user_email)

    @staticmethod
    def get_user_org_membership(user_id, org_id, user_email=None):
        """
        Get user's membership in an organization.

        Args:
            user_id: The user's ID (may be integer or UUID string)
            org_id: Organization ID
            user_email: Optional user email to also match by email

        Returns:
            Membership dict from shizuha-admin or None
        """
        is_valid, int_id = _is_valid_int_id(user_id)
        if not is_valid:
            return None

        from .admin_service import admin_service
        return admin_service.check_org_membership(int_id, org_id, user_email)

    @staticmethod
    def get_user_projects(user_id=None, organization_id=None, user_email=None, user=None):
        """
        Layered, additive project visibility — mirrors the item resolver so
        project-scoped surfaces (ProjectViewSet, project pickers, per-project
        task boards) see the same tenant-scoped permissions as items.

        Effective visibility = union of:
            L0 System   : is_superuser                         → all projects
            L1 Org      : user is owner/admin of project.org_id → all projects in those orgs
            L2 Project  : explicit ProjectMembership           → those projects
            L2b Org     : user is any member of project.org_id
                          AND project.access_level == 'open'   → baseline visibility
            (L3/L4 don't apply to projects — they're per-item concepts.)

        Two call forms:
            get_user_projects(user=<FederatedUser|User>, ...)
                preferred — reads .organization_memberships from the user
                object directly, no admin-service round-trip per call
            get_user_projects(user_id=..., user_email=...)
                legacy — falls back to admin-service for memberships
        """
        # Resolve user identity + org memberships
        if user is not None:
            user_id = user.id
            user_email = getattr(user, 'email', None) or user_email
            is_superuser = getattr(user, 'is_superuser', False)
            is_staff = getattr(user, 'is_staff', False)
            has_membership_claim = hasattr(user, 'organization_memberships')
            memberships = _normalize_memberships(
                getattr(user, 'organization_memberships', None)
            )
        else:
            is_superuser = False
            is_staff = False
            has_membership_claim = False
            memberships = {}

        is_valid, int_id = _is_valid_int_id(user_id)

        # L0: superuser → all active projects
        if is_superuser or is_staff:
            qs = Project.objects.filter(is_active=True)
            if organization_id:
                qs = qs.filter(org_id=organization_id)
            return qs

        # Determine member/admin orgs. Prefer the claim on `user`; if only
        # id/email was passed (legacy path), fall back to admin-service.
        if has_membership_claim:
            member_org_ids = list(memberships.keys())
            admin_org_ids = [oid for oid, role in memberships.items()
                             if role in ('owner', 'admin')]
        else:
            from .admin_service import admin_service
            if is_valid:
                member_org_ids = admin_service.get_user_organization_ids(int_id, user_email)
            else:
                member_org_ids = []
            # Legacy path can't distinguish admin from member — treat all as member
            # (safer: no accidental admin elevation from a call that can't verify).
            admin_org_ids = []

        if organization_id is not None:
            try:
                organization_id = int(organization_id)
            except (TypeError, ValueError):
                return Project.objects.none()
            if organization_id not in member_org_ids:
                return Project.objects.none()
            member_org_ids = [organization_id]
            admin_org_ids = [oid for oid in admin_org_ids if oid == organization_id]

        clauses = []

        # L1: org admin/owner → every project in those orgs
        if admin_org_ids:
            clauses.append(Q(org_id__in=admin_org_ids))

        # L2: explicit project membership
        email_q = _email_variant_q(('user_email',), user_email)
        if is_valid:
            pm_filter = Q(user_id=int_id)
            if email_q is not None:
                pm_filter |= email_q
        elif email_q is not None:
            pm_filter = email_q
        else:
            pm_filter = None

        if pm_filter is not None and member_org_ids:
            direct_project_ids = list(ProjectMembership.objects.filter(
                pm_filter, is_active=True,
                project__org_id__in=member_org_ids,
            ).values_list('project_id', flat=True))
            if direct_project_ids:
                clauses.append(Q(id__in=direct_project_ids))

        # L2b: org member + access_level='open'
        if member_org_ids:
            clauses.append(Q(org_id__in=member_org_ids) & Q(access_level='open'))

        if not clauses:
            return Project.objects.none()

        combined = clauses[0]
        for c in clauses[1:]:
            combined = combined | c

        return Project.objects.filter(combined, is_active=True).distinct()

    @staticmethod
    def get_valid_project_memberships(project, queryset=None):
        """Return active memberships whose identity belongs to ``project.org_id``.

        ProjectMembership is a project-local role projection, never an
        organization-membership grant. Historical rows can outlive an Admin org
        membership; those rows stay inert and hidden until a lifecycle
        reconciler retires them.
        """
        memberships = queryset
        if memberships is None:
            memberships = ProjectMembership.objects.filter(project=project)
        memberships = memberships.filter(project=project, is_active=True)

        from .admin_service import admin_service
        org_members = list(admin_service.get_org_members(project.org_id) or [])
        from ..user_service import user_service
        agent_identities = user_service.get_agent_identity_index()
        exact_members = []
        if agent_identities is not None:
            agent_user_ids = agent_identities.get('user_ids') or set()
            agent_emails = agent_identities.get('emails') or set()
            for member in org_members:
                if not isinstance(member, dict) or member.get('is_pending'):
                    continue
                try:
                    member_is_agent = int(
                        member.get('user_id') or member.get('id')
                    ) in agent_user_ids
                except (TypeError, ValueError):
                    member_is_agent = False
                member_aliases = set(_email_variants(
                    member.get('user_email') or member.get('email')
                ))
                member_is_agent = (
                    member_is_agent
                    or bool(member_aliases & agent_emails)
                )
                if not member_is_agent:
                    exact_members.append(member)

        from ..models import TeamRoster
        for tiers in TeamRoster.objects.filter(
            organization_id=project.org_id,
            is_active=True,
        ).values_list('tiers', flat=True):
            for tier in tiers if isinstance(tiers, list) else []:
                for email in tier if isinstance(tier, list) else [tier]:
                    exact_members.append({'user_email': email})
        identity_q = _org_member_identity_q(
            exact_members
        )
        if identity_q is None:
            return memberships.none()
        return memberships.filter(identity_q)

    # PLS-832: role rank for collapsing duplicate project membership edges.
    _MEMBERSHIP_ROLE_PRIORITY = {
        'owner': 4,
        'admin': 3,
        'member': 2,
        'viewer': 1,
    }

    @classmethod
    def _prefer_project_membership(cls, left, right):
        """Pick the stronger of two membership rows for the same identity."""
        lp = cls._MEMBERSHIP_ROLE_PRIORITY.get(getattr(left, 'role', ''), 0)
        rp = cls._MEMBERSHIP_ROLE_PRIORITY.get(getattr(right, 'role', ''), 0)
        if lp != rp:
            return left if lp > rp else right
        # Prefer a resolved user_id over email-only.
        left_uid = getattr(left, 'user_id', None) is not None
        right_uid = getattr(right, 'user_id', None) is not None
        if left_uid != right_uid:
            return left if left_uid else right
        left_ts = getattr(left, 'updated_at', None) or getattr(left, 'created_at', None)
        right_ts = getattr(right, 'updated_at', None) or getattr(right, 'created_at', None)
        if left_ts and right_ts and left_ts != right_ts:
            return left if left_ts >= right_ts else right
        return left

    @classmethod
    def dedupe_project_memberships(cls, memberships):
        """Collapse duplicate ProjectMembership rows to one identity per project.

        PLS-832: unique_together is only (project, user_id), so the same person
        can appear twice via (user_id=X, email=E) + (user_id=NULL, email=E) or
        two active rows sharing an email alias. List/count/authz consumers must
        see a single deterministic effective role per identity.
        """
        rows = list(memberships)
        if len(rows) <= 1:
            return rows

        parent = list(range(len(rows)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        uid_first: dict = {}
        email_first: dict = {}
        for i, row in enumerate(rows):
            uid = getattr(row, 'user_id', None)
            if uid is not None:
                if uid in uid_first:
                    union(i, uid_first[uid])
                else:
                    uid_first[uid] = i
            for variant in _email_variants(getattr(row, 'user_email', None)):
                if variant in email_first:
                    union(i, email_first[variant])
                else:
                    email_first[variant] = i

        groups: dict = {}
        for i, row in enumerate(rows):
            groups.setdefault(find(i), []).append(row)

        deduped = []
        for cluster in groups.values():
            best = cluster[0]
            for other in cluster[1:]:
                best = cls._prefer_project_membership(best, other)
            deduped.append(best)

        deduped.sort(
            key=lambda m: (
                (getattr(m, 'user_email', None) or '').lower(),
                getattr(m, 'user_id', None) or 0,
                getattr(m, 'id', 0) or 0,
            )
        )
        return deduped

    @staticmethod
    def get_user_project_membership(user_id, project, user_email=None):
        """
        Get user's membership in a project.

        Args:
            user_id: The user's ID (may be integer or UUID string)
            project: Project instance or ID
            user_email: Optional user email to also match by email

        Returns:
            ProjectMembership or None
        """
        project_id = project.id if hasattr(project, 'id') else project
        is_valid, int_id = _is_valid_int_id(user_id)

        email_q = _email_variant_q(('user_email',), user_email)
        if is_valid:
            q_filter = Q(user_id=int_id)
            if email_q is not None:
                q_filter |= email_q
        elif email_q is not None:
            q_filter = email_q
        else:
            return None

        return ProjectMembership.objects.filter(
            q_filter,
            project_id=project_id,
            is_active=True
        ).first()

    @staticmethod
    def can_access_organization(user_id, org_id, user_email=None):
        """
        Check if user can access an organization.

        Args:
            user_id: The user's ID (may be integer or UUID string)
            org_id: Organization ID
            user_email: Optional user email to also match by email

        Returns:
            bool
        """
        is_valid, int_id = _is_valid_int_id(user_id)
        if not is_valid:
            return False

        from .admin_service import admin_service
        return admin_service.is_org_member(int_id, org_id, user_email)

    @staticmethod
    def can_manage_organization(user_id, org_id, user_email=None):
        """
        Check if user can manage an organization (owner or admin).

        Args:
            user_id: The user's ID
            org_id: Organization ID
            user_email: Optional user email

        Returns:
            bool
        """
        is_valid, int_id = _is_valid_int_id(user_id)
        if not is_valid:
            return False

        from .admin_service import admin_service
        return admin_service.can_manage_org(int_id, org_id, user_email)

    @staticmethod
    def can_access_project(user_id, project, permission='browse', user_email=None, user=None):
        """
        Check if user can perform action on project.

        Args:
            user_id: The user's ID
            project: Project instance
            permission: One of 'browse', 'create', 'edit', 'delete',
                       'manage_project', 'manage_members'
            user_email: Optional user email
            user: Optional authenticated user object. Preferred for federated
                  users/agents because their JWT carries organization_memberships
                  and their user_id may be a UUID that is not queryable as a
                  local integer ID.

        Returns:
            bool
        """
        if user is not None:
            user_id = getattr(user, 'id', user_id)
            user_email = getattr(user, 'email', None) or user_email
            if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
                return True
            if hasattr(user, 'organization_memberships'):
                org_role = _normalize_memberships(
                    getattr(user, 'organization_memberships', None)
                ).get(int(project.org_id))
            else:
                from .admin_service import admin_service
                live_membership = admin_service.check_org_membership(
                    user_id, project.org_id, user_email,
                )
                org_role = (
                    str(live_membership.get('role') or 'member').strip().lower()
                    if live_membership and live_membership.get('is_member')
                    else None
                )
            if not org_role:
                return False
            if org_role in ('owner', 'admin'):
                return True
        else:
            # First check organization membership via shizuha-admin. Legacy callers
            # only pass an integer auth-service id; federated users/agents should
            # pass `user=` so UUID ids do not lose their JWT org-membership grants.
            if not PermissionService.can_access_organization(user_id, project.org_id, user_email):
                return False

        # Open projects: all org members can browse, create, and edit
        if project.access_level == 'open' and permission in ('browse', 'create', 'edit'):
            return True

        # Check project membership
        membership = PermissionService.get_user_project_membership(user_id, project, user_email)

        if not membership:
            # For open projects, org members can browse/create/edit without explicit membership
            if project.access_level == 'open':
                return permission in ('browse', 'create', 'edit')
            return False

        # Check specific permission
        permission_map = {
            'browse': membership.can_browse,
            'create': membership.can_create,
            'edit': membership.can_edit,
            'delete': membership.can_delete,
            'manage_project': membership.can_manage_project,
            'manage_members': membership.can_manage_members,
        }

        return permission_map.get(permission, False)

    @staticmethod
    def get_user_item_queryset(user, include_personal=True, project_id=None, organization_id=None):
        """
        Layered, additive permission resolver for item visibility.

        Effective visibility = union of the grants that apply to the user,
        modelled after GitLab / GitHub (inheritance from org down to
        project/item, most-permissive-wins, no subtractive "deny" layers):

            L0 System   : is_superuser                       → all items
            L1 Org      : user is owner/admin of item.project.org_id
                                                             → all items in those orgs
            L2 Project  : explicit ProjectMembership         → items in those projects
            L2b Org     : user is any member of item.project.org_id
                         AND project.access_level == 'open'  → baseline org visibility
            L3 Item     : assignee OR creator                → always visible to owner
            L4 Personal : item has no project AND user owns it → private workspace

        Grants are additive. If a user is admin in org A and member in org B,
        they see everything in A but only their assigned + open-project items
        in B.

        The org-membership truth comes from `user.organization_memberships`
        — a {org_id: role} dict populated from the JWT's
        `organization_memberships` claim by `tasks/authentication.py`. The
        claim is refreshed at token-mint time; memberships changes propagate
        on next refresh (standard multi-tenant JWT tradeoff).

        Args:
            user: Django user or FederatedUser. Must carry `.organization_memberships`.
            include_personal: Include personal items (no project)
            project_id: Filter to specific project (int or comma-separated list)
            organization_id: Filter to specific organization

        Returns:
            QuerySet of Item objects
        """
        project_ids = _parse_id_list(project_id)

        # L0: superuser — cross-tenant visibility
        if getattr(user, 'is_superuser', False):
            queryset = Item.objects.all()
            if not include_personal:
                queryset = queryset.filter(project__isnull=False)
            if project_ids:
                queryset = queryset.filter(project_id__in=project_ids)
            if organization_id:
                queryset = queryset.filter(project__org_id=organization_id)
            # No join-based grants on this path — DISTINCT is pure cost and was
            # a major contributor to list/COUNT pile-ups under home-dashboard
            # polling (SELECT DISTINCT on every task column + COUNT wrapper).
            return queryset

        user_id = user.id
        user_email = getattr(user, 'email', None)
        is_valid, int_id = _is_valid_int_id(user_id)
        has_membership_claim = hasattr(user, 'organization_memberships')
        memberships = _normalize_memberships(
            getattr(user, 'organization_memberships', None)
        )
        if not has_membership_claim:
            from .admin_service import admin_service
            member_org_ids = admin_service.get_user_organization_ids(
                int_id, user_email,
            ) if is_valid else []
            memberships = {
                int(org_id): 'member'
                for org_id in member_org_ids
                if str(org_id).isdigit()
            }
        admin_org_ids = [oid for oid, role in memberships.items()
                         if role in ('owner', 'admin')]
        member_org_ids = list(memberships.keys())

        # L3 identity match. Project-owned items are only visible through this
        # grant inside an organization the user belongs to; projectless items
        # remain private participant work.
        owner_q = None
        for slot_q in (
            _participant_slot_q(
                'assignee_email', 'assignee_id', user_email, int_id, is_valid,
            ),
            _participant_slot_q(
                'created_by_email', 'created_by_id', user_email, int_id, is_valid,
            ),
        ):
            if slot_q is not None:
                owner_q = slot_q if owner_q is None else owner_q | slot_q
        if owner_q is None:
            # No way to identify this user, and they're not a superuser — deny.
            return Item.objects.none()

        # Staff parity: carry the existing escape hatch. Staff effectively act
        # like org admins across every org they can reach, but still respect
        # the `organization_id` / `project_id` narrowing filters applied below.
        if getattr(user, 'is_staff', False):
            base_q = Q()
        else:
            # Build the additive union.
            clauses = []

            # L3: assignee/reporter inside one of the user's organizations.
            if member_org_ids:
                clauses.append(
                    Q(project__org_id__in=member_org_ids) & owner_q
                )

            # L3b: an active TeamRoster is an organization-and-team-scoped
            # agent entitlement. It can expose that exact team's queue, never
            # a similarly named team or project role in another organization.
            for org_id, team_name in _team_roster_bindings_for_email(user_email):
                clauses.append(
                    Q(project__org_id=org_id)
                    & Q(assignment_group__iexact=team_name)
                )

            # L1: org owner/admin → every item in those orgs
            if admin_org_ids:
                clauses.append(Q(project__org_id__in=admin_org_ids))

            # L2: explicit project memberships — pass the user object so the
            # project resolver applies the same layered model (avoids silent
            # member-only fallback when the caller is actually an org admin).
            accessible_projects = PermissionService.get_user_projects(user=user)
            if is_valid or user_email:
                clauses.append(Q(project__in=accessible_projects))

            # L2b: baseline org member → items in OPEN projects of those orgs
            if member_org_ids:
                clauses.append(
                    Q(project__org_id__in=member_org_ids) & Q(project__access_level='open')
                )

            # L4: personal items (no project, user is owner)
            if include_personal:
                clauses.append(Q(project__isnull=True) & owner_q)

            # Exact routing-authority receipts are already tenant-bound to one
            # task + one organization Team. They remain a valid item-local
            # grant even when the agent is not represented as a human Admin org
            # member; a legacy ProjectMembership cannot create this grant.
            armed_q = _armed_routing_q()
            if armed_q is not None:
                receipt_item_ids = _authorized_binding_item_ids(
                    user,
                    Item.objects.filter(armed_q),
                )
                if receipt_item_ids:
                    clauses.append(Q(id__in=receipt_item_ids))

            if not clauses:
                return Item.objects.none()

            # Fold the clauses with OR.
            base_q = clauses[0]
            for c in clauses[1:]:
                base_q = base_q | c

        queryset = Item.objects.filter(base_q) if base_q else Item.objects.all()

        # Narrowing filters — applied AFTER the union so that passing
        # `organization_id=X` effectively restricts visibility to "what you
        # can see within org X" rather than granting cross-org access.
        if not include_personal:
            queryset = queryset.filter(project__isnull=False)
        if project_ids:
            queryset = queryset.filter(project_id__in=project_ids)
        if organization_id:
            queryset = queryset.filter(project__org_id=organization_id)

        if not getattr(user, 'is_staff', False):
            armed_q = _armed_routing_q()
            if armed_q is not None:
                authorized_ids = _authorized_binding_item_ids(user, queryset.filter(armed_q))
                protected_q = armed_q
                if admin_org_ids:
                    protected_q &= ~Q(project__org_id__in=admin_org_ids)
                queryset = queryset.filter(~protected_q | Q(id__in=authorized_ids))

        return queryset.distinct()

    @staticmethod
    def _is_audit_reader(user_email, item):
        """PLAT-2692: True iff `user_email` is a configured audit identity AND
        `item` is a real QA cadence verification task. Read-only scope; the
        identity setting defaults empty so this is a no-op until an operator
        opts in. Never raises — a config/attr problem must never widen or break
        access."""
        try:
            from django.conf import settings
            email = (user_email or '').strip().lower()
            if not email:
                return False
            identities = getattr(settings, 'PULSE_AUDIT_READ_IDENTITIES', None) or []
            if email not in identities:
                return False
            labels = getattr(item, 'labels', None) or []
            if isinstance(labels, str):
                labels = [label.strip() for label in labels.split(',')]
            normalized_labels = {
                str(label).strip().lower()
                for label in labels
                if str(label).strip()
            }
            return (
                (getattr(item, 'source', '') or '').strip().lower() == 'qa_cadence_sweep'
                and (getattr(item, 'item_type', '') or '').strip().lower() == 'qa.cadence'
                and 'qa-cadence' in normalized_labels
            )
        except Exception:
            return False

    def can_access_item(user_id, item, permission='browse', is_staff=False, user_email=None):
        """
        Check if user can perform action on an item.

        Args:
            user_id: The user's ID
            item: Item instance
            permission: One of 'browse', 'edit', 'delete'
            is_staff: Whether the user is staff
            user_email: Optional user email

        Returns:
            bool
        """
        if is_staff:
            return True

        from .routing_authority import binding_candidates_for_item, subject_authorized_for_item
        binding_snapshot = binding_candidates_for_item(item.assignment_group, item)
        if binding_snapshot is not None:
            is_valid, uid = _is_valid_int_id(user_id)
            if not is_valid or permission == 'delete':
                return False
            is_assignee = _participant_slot_matches(
                getattr(item, 'assignee_email', None),
                getattr(item, 'assignee_id', None),
                user_email,
                uid,
            )
            if not is_assignee:
                return False
            return subject_authorized_for_item(
                uid, item, team_name=item.assignment_group,
            ).allowed

        participant_owner = _item_participant_matches(user_id, item, user_email)
        reporter_owner = _participant_slot_matches(
            getattr(item, 'created_by_email', None),
            getattr(item, 'created_by_id', None),
            user_email,
            user_id,
        )

        # Personal item (no project)
        if not item.project:
            return participant_owner

        # Project item
        project = item.project

        # PLAT-2692: opt-in, least-privilege audit-read scope. A configured
        # audit identity (e.g. the PLAT-1149 QA evidence-completeness auditor)
        # may BROWSE real qa_verification_sweep cadence tasks across
        # projects/orgs, so the audit can sample evidence without a 403. Read-
        # only, and a no-op unless PULSE_AUDIT_READ_IDENTITIES is populated.
        # Evaluated before the org/membership gates because the auditor is
        # intentionally cross-org for this scoped, read-only purpose.
        if permission == 'browse' and PermissionService._is_audit_reader(user_email, item):
            return True

        # Check organization membership via shizuha-admin
        if not PermissionService.can_access_organization(user_id, project.org_id, user_email):
            return False

        # Item participants retain access without an explicit project role, but
        # never without membership in the project's organization.
        if participant_owner:
            return True

        # Open projects: all org members can browse
        if project.access_level == 'open' and permission == 'browse':
            return True

        # Get project membership
        membership = PermissionService.get_user_project_membership(user_id, project, user_email)

        # Private/limited projects require membership
        if not membership and project.access_level in ['private', 'limited']:
            return False

        # Check specific permissions
        if permission == 'browse':
            return True  # All project members can browse

        elif permission == 'edit':
            # Can edit own items or if has edit permission
            return (
                participant_owner or
                (membership and membership.can_edit)
            )

        elif permission == 'delete':
            # Can delete own items or if has delete permission
            return (
                reporter_owner or
                (membership and membership.can_delete)
            )

        return False


# Singleton instance for convenience
permission_service = PermissionService()
