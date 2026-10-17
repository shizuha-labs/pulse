"""EVOL-20 Ph3 (PLAT-1304): membership rebalancer — MOVE MEMBERSHIPS, NOT TASKS.

Signed HLD ab8fc292 §5/§8/§10. When a project's ready-queue depth outruns its
active members' capacity, the sweep grants a **time-boxed SECONDARY**
AgentProjectMembership to an under-loaded eligible agent (changing FUTURE routing
only), and expires stale/time-boxed secondaries — instead of shuffling tasks
across projects (the churn class this replaces). Task-level moves remain ONLY for
failover (the PLS-15/PLAT-291 unavailable-agent resolver path), NOT here.

Signed guardrails (HLD §2/§5): cap ≤ 4 active memberships/agent (enforced by the
model), ≥ 24h cooldown between membership changes per agent, every change
audit-logged with the triggering metric. Flag-gated off by default
(``PULSE_MEMBERSHIP_REBALANCER_ENABLED``); overflow/never-idle semantics are
untouched (affinity is never made exclusive).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..models import (
    AgentProjectMembership,
    AgentProjectMembershipActivity,
    Item,
    Project,
)
from .agent_membership_service import create_membership, expire_membership
from .workflow_service import WorkflowService

logger = logging.getLogger(__name__)


def rebalancer_enabled() -> bool:
    return bool(getattr(settings, 'PULSE_MEMBERSHIP_REBALANCER_ENABLED', False))


def _skew_threshold() -> int:
    return int(getattr(settings, 'PULSE_MEMBERSHIP_REBALANCE_SKEW', 2))


def _secondary_ttl_hours() -> int:
    return int(getattr(settings, 'PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS', 168))


def _cooldown_hours() -> int:
    # SIGNED: ≥ 24h between membership changes per agent (HLD §5).
    return int(getattr(settings, 'PULSE_MEMBERSHIP_COOLDOWN_HOURS', 24))


def _project_ready_depth(project_id) -> int:
    """READY-queue depth for a project (HLD §5): PICKABLE work waiting for a seat —
    non-terminal, non-blocked/scheduled/deferred, non-umbrella, AND not already
    being worked (``in_progress`` excluded). Counting fully-assigned in-progress WIP
    would over-grant secondaries on work that already has an owner (kei P2); the
    signal we want is a backlog of unstarted/ready tasks outrunning member capacity."""
    return (
        Item.objects
        .filter(project_id=project_id, resolution__isnull=True)
        .exclude(status__in=WorkflowService._assignment_excluded_statuses())
        .exclude(status='in_progress')
        .exclude(WorkflowService._umbrella_item_q())
        .count()
    )


def _active_cap() -> int:
    cap = WorkflowService._wip_cap()
    return cap if cap and cap > 0 else 5


def _member_headroom(email, user_id) -> int:
    """Spare hands-on capacity for a member (>= 0)."""
    load = WorkflowService._agent_active_load(user_id, email)
    return max(0, _active_cap() - load)


def _last_change_at(agent_email, agent_user_id):
    """Most recent membership-activity timestamp for an agent (cooldown anchor)."""
    from django.db.models import Q
    q = Q(membership__agent_user_email__iexact=(agent_email or '').strip().lower())
    if agent_user_id is not None:
        q |= Q(membership__agent_user_id=agent_user_id)
    row = (AgentProjectMembershipActivity.objects.filter(q)
           .order_by('-created_at').values_list('created_at', flat=True).first())
    return row


def _agent_active_membership_count(agent_email, agent_user_id) -> int:
    from django.db.models import Q
    q = Q(agent_user_email__iexact=(agent_email or '').strip().lower())
    if agent_user_id is not None:
        q |= Q(agent_user_id=agent_user_id)
    return AgentProjectMembership.objects.filter(active=True).filter(q).count()


def _candidate_pool(project, now):
    """Under-loaded eligible agents for a secondary membership in `project`.

    Eligibility (org/cap/cooldown/availability safe):
    - already an ACTIVE member of ANOTHER project in the SAME org (bounds the pool
      to known same-org agents — never grants across orgs or to unknown agents);
    - NOT already a member of this project;
    - available for assignment (excludes stopped/disabled agents);
    - below the ≤4 active-membership cap;
    - past the ≥24h cooldown since its last membership change;
    - has hands-on headroom (< active cap).
    Returned least-loaded first.
    """
    cooldown_cutoff = now - timezone.timedelta(hours=_cooldown_hours())
    existing = set(
        AgentProjectMembership.objects
        .filter(active=True, project_id=project.id)
        .values_list('agent_user_email', flat=True))
    existing = {(e or '').strip().lower() for e in existing}

    # Same-org agent universe: distinct active members across the org's projects.
    org_project_ids = list(
        Project.objects.filter(org_id=project.org_id).values_list('id', flat=True))
    rows = (
        AgentProjectMembership.objects
        .filter(active=True, project_id__in=org_project_ids)
        .values_list('agent_user_email', 'agent_user_id').distinct())

    candidates = []
    seen = set()
    for email, uid in rows:
        e = (email or '').strip().lower()
        if not e or e in existing or e in seen:
            continue
        seen.add(e)
        if not WorkflowService._is_agent_available_for_assignment(e):
            continue
        if _agent_active_membership_count(e, uid) >= AgentProjectMembership.MAX_ACTIVE_PER_AGENT:
            continue
        last = _last_change_at(e, uid)
        if last is not None and last > cooldown_cutoff:
            continue  # cooldown not elapsed
        headroom = _member_headroom(e, uid)
        if headroom <= 0:
            continue
        candidates.append((headroom, e, uid))
    # most headroom (least-loaded) first
    candidates.sort(key=lambda c: -c[0])
    return candidates


def rebalance_memberships(*, now=None, actor_email='system@shizuha.com', actor_user_id=None) -> dict:
    """One rebalance pass. Returns a summary of expired + added memberships.

    No-op (returns disabled summary) unless the Ph3 flag is on."""
    if not rebalancer_enabled():
        return {'enabled': False, 'expired': 0, 'added': [], 'skipped': []}

    now = now or timezone.now()
    summary = {'enabled': True, 'expired': 0, 'added': [], 'skipped': []}

    # 1. Expire time-boxed secondaries whose window has passed.
    stale = AgentProjectMembership.objects.filter(
        active=True, tier='secondary', expires_at__lte=now)
    for m in list(stale):
        expire_membership(m, reason='EVOL-20 Ph3: time-boxed secondary expired',
                          actor_user_id=actor_user_id, actor_email=actor_email)
        summary['expired'] += 1

    # 2. Per project: grant a secondary where ready-depth outruns capacity.
    threshold = _skew_threshold()
    ttl = timezone.timedelta(hours=_secondary_ttl_hours())
    projects = Project.objects.filter(is_active=True)
    for project in projects:
        members = list(
            AgentProjectMembership.objects
            .filter(active=True, project_id=project.id)
            .values_list('agent_user_email', 'agent_user_id'))
        if not members:
            continue  # never grant into a project with no primary members
        capacity = sum(_member_headroom(e, uid) for e, uid in members)
        ready = _project_ready_depth(project.id)
        skew = ready - capacity
        if skew <= threshold:
            continue

        pool = _candidate_pool(project, now)
        if not pool:
            summary['skipped'].append(
                {'project': project.key, 'skew': skew, 'reason': 'no eligible under-loaded agent'})
            continue

        _headroom, email, uid = pool[0]
        reason = (f'EVOL-20 Ph3 rebalance: {project.key} ready={ready} '
                  f'capacity={capacity} skew={skew} > threshold={threshold}')
        try:
            m = create_membership(
                project, agent_user_email=email, agent_user_id=uid,
                tier='secondary', reason=reason,
                actor_user_id=actor_user_id, actor_email=actor_email)
            m.expires_at = now + ttl
            m.save(update_fields=['expires_at', 'updated_at'])
            summary['added'].append(
                {'project': project.key, 'agent': email, 'skew': skew,
                 'expires_at': m.expires_at.isoformat()})
            logger.info('EVOL-20 Ph3: granted secondary %s @ %s (%s)',
                        email, project.key, reason)
        except ValidationError as exc:
            # cap raced or field invalid — skip, never crash the sweep.
            summary['skipped'].append(
                {'project': project.key, 'agent': email, 'reason': str(exc)})
    return summary
