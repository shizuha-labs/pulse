"""PLAT-5343 slice 1 — Pulse v2 project-membership candidate/roster snapshot.

``POST /api/internal/v2/project-membership-candidates:resolve`` returns the
subject-bound local snapshot (HLD v12 §4.2) that Admin composes with its own
local membership set. This route is the acyclic replacement for the v1
Admin→Pulse→Admin callback: Pulse performs a fixed, bounded number of local
queries (two on every path that has candidates, one when there are none) and
**zero** HTTP/RPC calls.

Normative properties pinned here (HLD §4.2):

- Candidate identity parity:
  * an active ``ProjectMembership`` with non-null ``user_id`` matches only the
    exact asserted ``sub``;
  * email/agent-domain aliases may match only an active row whose
    ``user_id IS NULL``;
  * inactive memberships never match;
  * role de-duplication preserves the current ``owner > member > viewer``
    priority;
  * every candidate carries its exact project ``organization_id``.
- Roster evidence parity is the exact current
  ``PermissionService.identity_belongs_to_organization`` roster predicate: an
  active ``TeamRoster``, exact non-null ``organization_id``, and an asserted
  email alias present in ``tiers``. A similarly named team in another
  organization, an inactive roster, a null-org roster, or ``junior_emails``
  without a ``tiers`` placement is not evidence.
- The snapshot is computed fresh for this call and never cached beyond
  ``valid_for_ms`` (which this route does not use for caching at all — the
  value only tells Admin how long the answer may be trusted).
- Provider SLO: p99 ≤ 250 ms, zero responses > 500 ms (latency harness, §10
  gate 1).

The v1 route (``/api/internal/users/<id>/project-memberships/``) is retained
untouched during the shadow/parity window (§4.4 step 1) and is removed only
after exact-head parity and live mint evidence (§4.4 step 5).
"""
import logging
import uuid

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ProjectMembership, TeamRoster
from .services.permissions import _email_variants
from .services.subject_assertion import (
    SubjectAssertionRejected,
    authenticate_internal_service,
    subject_fingerprint,
)

logger = logging.getLogger('tasks.membership_v2')

SNAPSHOT_SCHEMA = 'shizuha.pulse.project-membership-candidates.v2'
SNAPSHOT_VALID_FOR_MS = 500

# Exact current v1 de-duplication priority (InternalUserProjectMembershipsView).
ROLE_PRIORITY = {'owner': 3, 'member': 2, 'viewer': 1}


def _alias_set(email):
    """Aliases the asserted email may match: case-insensitive exact email plus
    only the same-local-part swap between @shizuha.com and @agents.shizuha.io
    (HLD §4.1 — receivers derive aliases themselves)."""
    return set(_email_variants(email))


def build_membership_snapshot(subject_id, email):
    """Compute the v2 snapshot for one subject in ≤3 local queries, zero calls.

    Query 1 — candidate ProjectMembership rows (exact-sub OR null-user_id
    alias rows, active only, joined to the project for its exact org).
    Query 2 — active TeamRoster rows for the candidate organizations (only
    when at least one candidate exists); the alias-in-tiers scan is the exact
    current in-Python predicate.
    """
    aliases = _alias_set(email)

    # --- Query 1: candidates -------------------------------------------------
    from django.db.models import Q
    identity_q = Q(user_id=subject_id, is_active=True)
    if aliases:
        alias_q = Q(is_active=True, user_id__isnull=True)
        email_q = None
        for variant in aliases:
            clause = Q(user_email__iexact=variant)
            email_q = clause if email_q is None else email_q | clause
        alias_q &= email_q
        identity_q |= alias_q

    rows = list(
        ProjectMembership.objects
        .filter(identity_q)
        .select_related('project')
        .values_list('project_id', 'project__org_id', 'role')
    )

    # Role de-duplication, exact current priority; one candidate per project
    # carrying its exact project organization_id.
    dedup = {}
    org_by_project = {}
    for project_id, org_id, role in rows:
        org_by_project.setdefault(project_id, org_id)
        existing = dedup.get(project_id)
        if existing is None or ROLE_PRIORITY.get(role, 0) > ROLE_PRIORITY.get(existing, 0):
            dedup[project_id] = role

    unique_candidates = [
        {'project_id': project_id, 'organization_id': org_by_project[project_id], 'role': role}
        for project_id, role in dedup.items()
    ]

    # --- Query 2: roster evidence for the candidate organizations ------------
    roster_org_evidence = []
    candidate_orgs = {c['organization_id'] for c in unique_candidates if c['organization_id'] is not None}
    if candidate_orgs and aliases:
        rosters = list(
            TeamRoster.objects
            .filter(organization_id__in=candidate_orgs, is_active=True)
            .values_list('id', 'version', 'organization_id', 'tiers')
        )
        by_org = {}
        for roster_id, version, org_id, tiers in rosters:
            if not isinstance(tiers, list):
                continue
            matched = False
            for tier in tiers:
                members = tier if isinstance(tier, list) else [tier]
                for member in members:
                    if aliases & set(_email_variants(member)):
                        matched = True
                        break
                if matched:
                    break
            if matched:
                by_org.setdefault(org_id, []).append({'id': roster_id, 'version': version})
        for org_id in sorted(by_org):
            roster_org_evidence.append({
                'organization_id': org_id,
                'roster_rows': sorted(by_org[org_id], key=lambda row: row['id']),
            })

    return unique_candidates, roster_org_evidence


class ProjectMembershipCandidatesResolveV2View(APIView):
    """Admin-facing v2 snapshot endpoint (HLD §4.2). Fail-closed auth."""

    # Authentication is enforced in-view (fail-closed, typed rejections); DRF's
    # AllowAny here does NOT make the route public — every request without a
    # valid credential + subject assertion is rejected below.
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        try:
            principal, claims = authenticate_internal_service(
                request,
                expected_issuer='shizuha-admin',
                expected_audience='shizuha-pulse',
                expected_scope='project-membership-candidates:resolve',
                require_parent_hash=True,
                body=getattr(request, 'data', None),
            )
        except SubjectAssertionRejected as exc:
            logger.warning('v2-candidates rejected service=%s reason=%s',
                           (request.META.get('HTTP_X_INTERNAL_SERVICE', '') or '').strip(),
                           exc.reason)
            return Response(
                {'error': 'internal_service_auth_failed', 'reason': exc.reason},
                status=401,
            )

        subject_id = claims['sub']
        email = claims.get('email')

        candidates, roster_org_evidence = build_membership_snapshot(subject_id, email)

        return Response({
            'schema': SNAPSHOT_SCHEMA,
            'subject_fingerprint': subject_fingerprint(claims.get('iss', ''), subject_id, email),
            'snapshot_id': str(uuid.uuid4()),
            'generated_at': timezone.now().isoformat().replace('+00:00', 'Z'),
            'valid_for_ms': SNAPSHOT_VALID_FOR_MS,
            'candidates': candidates,
            'roster_org_evidence': roster_org_evidence,
        })
