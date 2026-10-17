"""Server-owned, read-only resolver canaries for terminal outage evidence.

PLAT-5349 closes the gap between a durable team-outage episode and the
item-aware resolver diagnostic.  Once the original work item has moved lanes
or terminalized, its current ``assignment_group`` is no longer the routing
tuple that armed the outage.  A caller-supplied group must not be trusted as a
replacement.  This module accepts the group only when a same-organization,
system-filed outage carrier in the item's durable link graph records that exact
team signal, then builds a transient item-shaped copy for the production
resolver.  No Item, link, status, ownership, comment, notification, or capacity
reservation is written by the canary path.
"""

from __future__ import annotations

import copy
import hashlib
import json

from django.db.models import Q

from ..models import Item, ItemActivity, ItemLink


class ResolverCanaryContextError(ValueError):
    """The persisted item graph does not authorize an equivalent canary."""


def _canonical_digest(value) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _project_org_id(item) -> int | None:
    project = getattr(item, 'project', None)
    value = getattr(project, 'org_id', None) if project is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _system_outage_anchor(item, *, organization_id: int, team_slug: str):
    """Return validated anchor metadata, or ``None`` for an unrelated row."""
    if _project_org_id(item) != organization_id:
        return None
    if (
        getattr(item, 'created_by_id', None) != 0
        or str(getattr(item, 'created_by_email', '') or '').strip().lower()
        != 'system@pulse'
    ):
        return None
    metadata = getattr(item, 'metadata', None)
    if not isinstance(metadata, dict) or not metadata.get('auto_filed'):
        return None
    signal = str(metadata.get('org_health_signal') or '').strip().lower()
    # PLAT-4829 scoped new carriers by organization.  Legacy carriers remain
    # valid only because the anchor's project boundary above supplies the
    # missing organization; never accept another numeric scope.
    valid_signals = {
        f'zero-member-outage:{team_slug}',
        f'zero-member-outage:{organization_id}:{team_slug}',
    }
    if signal not in valid_signals:
        return None

    outage = metadata.get('org_health_outage') or {}
    context = outage.get('probe_context') or {}
    if context:
        # A present context is authority, not a hint.  Never fall back to the
        # legacy link-only equivalence when its subject is stale/mismatched.
        subject = context.get('subject') or {}
        try:
            context_org_id = int(subject.get('organization_id'))
        except (TypeError, ValueError):
            return None
        if (
            context_org_id != organization_id
            or str(subject.get('team_slug') or '').strip().lower() != team_slug
            or str(context.get('signal') or '').strip().lower() != signal
        ):
            return None
    return {'signal': signal, 'context': context}


def _linked_outage_anchor(source_item, team_slug: str, *, max_depth: int = 2):
    """Find a system-filed exact-team carrier through a bounded link graph.

    The task/link rows are durable server records.  Treating links as undirected
    is intentional: ``blocks`` and ``is_blocked_by`` encode the same incident
    lineage with opposite storage directions, while ``relates_to`` is symmetric.
    The bound covers source -> owning regression -> outage carrier without
    allowing an unbounded graph walk.
    """
    organization_id = _project_org_id(source_item)
    if organization_id is None:
        raise ResolverCanaryContextError('project_organization_missing')

    start_id = source_item.pk
    source_terminal_at = None
    from .workflow_service import WorkflowService
    for activity in ItemActivity.objects.filter(
        item=source_item,
        field_name__iexact='status',
    ).order_by('created_at', 'id'):
        if WorkflowService._status_is_terminal(activity.new_value):
            source_terminal_at = activity.created_at
            break
    source_terminal_at = source_terminal_at or getattr(source_item, 'updated_at', None)
    visited = {start_id}
    frontier = {start_id}
    parent = {}
    items = {start_id: source_item}
    candidates = []

    direct = _system_outage_anchor(
        source_item,
        organization_id=organization_id,
        team_slug=team_slug,
    )
    if direct:
        candidates.append((0, source_item, direct))

    for depth in range(1, max_depth + 1):
        links = list(
            ItemLink.objects.filter(
                Q(source_item_id__in=frontier) | Q(target_item_id__in=frontier)
            ).select_related(
                'source_item__project', 'target_item__project'
            ).order_by('created_at', 'id')
        )
        next_frontier = set()
        for link in links:
            # The equivalence lineage must already exist when the source item
            # terminalizes.  A caller cannot retroactively authorize a canary by
            # attaching an arbitrary completed item to a historical carrier.
            if (
                source_terminal_at is not None
                and link.created_at > source_terminal_at
            ):
                continue
            for current_id, neighbor in (
                (link.source_item_id, link.target_item),
                (link.target_item_id, link.source_item),
            ):
                if current_id not in frontier or neighbor.pk in visited:
                    continue
                if _project_org_id(neighbor) != organization_id:
                    continue
                visited.add(neighbor.pk)
                next_frontier.add(neighbor.pk)
                items[neighbor.pk] = neighbor
                parent[neighbor.pk] = (current_id, link)
                anchor = _system_outage_anchor(
                    neighbor,
                    organization_id=organization_id,
                    team_slug=team_slug,
                )
                if anchor:
                    candidates.append((depth, neighbor, anchor))
        frontier = next_frontier
        if not frontier:
            break

    if not candidates:
        raise ResolverCanaryContextError('no_server_owned_outage_equivalence')

    # Prefer a persisted exact probe context, then the closest/oldest carrier.
    candidates.sort(key=lambda row: (
        0 if row[2]['context'] else 1,
        row[0],
        getattr(row[1], 'created_at', None),
        str(row[1].pk),
    ))
    _depth, anchor, anchor_data = candidates[0]

    path_ids = [anchor.pk]
    path_links = []
    cursor = anchor.pk
    while cursor != start_id:
        previous, link = parent[cursor]
        path_links.append({
            'id': str(link.pk),
            'type': link.link_type,
        })
        path_ids.append(previous)
        cursor = previous
    path_ids.reverse()
    path_links.reverse()
    return (
        anchor,
        anchor_data,
        [items[item_id] for item_id in path_ids],
        path_links,
    )


def build_terminal_outage_resolver_canary(source_item, team_name: str):
    """Return ``(canary_item, evidence, candidate_snapshot)``.

    The canary retains the persisted probe PK/project so exact routing-binding
    authorization can re-load the server-owned organization boundary, but all
    mutable routing/workflow fields are copied in memory.  Required capability
    input is frozen on a private attribute consumed by ``WorkflowService``.
    """
    from .routing_authority import routing_candidate_snapshot_for_item
    from .workflow_service import WorkflowService

    team_slug = WorkflowService._canonical_team_name(team_name)
    if not team_slug:
        raise ResolverCanaryContextError('team_missing')
    if not WorkflowService._status_is_terminal(getattr(source_item, 'status', None)):
        raise ResolverCanaryContextError('source_item_not_terminal')

    organization_id = _project_org_id(source_item)
    anchor, anchor_data, path_items, path_links = _linked_outage_anchor(
        source_item,
        team_slug,
    )
    context = anchor_data['context']
    persisted_probe = context.get('probe') or {}

    probe_source = source_item
    if persisted_probe:
        probe_id = persisted_probe.get('item_id')
        probe_key = str(persisted_probe.get('item_key') or '').strip()
        probe_q = Item.objects.select_related('project').filter(
            project__org_id=organization_id,
        )
        if probe_id:
            probe_source = probe_q.filter(pk=probe_id).first()
        elif probe_key:
            probe_source = probe_q.filter(item_key__iexact=probe_key).first()
        else:
            probe_source = None
        if probe_source is None:
            raise ResolverCanaryContextError('persisted_probe_item_missing')

    canary = copy.copy(probe_source)
    probe_status = str(persisted_probe.get('status') or '').strip()
    if not probe_status or WorkflowService._status_is_terminal(probe_status):
        # Legacy carriers predate persisted OutageProbeContext.  Use the
        # workflow's server-owned initial/first active stage rather than a
        # caller-supplied or hard-coded stage.  The fallback supports legacy
        # workflows whose definition row is unavailable in old fixtures.
        workflow = WorkflowService._workflow_for_permission_check(probe_source)
        initial = getattr(workflow, 'initial_status', None) if workflow else None
        probe_status = str(getattr(initial, 'slug', '') or '').strip()
        if not probe_status and workflow is not None:
            for workflow_status in workflow.workflow_statuses.select_related(
                'status'
            ).order_by('position'):
                candidate_status = str(
                    getattr(workflow_status.status, 'slug', '') or ''
                ).strip()
                if (
                    candidate_status
                    and not WorkflowService._status_is_terminal(candidate_status)
                ):
                    probe_status = candidate_status
                    break
        if not probe_status:
            probe_status = 'open'
    probe_workflow = str(
        persisted_probe.get('workflow')
        or getattr(probe_source, 'workflow_name', '')
        or ''
    ).strip()

    persisted_caps = persisted_probe.get('required_capabilities')
    if isinstance(persisted_caps, list):
        required_capabilities = sorted({
            WorkflowService._normalize_capability_slug(value)
            for value in persisted_caps
            if WorkflowService._normalize_capability_slug(value)
        })
    else:
        capability_probe = copy.copy(probe_source)
        capability_probe.assignment_group = team_slug
        capability_probe.status = probe_status
        capability_probe.workflow_status = probe_status
        capability_probe.workflow_name = probe_workflow
        required_capabilities = sorted(
            WorkflowService._task_required_capabilities(capability_probe)
        )

    canary.assignment_group = team_slug
    canary.assignee_id = None
    canary.assignee_email = ''
    canary.status = probe_status
    canary.workflow_status = probe_status
    canary.workflow_name = probe_workflow
    canary.labels = {}
    canary.tags = ''
    canary.metadata = {}
    canary._suppress_routability_holds = True
    canary._resolver_canary_required_capabilities = frozenset(
        required_capabilities
    )

    intent = str(persisted_probe.get('intent') or 'new_assignment').strip()
    requirement_key = str(persisted_probe.get('requirement_key') or '').strip()
    if not requirement_key:
        requirement_key = ':'.join([
            str(organization_id),
            probe_workflow,
            probe_status,
            intent,
        ])
    capability_decision_version = str(
        persisted_probe.get('capability_decision_version') or ''
    ).strip() or _canonical_digest({
        'requirement_key': requirement_key,
        'required_capabilities': required_capabilities,
    })

    candidate_snapshot = routing_candidate_snapshot_for_item(team_slug, canary)
    routing_source = candidate_snapshot.get('source') or 'authority_unknown'
    source_version = candidate_snapshot.get('source_version')
    evidence = {
        'schema_version': 'pulse.resolver-canary.v1',
        'kind': (
            'persisted_outage_probe_equivalence'
            if context else 'legacy_server_carrier_equivalence'
        ),
        'subject': {
            'organization_id': organization_id,
            'team_slug': team_slug,
        },
        'episode_generation': (
            context.get('episode_generation')
            if context else (anchor.metadata or {}).get('auto_filed')
        ),
        'signal': anchor_data['signal'],
        'anchor_item_key': anchor.item_key,
        'source_item_key': source_item.item_key,
        'probe_item_key': probe_source.item_key,
        'equivalence_path': [item.item_key for item in path_items],
        'equivalence_links': path_links,
        'workflow': probe_workflow,
        'status': probe_status,
        'intent': intent,
        'requirement_key': requirement_key,
        'required_capabilities': required_capabilities,
        'capability_decision_version': capability_decision_version,
        'routing_source': routing_source,
        'routing_source_version': source_version,
        'entitlement_version': (
            source_version
            if routing_source == 'routing_binding'
            else 'team_roster_compat'
        ),
    }
    evidence['context_digest'] = _canonical_digest(evidence)
    canary._resolver_canary_context = evidence
    return canary, evidence, candidate_snapshot


def candidate_snapshot_fingerprint(snapshot) -> dict:
    """Bounded equality input for the pre/post resolver authority fence."""
    return {
        'source': snapshot.get('source'),
        'source_version': snapshot.get('source_version'),
        'reason': snapshot.get('reason'),
        'candidates': sorted(
            (
                int(row['user_id']) if row.get('user_id') is not None else None,
                str(row.get('email') or '').strip().lower(),
            )
            for row in (snapshot.get('candidates') or [])
            if isinstance(row, dict)
        ),
    }
