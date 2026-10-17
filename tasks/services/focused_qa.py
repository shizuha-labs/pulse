"""Focused live-QA task automation for high-risk work (PLS-184)."""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Iterable, List, Optional, Set

from django.db import transaction
from django.db.models import Q

from tasks.models import (
    Comment,
    DeploymentEvent,
    Item,
    ItemActivity,
    ItemLink,
    Project,
    TeamRoster,
    Workflow,
)

logger = logging.getLogger(__name__)

FOCUSED_QA_ROUTING_TAGS = {
    'qa-independence-exception',
    'qa-routing-blocked',
}

HIGH_RISK_LABELS = {
    'requires-live-qa',
    'auth',
    'security-sensitive',
    'data-migration',
    'payments-books',
    'agent-runtime',
    'infra-control-plane',
    'user-facing',
    'replay',
}
# PLS-300: deterministic primary-label order so a multi-risk finding coalesces to
# exactly ONE focused-QA task (highest-signal label wins) instead of one-per-label.
_RISK_LABEL_PRIORITY = [
    'requires-live-qa', 'auth', 'payments-books', 'data-migration',
    'agent-runtime', 'infra-control-plane', 'replay', 'security-sensitive',
    'user-facing',
]

# PLAT-8222: demand-side capability mapping — high-risk label → the
# TeamCapabilityProfile capability tag a candidate seat's team must hold before
# the rebalance sweep may pull the item. Co-located with HIGH_RISK_LABELS so the
# demand vocabulary and its capability mapping evolve together. A high-risk
# label with NO mapping entry is deliberately UNGATED: the gate must never
# freeze routing because a new vocabulary addition has not been mapped yet.
HIGH_RISK_LABEL_CAPABILITY_MAP = {
    'requires-live-qa': 'live-qa',
    'infra-control-plane': 'cluster-exec',
    'security-sensitive': 'security-review',
    'auth': 'security-review',
    'data-migration': 'data-migration',
    'payments-books': 'payments-review',
    'agent-runtime': 'agent-runtime',
    'replay': 'replay-inspection',
    'user-facing': 'user-facing',
}


def item_required_capabilities(item) -> Set[str]:
    """PLAT-8222: capability tags demanded by an item's high-risk labels.

    Pure predicate over the item's labels/tags (no DB access). The rebalance
    sweep's candidate gate unions these per item and requires the candidate
    seat's team to hold every one. Labels with no mapping entry contribute
    nothing (unmapped vocabulary stays un-gated — see the map comment)."""
    required: Set[str] = set()
    for label in risk_labels_for_item(item):
        cap = HIGH_RISK_LABEL_CAPABILITY_MAP.get(label)
        if cap:
            required.add(cap)
    return required


def seat_team_satisfies(team_name, required) -> bool:
    """PLAT-8222: can this team's seats execute work demanding ``required``?

    PLS-198 fail-open convention: a team with NO TeamCapabilityProfile row is
    un-gated — the registry is opt-in, and gating profile-less teams would
    freeze routing fleet-wide (the exact failure mode this defect documents).
    A profiled team must hold every required capability, evaluated against the
    union of the profile row and the hardcoded baseline
    (``WorkflowService._team_conferred_capabilities``) so a baseline-conferred
    capability is never dropped by an incomplete profile row. Slugs are
    normalized through ``_normalize_capability_slug`` on both sides so
    ``host_exec``/``host-exec``-style variants compare equal."""
    required = {str(r or '').strip().lower() for r in (required or set()) if str(r or '').strip()}
    if not required:
        return True
    from tasks.models import TeamCapabilityProfile
    # iexact: the sweep matches assignment_group case-insensitively (PLAT-916
    # group_q), so the profile check must resolve the same way; the row's
    # canonical team_name feeds the conferred read.
    row = TeamCapabilityProfile.objects.filter(team_name__iexact=team_name).first()
    if row is None:
        return True  # PLS-198 fail-open: opt-in registry, profile-less teams un-gated
    from tasks.services.workflow_service import WorkflowService
    norm = WorkflowService._normalize_capability_slug
    conferred = {norm(c) for c in WorkflowService._team_conferred_capabilities(row.team_name)}
    conferred.discard('')
    return {norm(r) for r in required} <= conferred


TERMINAL_STATUS_SLUGS = {
    'completed', 'closed', 'done', 'resolved', 'merged', 'deployed', 'verified',
}
NON_ACTIVE_STATUSES = {
    'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}
# PLS-300 (gate A): a finding closed as a NON-FIX ships no behavior change, so
# there is nothing to live-verify. Matched against the item's resolution slug or
# status when a transition drops it into a terminal state.
NON_FIX_RESOLUTION_SLUGS = {
    'rejected', 'wont_fix', 'wontfix', "won't_fix", 'not_a_bug', 'not-a-bug',
    'notabug', 'false_positive', 'false-positive', 'invalid', 'duplicate',
    'accepted_risk', 'accepted-risk', 'obsolete', 'cancelled', 'canceled',
}
# PLS-300 (gate B): static-analysis / dependency-CVE findings have no distinct
# LIVE user surface — they are verified by re-running the scanner / CI or by a
# code path, not by live QA. `security-ci` tags every such scanner finding; a
# genuine live-surface remediation lives on the owning DEV task (which carries
# auth/user-facing risk labels), not on the scanner finding itself.
STATIC_SCAN_TOKENS = {
    'security-ci', 'dependency', 'dependencies', 'deps', 'sca', 'dep-cve',
    'tool:deps', 'tool:osv', 'tool:trivy', 'tool:bandit', 'tool:semgrep',
    'tool:gitleaks', 'tool:pip-audit', 'tool:npm-audit',
}
# Process-meta origins are bookkeeping, not a live product surface.
# Minting focused-QA from them is QA-of-QA and was the bulk of Pulse
# clutter (live 2026-08-29: Formal review / Re-approve / Deploy-verify
# plus "Record correlated production DeploymentEvent").
# PLAT-7600 follow-up (live repro FRG-68): waiver-mint executor tasks
# ("Mint [role-gated] deploy-verify waiver for <target> …") are the same
# bookkeeping class — the receipt lands on the waiver TARGET, the mint
# task has no deployable artifact, and QA-of-QA on it can only close via
# waiver recursion. Anchored to the imperative "Mint …" form so code
# tasks that BUILD the waiver endpoint ("Add …" titles) still spawn QA.
PROCESS_META_TITLE_PREFIXES = (
    'formal review of',
    're-approve ',
    'deploy-verify ',
    'record correlated production deploymentevent',
    'mint deploy-verify waiver',
    'mint role-gated deploy-verify waiver',
)

# PLS-571: risk labels prioritize live-testable work; they do not make an
# otherwise non-live artifact eligible.  These are typed, machine-authored
# signals rather than title keywords.  Workflow.resolve() canonicalizes display
# names / slug spelling before this set is consulted.
DEVELOPMENT_WORKFLOW_SLUGS = {
    'autonomous-dev', 'autonomous-bug', 'bug-workflow', 'software-development',
}
IMPLEMENTATION_ITEM_TYPE_PREFIXES = (
    'bug', 'task.bug', 'task.feature', 'task.improvement', 'task.development',
)
LIVE_TARGET_METADATA_KEYS = {
    'live_target', 'live_targets', 'qa_surface', 'qa_surfaces',
    'deployment_target', 'live_url',
}
IMPLEMENTATION_METADATA_KEYS = {
    'pull_requests', 'linked_pr', 'implementation_commit', 'merged_commit',
}
NO_LIVE_MUTATION_TOKENS = {'no-live-mutation', 'no_live_mutation'}


def _focused_qa_enabled() -> bool:
    """PLS-300 kill switch: set PULSE_FOCUSED_QA_ENABLED=0 to instantly pause
    focused-QA auto-creation (interim containment) without a code change."""
    return os.environ.get('PULSE_FOCUSED_QA_ENABLED', '1').strip().lower() not in (
        '0', 'false', 'no', 'off',
    )


# ---------------------------------------------------------------------------
# PLS-825 (PLS-819 S2): focused-qa workflow cutover (HLD §6).
# ---------------------------------------------------------------------------

FOCUSED_QA_WORKFLOW_SLUG = 'focused-qa'
# HLD §3 — the four typed disposition transitions that make the dedicated
# workflow the disposition contract. New generation creation is enabled only
# when this inventory is present and active.
REQUIRED_FOCUSED_QA_TRANSITIONS = {
    'Pass QA Evidence',
    'Confirm Product Failure',
    'Return Evidence for Correction',
    'Close Duplicate / Subsumed',
}
# HLD §6 step 7 — compatibility window: the legacy reviewed-task generator path
# stays available (disabled by default) via PULSE_FOCUSED_QA_WORKFLOW=reviewed-task.
COMPAT_WORKFLOW_NAME = 'reviewed-task'


def focused_qa_rollout_gate() -> dict:
    """HLD §6 step 6 readiness report for the focused-QA cutover.

    New creation may switch to the ``focused-qa`` workflow only after schema,
    workflow inventory, equivalence-key inventory, backfill counts, and handler
    probes are green. Returns a dict of named checks; ``ready`` is True only when
    every check passes. Used by ops/verification to prove the rollout is safe and
    by the runtime generator gate.
    """
    checks = {}
    try:
        from tasks.models import (
            FocusedQABugEquivalenceKey,
            FocusedQADispositionReceipt,
            FocusedQAGeneration,
            Workflow,
            WorkflowTransition,
        )
    except Exception as exc:  # pragma: no cover - schema probe
        checks['schema'] = False
        checks['workflow_inventory'] = False
        checks['equivalence_key_inventory'] = False
        checks['backfill'] = False
        checks['handler_probes'] = False
        checks['error'] = str(exc)
        checks['ready'] = False
        return checks

    # Schema: the immutable generation/receipt/equivalence tables exist.
    try:
        FocusedQAGeneration.objects.none().count()
        FocusedQADispositionReceipt.objects.none().count()
        FocusedQABugEquivalenceKey.objects.none().count()
        checks['schema'] = True
    except Exception:
        checks['schema'] = False

    # Workflow inventory: focused-qa workflow active with the typed dispositions.
    try:
        wf = Workflow.objects.get(slug=FOCUSED_QA_WORKFLOW_SLUG, is_active=True)
        have = set(
            WorkflowTransition.objects.filter(workflow=wf).values_list('name', flat=True)
        )
        checks['workflow_inventory'] = REQUIRED_FOCUSED_QA_TRANSITIONS.issubset(have)
    except Workflow.DoesNotExist:
        checks['workflow_inventory'] = False

    # Equivalence-key inventory: the reservation table is queryable.
    try:
        FocusedQABugEquivalenceKey.objects.none().count()
        checks['equivalence_key_inventory'] = True
    except Exception:
        checks['equivalence_key_inventory'] = False

    # Backfill counts: every non-terminal focused-qa-automation task has a
    # generation row (lossless backfill complete). A missing generation means the
    # cutover would race an incomplete backfill — disable new creation.
    try:
        orphaned = (
            Item.objects
            .filter(source='focused-qa-automation')
            .exclude(status__in=NON_ACTIVE_STATUSES)
            .filter(focused_qa_generation__isnull=True)
            .count()
        )
        checks['backfill'] = orphaned == 0
        checks['backfill_orphans'] = orphaned
    except Exception:
        checks['backfill'] = False

    # Handler probes: the disposition engine is importable and callable.
    try:
        from tasks.services import focused_qa_disposition
        checks['handler_probes'] = callable(getattr(focused_qa_disposition, 'commit_disposition', None))
    except Exception:
        checks['handler_probes'] = False

    checks['ready'] = all(
        checks.get(k) for k in (
            'schema', 'workflow_inventory', 'equivalence_key_inventory',
            'backfill', 'handler_probes',
        )
    )
    return checks


def _focused_qa_workflow_ready() -> bool:
    """Runtime gate for the generator: the dedicated workflow is seeded with the
    typed dispositions and the lossless backfill is complete. Mirrors HLD §6
    step 6; cheap enough to call on each creation attempt."""
    try:
        return bool(focused_qa_rollout_gate().get('ready'))
    except Exception:
        return False


def _focused_qa_workflow_name() -> str:
    """HLD §6 step 7: the focused-qa workflow is the default once the rollout
    gate is green; the legacy reviewed-task generator path stays available
    (disabled by default) for the initial compatibility window via
    ``PULSE_FOCUSED_QA_WORKFLOW=reviewed-task``. New creation is disabled
    entirely when the PLS-300 kill switch is off."""
    if not _focused_qa_enabled():
        return COMPAT_WORKFLOW_NAME
    forced = os.environ.get('PULSE_FOCUSED_QA_WORKFLOW', '').strip().lower()
    if forced == COMPAT_WORKFLOW_NAME:
        return COMPAT_WORKFLOW_NAME
    if _focused_qa_workflow_ready():
        return FOCUSED_QA_WORKFLOW_SLUG
    return COMPAT_WORKFLOW_NAME


def _bind_focused_qa_generation(
    *,
    task: Item,
    origin_item: Optional[Item],
    deployment_event: Optional[DeploymentEvent],
    project: Optional[Project],
    risk_label: str,
    environment: str,
    trigger: str,
    generation_key: str,
) -> None:
    """HLD §2.1: bind one immutable generation row to a newly created focused-QA
    task. Idempotent — reusing the same generation key with the same target
    snapshot returns the existing row; a different snapshot raises
    GenerationConflict and mutates nothing (retries are safe)."""
    from tasks.services.focused_qa_disposition import find_or_create_generation

    surface_key = _deployment_surface_key(deployment_event)
    origin_surface_schema = 'task-root.v1'
    origin_surface_key = f'task-root.v1:{origin_item.pk}' if origin_item else ''
    find_or_create_generation(
        generation_key=generation_key,
        focused_qa_task=task,
        originating_task=origin_item or task,
        organization=str(project.org_id) if (project and project.org_id) else '',
        environment=environment,
        observed_deployment_event=deployment_event,
        observed_build=(deployment_event.commit_sha if deployment_event else ''),
        trigger_identity=trigger,
        owning_project=project,
        origin_surface_schema=origin_surface_schema,
        origin_surface_key=origin_surface_key,
        metadata={
            'generator': 'focused-qa.v2',
            'risk_label': risk_label,
            'deployment_surface_key': surface_key or None,
        },
        create_task=False,
    )


def _focused_qa_generation_key(
    *,
    origin_item: Optional[Item],
    deployment_event: Optional[DeploymentEvent],
    risk_label: str,
    environment: str,
) -> str:
    """Deterministic generation key for the generating event (HLD §2.1).

    A deployment event is the strongest identity: the same deploy + risk + env
    retries to the same generation/task. Without a deployment event (e.g.
    status_transition / label_update triggers), the origin task + risk + env is
    the stable key.
    """
    if deployment_event is not None:
        return f'deploy:{deployment_event.pk}:{risk_label}:{environment}'
    origin = str(origin_item.pk) if origin_item else 'none'
    return f'origin:{origin}:{risk_label}:{environment}'


def _resolution_slug(item: Item) -> str:
    try:
        res = getattr(item, 'resolution', None)
        return (getattr(res, 'slug', '') or '').strip().lower()
    except Exception:
        return ''


def _is_non_fix_disposition(item: Item) -> bool:
    """PLS-300 gate A: True when the item was closed WITHOUT shipping a fix
    (rejected / won't-fix / FP / duplicate / accepted-risk) — nothing to verify."""
    if _resolution_slug(item) in NON_FIX_RESOLUTION_SLUGS:
        return True
    return str(getattr(item, 'status', '') or '').strip().lower() in {
        'rejected', 'wont_fix', 'duplicate', 'cancelled', 'canceled', 'deferred',
    }


def _is_static_scan_finding(item: Item) -> bool:
    """PLS-300 gate B: True for static-scan / dependency-CVE findings that have
    no live-verifiable surface (verified by the scanner/CI, not live QA)."""
    tokens = set(_split_tokens(getattr(item, 'labels', None))) | set(_split_tokens(getattr(item, 'tags', None)))
    if tokens & STATIC_SCAN_TOKENS:
        return True
    return str(getattr(item, 'source', '') or '').strip().lower().startswith('security-ci')


def _metadata_has_value(metadata: dict, keys: Set[str]) -> bool:
    """Return whether one of ``keys`` carries a concrete non-empty value."""
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif isinstance(value, (list, tuple, set, dict)):
            if value:
                return True
        elif value is not None and value is not False:
            return True
    return False


def _canonical_workflow_slug(item: Item) -> str:
    workflow_name = str(getattr(item, 'workflow_name', '') or '').strip()
    workflow = Workflow.resolve(workflow_name)
    if workflow:
        return workflow.slug.strip().lower()
    # Legacy/imported fixtures may reference a workflow before the definition is
    # present.  Normalize spelling, but keep the allow-list semantic and closed.
    return workflow_name.lower().replace('_', '-').replace(' ', '-')


def _is_live_testable_source(
    item: Optional[Item], *, trigger: str, deployment_event: Optional[DeploymentEvent] = None,
) -> bool:
    """Canonical PLS-571 positive-eligibility predicate for focused QA.

    A real deployment event is sufficient evidence.  Without one, development
    and bug work is eligible by typed item/workflow identity; other source
    classes require an explicit implementation artifact or live-target metadata.
    Risk-label presence and trigger names alone never establish eligibility.
    """
    if deployment_event is not None:
        return True
    if item is None:
        return False

    metadata = getattr(item, 'metadata', None) or {}
    labels_and_tags = (
        set(_split_tokens(getattr(item, 'labels', None)))
        | set(_split_tokens(getattr(item, 'tags', None)))
    )
    if labels_and_tags & NO_LIVE_MUTATION_TOKENS:
        return False

    item_type = str(getattr(item, 'item_type', '') or '').strip().lower()
    if any(item_type == prefix or item_type.startswith(f'{prefix}.')
           for prefix in IMPLEMENTATION_ITEM_TYPE_PREFIXES):
        return True
    if _canonical_workflow_slug(item) in DEVELOPMENT_WORKFLOW_SLUGS:
        return True
    if _metadata_has_value(metadata, IMPLEMENTATION_METADATA_KEYS):
        return True
    if _metadata_has_value(metadata, LIVE_TARGET_METADATA_KEYS):
        return True
    return False


def _is_process_meta_origin(item: Item) -> bool:
    """True for review/re-approve/deploy-verify bookkeeping — not a product surface."""
    title = str(getattr(item, 'title', '') or '').strip().lower()
    if not title:
        return False
    if any(title.startswith(prefix) for prefix in PROCESS_META_TITLE_PREFIXES):
        return True
    return 'record deploymentevent + qa verdict' in title


def _cancel_open_qa_for_origin(origin_item: Item) -> int:
    """Cancel leftover todo/in_progress focused-QA for a process-meta origin."""
    extras = Item.objects.filter(
        source='focused-qa-automation',
        metadata__focused_qa_origin_item_id=str(origin_item.pk),
    ).exclude(status__in=NON_ACTIVE_STATUSES)
    closed = 0
    for extra in extras:
        if extra.status not in ('todo', 'in_progress'):
            continue
        Item.objects.filter(pk=extra.pk).update(status='cancelled')
        closed += 1
    return closed


def _label_value(labels, key: str) -> str:
    if isinstance(labels, dict):
        return str(labels.get(key, '') or '').strip().lower()
    return ''


def _is_focused_qa_artifact(item: Item) -> bool:
    """PLS-317/PLS-370 gate: focused-QA must never recursively spawn QA.

    The predicate is identity-based and workflow-agnostic: a QA artifact is a QA
    artifact because the task says so (source/labels/tags/metadata/item_type or
    QA automation creator), not because it happened to travel through one
    workflow. Generated QA tasks and manual verification tasks can still carry
    the original risk labels (auth/security-sensitive/user-facing/etc.); those
    labels describe what QA should verify and are not authorization to create
    another QA task from the QA artifact itself.
    """
    source = str(getattr(item, 'source', '') or '').strip().lower()
    item_type = str(getattr(item, 'item_type', '') or '').strip().lower()
    workflow = str(getattr(item, 'workflow_name', '') or '').strip().lower().replace('_', '-')
    title = str(getattr(item, 'title', '') or '').strip().lower()
    metadata = getattr(item, 'metadata', None) or {}
    labels = getattr(item, 'labels', None)
    tokens = set(_split_tokens(labels)) | set(_split_tokens(getattr(item, 'tags', None)))
    label_source = _label_value(labels, 'source').replace('_', '-')
    created_by = str(getattr(item, 'created_by_email', '') or '').strip().lower()
    assignment_group = str(getattr(item, 'assignment_group', '') or '').strip().lower().replace('_', '-')

    if source.startswith('focused-qa'):
        return True
    if label_source.startswith('focused-qa'):
        return True
    if item_type == 'task.focused_qa':
        return True
    if any(str(k).startswith('focused_qa_') for k in metadata.keys()):
        return True
    if 'focused-qa' in tokens or 'focused_qa' in tokens:
        return True
    if title.startswith('[focused qa') or title.startswith('focused qa'):
        return True
    # Backstop for older reviewed-task QA artifacts whose explicit
    # focused-QA marker may be missing but which were still generated by the QA
    # automation lane. This keeps suppression tied to QA identity, not to every
    # reviewed-task item.
    if created_by == 'qa-automation@shizuha.com' and (workflow == 'reviewed-task' or assignment_group == 'qa'):
        return True
    return False


def _primary_risk_label(labels: Set[str]) -> str:
    for candidate in _RISK_LABEL_PRIORITY:
        if candidate in labels:
            return candidate
    return sorted(labels)[0]


def _split_tokens(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, dict):
        tokens = list(value.keys())
        # Also honor labels represented as {"risk": "auth"} or
        # {"requires-live-qa": true}; values can carry the actual label.
        tokens.extend(v for v in value.values() if isinstance(v, str))
        return [str(t).strip().lower() for t in tokens if str(t).strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(t).strip().lower() for t in value if str(t).strip()]
    return [t.strip().lower() for t in str(value).replace(';', ',').split(',') if t.strip()]


def risk_labels_for_item(item: Item) -> Set[str]:
    """Return high-risk labels present in an item's labels or tags."""
    tokens = set(_split_tokens(getattr(item, 'labels', None))) | set(_split_tokens(getattr(item, 'tags', None)))
    return tokens & HIGH_RISK_LABELS


def _qa_title(origin_item: Optional[Item], deployment_event: Optional[DeploymentEvent], risk_label: str, environment: str) -> str:
    if origin_item:
        source = origin_item.item_key or str(origin_item.pk)
        title = origin_item.title
    elif deployment_event:
        source = str(deployment_event.id)[:8]
        title = f"{deployment_event.service} {deployment_event.commit_sha[:12]}"
    else:
        source = 'unknown'
        title = 'unknown work'
    return f"[Focused QA:{risk_label}] {source} — {title} ({environment})"[:400]


def _deployment_qa_surfaces(deployment_event: Optional[DeploymentEvent]) -> List[str]:
    if not deployment_event:
        return []
    value = (deployment_event.metadata or {}).get('qa_surfaces') or []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(',')]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(surface).strip() for surface in value if str(surface).strip()]


def _expected_surface(origin_item: Optional[Item], deployment_event: Optional[DeploymentEvent], risk_label: str) -> str:
    if deployment_event:
        target = deployment_event.deployment_target or deployment_event.service
        ns = f" in namespace `{deployment_event.namespace}`" if deployment_event.namespace else ''
        surfaces = _deployment_qa_surfaces(deployment_event)
        required = f" Required operator surfaces: {', '.join(f'`{surface}`' for surface in surfaces)}." if surfaces else ''
        return f"Live `{deployment_event.environment}` deployment target `{target}`{ns}.{required}"
    if risk_label == 'auth':
        return 'Login/session/JWKS-protected API behavior touched by the originating task.'
    if risk_label == 'user-facing':
        return 'User-facing page/API flow described by the originating task.'
    if risk_label == 'agent-runtime':
        return 'Agent runtime / queue / Connect behavior described by the originating task.'
    if risk_label == 'data-migration':
        return 'Migrated data shape and read/write paths described by the originating task.'
    return 'Live surface described by the originating task and linked PR/deployment metadata.'


def _canonical_team_name(value: str) -> str:
    normalized = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    return TeamRoster._CANONICAL_ALIASES.get(normalized, normalized)


def _roster_member_emails(roster: Optional[TeamRoster]) -> set[str]:
    if not roster:
        return set()
    return {
        str(email or '').strip().lower()
        for email in TeamRoster._flat_member_emails(roster.tiers, roster.junior_emails)
        if str(email or '').strip()
    }


def _assignee_on_roster(email: Optional[str], roster: Optional[TeamRoster]) -> bool:
    """True when email is a live member of the exact-org roster (PLS-609)."""
    normalized = str(email or '').strip().lower()
    if not normalized:
        return False
    return normalized in _roster_member_emails(roster)


def _active_org_roster(project: Optional[Project], team_name: str) -> Optional[TeamRoster]:
    """Return a staffed roster only from the task's exact organization."""
    if not project or not project.org_id or not team_name:
        return None
    roster = TeamRoster.objects.filter(
        organization_id=project.org_id,
        team_name=_canonical_team_name(team_name),
        is_active=True,
    ).first()
    if not roster:
        return None
    if not TeamRoster._flat_member_emails(roster.tiers, roster.junior_emails):
        return None
    return roster


def _origin_team_roster(
    project: Optional[Project],
    origin_item: Optional[Item],
) -> Optional[TeamRoster]:
    if not project or not origin_item:
        return None
    roster = _active_org_roster(project, origin_item.assignment_group)
    if roster:
        return roster
    # Review transitions commonly replace the owning team with ``qa`` and
    # clear the assignee before focused-QA automation is retriggered.  The
    # originating reporter remains a valid exact-org ownership signal in that
    # state.  Never search outside the project's organization.
    local_identities = {
        str(email or '').strip().lower()
        for email in (
            origin_item.assignee_email,
            origin_item.created_by_email,
        )
        if str(email or '').strip()
    }
    if local_identities:
        for candidate in TeamRoster.objects.filter(
            organization_id=project.org_id,
            is_active=True,
        ):
            if local_identities & set(TeamRoster._flat_member_emails(
                candidate.tiers, candidate.junior_emails,
            )):
                return candidate
    return None


def _focused_qa_route(
    project: Optional[Project],
    origin_item: Optional[Item],
    preserved_assignment_group: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """Choose an org-local route without borrowing another organization's QA.

    Prefer a staffed QA team in the exact task organization.  When an
    organization has no QA team, keep verification with a real local owning
    team and mark the independence exception explicitly.  If no local team can
    be identified, leave the task honestly unassigned instead of minting a
    phantom ``qa`` queue.
    """
    qa_roster = _active_org_roster(project, 'qa')
    if qa_roster:
        return qa_roster.team_name, 'qa-roster'
    origin_roster = _origin_team_roster(project, origin_item)
    if origin_roster:
        return origin_roster.team_name, 'origin-team-fallback'
    # A repeated trigger must not erase a previously proven tenant-local route
    # merely because the source item has already entered its temporary QA
    # review lane.  Revalidate the stored group against the exact organization
    # before preserving it.
    preserved_roster = _active_org_roster(
        project, preserved_assignment_group or '',
    )
    if preserved_roster and _canonical_team_name(
        preserved_roster.team_name,
    ) != 'qa':
        return preserved_roster.team_name, 'origin-team-fallback'
    return None, 'unroutable-local'


def _qa_routing_notice(assignment_group: Optional[str], routing_mode: str) -> str:
    if routing_mode == 'qa-roster':
        return (
            'Routing boundary: assigned to the staffed QA team in this exact '
            'organization.'
        )
    if routing_mode == 'origin-team-fallback':
        return (
            'Routing boundary: this organization has no staffed QA team. To '
            'preserve tenant isolation, verification stays in the originating '
            f"organization's `{assignment_group}` team. This is a documented "
            'independence exception, not independent QA; retain that exception '
            'in the final readiness record.'
        )
    return (
        'Routing boundary: this organization has no staffed QA team and no '
        'routable local owning team could be proven. The task is intentionally '
        'left unassigned; do not borrow a team or agent from another '
        'organization.'
    )


def _with_current_routing_tags(tags: object, routing_mode: str) -> str:
    route_tags = {
        'origin-team-fallback': ['qa-independence-exception'],
        'unroutable-local': ['qa-routing-blocked'],
    }.get(routing_mode, [])
    preserved = set(_split_tokens(tags)) - FOCUSED_QA_ROUTING_TAGS
    return ','.join(sorted({*preserved, *route_tags}))


def _labels_with_routing_mode(labels: object, routing_mode: str) -> object:
    """Merge ``routing_mode`` into a labels value, preserving its shape.

    PLAT-9504: labels are deliberately dual-shape (PLS-712) — task items
    persist flat string arrays, alert producers persist key/value objects —
    and consumers must not assume either shape. Dict-splatting a list-shaped
    value raised ``TypeError: 'list' object is not a mapping`` and crashed the
    focused-QA reconcile on every pass (10-11 tracebacks per pod post-roll).
    """
    if isinstance(labels, Mapping):
        return {**labels, 'routing_mode': routing_mode}
    if isinstance(labels, (list, tuple)):
        return [*(label for label in labels if label != 'routing_mode'), routing_mode]
    return {'routing_mode': routing_mode}


def _as_mapping(value: object, *, item_key: str = '?', field: str = 'metadata') -> dict:
    """PLAT-9504: normalize a JSONField value before any ``{**...}`` unpack.

    A focused-QA candidate whose ``metadata`` holds a LIST raised
    ``TypeError: 'list' object is not a mapping`` on every sweep pass (the
    filter re-selects the same row regardless of metadata shape) — the
    per-pass crash storm. Normalize at the boundary and fail LOUD: a truthy
    non-mapping is logged with the item key so the poisoned row can be found
    and repaired; nothing is silently discarded.
    """
    if isinstance(value, dict):
        return value
    if value:
        logger.warning(
            'focused_qa: item %s field %s holds non-mapping %s — normalizing to {} '
            '(PLAT-9504; the row needs repair)',
            item_key, field, type(value).__name__,
        )
    return {}


def _qa_description(
    origin_item: Optional[Item],
    deployment_event: Optional[DeploymentEvent],
    risk_label: str,
    environment: str,
    trigger: str,
    assignment_group: Optional[str],
    routing_mode: str,
) -> str:
    source_bits = []
    if origin_item:
        source_bits.extend([
            f"- originating task: **{origin_item.item_key or origin_item.pk}** — {origin_item.title}",
            f"- originating workflow/status: `{origin_item.workflow_name or 'unknown'}` / `{origin_item.status}`",
        ])
        prs = (origin_item.metadata or {}).get('pull_requests') or []
        if prs:
            source_bits.append('- linked PRs: ' + ', '.join(str(p.get('url') or p.get('number')) for p in prs if isinstance(p, dict)))
    if deployment_event:
        source_bits.extend([
            f"- deployment event: `{deployment_event.id}`",
            f"- repo/service: `{deployment_event.repo}` / `{deployment_event.service}`",
            f"- commit: `{deployment_event.commit_sha}`",
            f"- image: `{deployment_event.image_digest or deployment_event.image_tag or deployment_event.image or 'unknown'}`",
            f"- deployment state/smoke: `{deployment_event.state}` / `{deployment_event.smoke_result}`",
        ])
        if deployment_event.pipeline_url:
            source_bits.append(f"- pipeline: {deployment_event.pipeline_url}")
        if deployment_event.logs_url:
            source_bits.append(f"- logs: {deployment_event.logs_url}")
    return "\n".join([
        f"Focused live QA was requested automatically because risk label `{risk_label}` matched trigger `{trigger}`.",
        '',
        f"Environment: `{environment}`",
        f"Expected surface: {_expected_surface(origin_item, deployment_event, risk_label)}",
        _qa_routing_notice(assignment_group, routing_mode),
        '',
        'Source context:',
        *(source_bits or ['- no source context provided']),
        '',
        'Evidence required (QA Operating Model):',
        '- date/time and target environment',
        '- exact commit/image/build if relevant',
        '- user/account/agent used for the test',
        '- commands, URLs, or browser paths exercised',
        '- expected result and observed result',
        '- screenshots/log excerpts when UI or runtime behavior matters',
        '- linked owning-project `autonomous-bug` for every failure (do not reopen the dev task automatically)',
    ])


def _deployment_surface_key(deployment_event: Optional[DeploymentEvent]) -> str:
    """Stable surface identity for latest-wins coalescing (PLS-592 / PLS-593).

    Not DeploymentEvent id — successive builds of the same service/env share one
    active Focused-QA unit.
    """
    if not deployment_event:
        return ''
    repo = str(getattr(deployment_event, 'repo', '') or '').strip().lower()
    service = str(getattr(deployment_event, 'service', '') or '').strip().lower()
    env = str(getattr(deployment_event, 'environment', '') or 'live').strip().lower()
    target = str(getattr(deployment_event, 'deployment_target', '') or '').strip().lower()
    ns = str(getattr(deployment_event, 'namespace', '') or '').strip().lower()
    return '|'.join([repo, service, env, target, ns])


def _conflicted_verifier_emails(
    origin_item: Optional[Item],
    *,
    actor_email: Optional[str] = None,
    existing_metadata: Optional[dict] = None,
) -> Set[str]:
    """Emails that must never receive Focused-QA of this origin (SoD / PLS-592)."""
    conflicted: Set[str] = set()
    if actor_email and str(actor_email).strip():
        conflicted.add(str(actor_email).strip().lower())
    if origin_item is not None:
        for attr in ('assignee_email', 'created_by_email'):
            val = getattr(origin_item, attr, None)
            if val and str(val).strip():
                conflicted.add(str(val).strip().lower())
        # PLS-592: verification participation is not equivalent to current
        # assignment.  A reviewer can advance the origin through a review
        # status without ever owning it, so consume the typed lifecycle audit
        # rather than inferring authority from free-text comments.
        if getattr(origin_item, 'pk', None):
            participants = ItemActivity.objects.filter(
                item_id=origin_item.pk,
                action='status_changed',
                field_name__iexact='Status',
            ).values_list('user_email', flat=True)
            for email in participants:
                if email and str(email).strip():
                    conflicted.add(str(email).strip().lower())
        # PLS-1336 (fingerprint QaRosterOverridesSoDDirectAssignment): a
        # delivery may record its SoD boundary explicitly — approver-scope /
        # fixture parties typed under ``sod_excluded_verifier_emails`` on the
        # origin item. Score them as conflicted so the focused-QA resolver can
        # never re-land the lap on them, whatever the capacity picture. The
        # 2026-07-21 and 2026-09-23 observations both had the fixture party
        # recorded only in prose, which the router cannot score; capacity was
        # the sole constraint and the lap ping-ponged back to the excluded
        # principal once their seat freed up. Recording the boundary under this
        # typed key makes the exclusion first-class (PLS-1336 fix direction 1).
        origin_meta = getattr(origin_item, 'metadata', None) if origin_item else None
        if isinstance(origin_meta, dict):
            for email in origin_meta.get('sod_excluded_verifier_emails') or []:
                if email and str(email).strip():
                    conflicted.add(str(email).strip().lower())
    meta = existing_metadata or {}
    for email in meta.get('focused_qa_conflicted_emails') or []:
        if email and str(email).strip():
            conflicted.add(str(email).strip().lower())
    conflicted.discard('')
    conflicted.discard('qa-automation@shizuha.com')
    return conflicted


def _origin_assignee_email(origin_item: Optional[Item]) -> Optional[str]:
    """Return the deterministic distinguished identity for resolver fallback.

    ``exclude_email`` has author-specific fallback semantics in the shared
    resolver, so it must never be selected from the unordered conflict set.
    The full conflict set is supplied independently via hard_exclude_emails.
    """
    email = getattr(origin_item, 'assignee_email', None) if origin_item else None
    normalized = str(email or '').strip().lower()
    return normalized or None


def _find_open_qa_task(origin_item: Optional[Item], deployment_event: Optional[DeploymentEvent], risk_label: str, environment: str) -> Optional[Item]:
    qs = Item.objects.filter(
        source='focused-qa-automation',
        metadata__focused_qa_environment=environment,
    ).exclude(status__in=NON_ACTIVE_STATUSES)
    if origin_item:
        # PLS-300: one task per origin+env. Primary risk_label is display
        # identity, not a second mint key — a later status_transition /
        # deployment_event that adds requires-live-qa (priority 1) on top of
        # an existing auth/data-migration task must UPDATE that task, not
        # open a sibling. Live 2026-08-29: 8 origin pairs (e.g. CON-327)
        # minted both `[Focused QA:data-migration]` and `[Focused QA:requires-live-qa]`
        # and pushed source-flood over the 100/24h cap.
        qs = qs.filter(metadata__focused_qa_origin_item_id=str(origin_item.pk))
        # Oldest mint is the identity; later sequential labels UPDATE it.
        return qs.order_by('created_at').first()
    else:
        if deployment_event:
            # PLS-592: coalesce deployment-only QA by surface key (repo/service/
            # env/target/ns), not per DeploymentEvent id — successive builds of
            # the same surface must update one active unit (latest-wins).
            surface = _deployment_surface_key(deployment_event)
            if surface:
                by_surface = qs.filter(
                    metadata__focused_qa_deployment_surface_key=surface,
                ).order_by('-created_at').first()
                if by_surface:
                    return by_surface
            # Fallback: exact event id (legacy rows without surface key).
            qs = qs.filter(metadata__focused_qa_deployment_event_id=str(deployment_event.id))
        else:
            qs = qs.filter(metadata__focused_qa_origin_item_id__isnull=True)
            qs = qs.filter(metadata__focused_qa_deployment_event_id__isnull=True)
            qs = qs.filter(metadata__focused_qa_risk_label=risk_label)
    return qs.order_by('-created_at').first()


def _collapse_open_qa_siblings(
    keeper: Item,
    origin_item: Optional[Item],
    deployment_event: Optional[DeploymentEvent],
    environment: str,
) -> int:
    """Cancel leftover open siblings for the same origin+env or deploy surface.

    PLS-300 / PLS-592: the finder returns one keeper, but historical sequential
    labels and pre-surface-key deploys left extras in todo/in_progress. Live
    2026-08-29: 8 origin pairs + 6 surface groups. Operator: cancel the trash.
    in_review/blocked extras are left for the typed Close Duplicate path.
    """
    qs = Item.objects.filter(
        source='focused-qa-automation',
        metadata__focused_qa_environment=environment,
    ).exclude(status__in=NON_ACTIVE_STATUSES).exclude(pk=keeper.pk)
    if origin_item:
        extras = qs.filter(metadata__focused_qa_origin_item_id=str(origin_item.pk))
    elif deployment_event:
        surface = _deployment_surface_key(deployment_event)
        if not surface:
            return 0
        extras = qs.filter(
            metadata__focused_qa_origin_item_id__isnull=True,
            metadata__focused_qa_deployment_surface_key=surface,
        )
    else:
        return 0
    closed = 0
    for extra in extras:
        if extra.status not in ('todo', 'in_progress'):
            continue
        # Queryset.update skips status-coercion on save() (tests without the
        # full focused-qa status inventory would bounce cancelled -> todo).
        Item.objects.filter(pk=extra.pk).update(status='cancelled')
        try:
            ItemLink.objects.get_or_create(
                source_item=extra,
                target_item=keeper,
                link_type='duplicates',
                defaults={
                    'created_by_id': 0,
                    'created_by_email': 'qa-automation@shizuha.com',
                },
            )
        except Exception:
            pass
        closed += 1
    return closed


def _project_for(origin_item: Optional[Item], deployment_event: Optional[DeploymentEvent]) -> Optional[Project]:
    if origin_item and origin_item.project_id:
        return origin_item.project
    if deployment_event and deployment_event.project_id:
        return deployment_event.project
    return Project.objects.filter(key__iexact='PLS', is_active=True).first() or Project.objects.filter(is_active=True).order_by('id').first()


@transaction.atomic
def ensure_focused_qa_task(
    *,
    origin_item: Optional[Item] = None,
    deployment_event: Optional[DeploymentEvent] = None,
    risk_label: str,
    all_risk_labels: Optional[Iterable[str]] = None,
    environment: str = 'live',
    trigger: str = 'risk_label',
    actor_email: Optional[str] = None,
) -> Item:
    risk_label = str(risk_label).strip().lower()
    environment = (environment or 'live').strip().lower()
    # PLS-300: one task per origin+env. `risk_label` is the ranked PRIMARY
    # display label, not a second mint key; all matched labels ride along as
    # tags/metadata so QA sees every surface without N duplicate tasks.
    label_set = sorted({str(l).strip().lower() for l in (all_risk_labels or [risk_label]) if str(l).strip()})
    existing = _find_open_qa_task(origin_item, deployment_event, risk_label, environment)
    if existing is None and deployment_event is not None:
        # PLS-592 guard: the open-task dedupe excludes closed tasks, so re-ingest
        # of the SAME deploy (same event id) would re-mint a fresh focused-QA task
        # once the prior one is closed — an infinite QA-of-QA loop (observed on
        # dojo UX closes). If this exact origin+risk+env already produced a task
        # for this deployment event, it was already queued/verified; skip creation.
        closed_for_same_event = Item.objects.filter(
            source='focused-qa-automation',
            metadata__focused_qa_risk_label=risk_label,
            metadata__focused_qa_environment=environment,
            metadata__focused_qa_deployment_event_id=str(deployment_event.id),
        )
        if origin_item:
            closed_for_same_event = closed_for_same_event.filter(
                metadata__focused_qa_origin_item_id=str(origin_item.pk),
            )
        else:
            closed_for_same_event = closed_for_same_event.filter(
                metadata__focused_qa_origin_item_id__isnull=True,
            )
        existing = closed_for_same_event.order_by('-created_at').first()
    project = _project_for(origin_item, deployment_event)
    existing_metadata = _as_mapping(existing.metadata if existing else None,
                                    item_key=str(getattr(existing, 'pk', '')
                                                 or getattr(origin_item, 'item_key', '') or '?'),
                                    field='metadata') if existing else {}
    if existing:
        prior = set(existing_metadata.get('focused_qa_risk_labels') or [])
        prior_primary = str(existing_metadata.get('focused_qa_risk_label') or '').strip()
        if prior_primary:
            prior.add(prior_primary)
        label_set = sorted({str(x).strip().lower() for x in (*label_set, *prior) if str(x).strip()})
        if label_set:
            risk_label = _primary_risk_label(set(label_set))
    preserved_assignment_group = None
    if existing_metadata.get('focused_qa_routing_mode') in {
        'origin-team-fallback', 'unroutable-local',
    }:
        preserved_assignment_group = (
            existing_metadata.get('focused_qa_assignment_group')
            or existing.assignment_group
        )
    assignment_group, routing_mode = _focused_qa_route(
        project,
        origin_item,
        preserved_assignment_group=preserved_assignment_group,
    )
    conflicted = _conflicted_verifier_emails(
        origin_item,
        actor_email=actor_email,
        existing_metadata=existing_metadata,
    )
    surface_key = _deployment_surface_key(deployment_event)
    metadata = {
        'focused_qa_origin_item_id': str(origin_item.pk) if origin_item else None,
        'focused_qa_origin_item_key': origin_item.item_key if origin_item else None,
        'focused_qa_deployment_event_id': str(deployment_event.id) if deployment_event else None,
        'focused_qa_deployment_surface_key': surface_key or None,
        'focused_qa_risk_label': risk_label,
        'focused_qa_risk_labels': label_set,
        'focused_qa_environment': environment,
        'focused_qa_trigger': trigger,
        'focused_qa_routing_mode': routing_mode,
        'focused_qa_assignment_group': assignment_group,
        # PLS-592: persistent SoD exclusion for every writer (create/update/reconcile).
        'focused_qa_conflicted_emails': sorted(conflicted),
        'focused_qa_origin_verifier_identity': sorted(conflicted),
    }
    title = _qa_title(origin_item, deployment_event, risk_label, environment)
    description = _qa_description(
        origin_item,
        deployment_event,
        risk_label,
        environment,
        trigger,
        assignment_group,
        routing_mode,
    )
    if existing:
        existing.title = title
        existing.description = description
        route_changed = existing.assignment_group != assignment_group
        existing.assignment_group = assignment_group
        if route_changed:
            existing.assignee_id = None
            existing.assignee_email = None
        # PLS-825: non-terminal focused-QA items migrate to the dedicated
        # focused-qa workflow once the rollout gate is green (HLD §2, §6 step 6).
        # Terminal historical items stay immutable on their current workflow.
        if existing.status not in NON_ACTIVE_STATUSES:
            existing.workflow_name = _focused_qa_workflow_name()
        existing.metadata = {**_as_mapping(existing.metadata, item_key=str(existing.pk)), **metadata}
        existing.labels = _labels_with_routing_mode(existing.labels, routing_mode)
        existing.tags = _with_current_routing_tags(
            existing.tags, routing_mode,
        )
        existing.priority = 'high'
        existing.save(update_fields=[
            'title', 'description', 'assignment_group', 'assignee_id',
            'assignee_email', 'workflow_name', 'metadata', 'labels', 'tags',
            'priority', 'updated_at',
        ])
        current_assignee = str(existing.assignee_email or '').strip().lower()
        needs_reassign = route_changed or (
            current_assignee and current_assignee in conflicted
        ) or (not current_assignee and assignment_group)
        if needs_reassign and assignment_group:
            try:
                from tasks.services.workflow_service import WorkflowService
                uid, email = WorkflowService._resolve_team_assignee(
                    assignment_group,
                    exclude_email=_origin_assignee_email(origin_item),
                    hard_exclude_emails=conflicted,
                    enforce_cap=True,
                    project_key=(project.key if project else None),
                    item=existing,
                )
                if uid or email:
                    existing.assign({'id': uid, 'email': email}, save=True)
                    meta = _as_mapping(existing.metadata, item_key=str(existing.pk))
                    meta.pop('focused_qa_sod_hold', None)
                    existing.metadata = meta
                    existing.save(update_fields=['metadata', 'updated_at'])
                elif conflicted:
                    # PLS-592: no safe peer — leave group-queued (SoD hold), never
                    # assign a conflicted origin verifier.
                    existing.assignee_id = None
                    existing.assignee_email = None
                    existing.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
                    meta = {**_as_mapping(existing.metadata, item_key=str(existing.pk)), 'focused_qa_sod_hold': True}
                    existing.metadata = meta
                    existing.save(update_fields=['metadata', 'updated_at'])
            except Exception:
                logger.warning(
                    'Focused QA route updated but assignee lease failed for %s',
                    existing.item_key or existing.pk,
                    exc_info=True,
                )
        qa = existing
        if deployment_event:
            Comment.objects.get_or_create(
                item=qa,
                author_id=0,
                author_email='qa-automation@shizuha.com',
                content=(
                    f"Focused QA source updated from DeploymentEvent `{deployment_event.id}` "
                    f"({deployment_event.repo}/{deployment_event.service} @ {deployment_event.commit_sha[:12]})."
                ),
            )
    else:
        qa_workflow = _focused_qa_workflow_name()
        qa = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key() if project else None,
            mode='task',
            item_type='task.focused_qa',
            title=title,
            description=description,
            source='focused-qa-automation',
            source_id=':'.join(str(v) for v in [metadata['focused_qa_origin_item_id'] or 'none', metadata['focused_qa_deployment_event_id'] or 'none', risk_label, environment]),
            status='todo',
            priority='high',
            assignment_group=assignment_group,
            workflow_name=qa_workflow,
            created_by_id=0,
            created_by_email='qa-automation@shizuha.com',
            labels={
                'risk_label': risk_label,
                'risk_labels': label_set,
                'environment': environment,
                'source': 'focused-qa',
                'routing_mode': routing_mode,
            },
            tags=_with_current_routing_tags(
                ','.join(['focused-qa', *label_set, environment]),
                routing_mode,
            ),
            metadata=metadata,
        )
        # PLS-825 (HLD §2.1): when the dedicated workflow is active, bind one
        # immutable generation row so retries of the same generating event are
        # idempotent. The legacy reviewed-task path binds no generation.
        if qa_workflow == FOCUSED_QA_WORKFLOW_SLUG:
            _bind_focused_qa_generation(
                task=qa,
                origin_item=origin_item,
                deployment_event=deployment_event,
                project=project,
                risk_label=risk_label,
                environment=environment,
                trigger=trigger,
                generation_key=_focused_qa_generation_key(
                    origin_item=origin_item,
                    deployment_event=deployment_event,
                    risk_label=risk_label,
                    environment=environment,
                ),
            )
        try:
            from tasks.services.workflow_service import WorkflowService
            if assignment_group:
                uid, email = WorkflowService._resolve_team_assignee(
                    assignment_group,
                    exclude_email=_origin_assignee_email(origin_item),
                    hard_exclude_emails=conflicted,
                    enforce_cap=True,
                    project_key=(project.key if project else None),
                    item=qa,
                )
                if uid or email:
                    qa.assign({'id': uid, 'email': email}, save=True)
                elif conflicted:
                    # PLS-592: group-queue with typed SoD hold rather than self-review.
                    meta = {**_as_mapping(qa.metadata, item_key=str(qa.pk)), 'focused_qa_sod_hold': True}
                    qa.metadata = meta
                    qa.save(update_fields=['metadata', 'updated_at'])
        except Exception:
            pass
        Comment.objects.create(
            item=qa,
            author_id=0,
            author_email='qa-automation@shizuha.com',
            content=f"Auto-created focused QA task for risk label `{risk_label}` via `{trigger}`. Repeated triggers update this task instead of creating duplicates.",
        )
    _collapse_open_qa_siblings(
        keeper=qa,
        origin_item=origin_item,
        deployment_event=deployment_event,
        environment=environment,
    )
    if origin_item:
        try:
            ItemLink.objects.get_or_create(
                source_item=origin_item,
                target_item=qa,
                link_type='relates_to',
                defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
            )
        except Exception:
            pass
    if deployment_event and deployment_event.qa_task_id != qa.pk:
        deployment_event.qa_task = qa
        deployment_event.save(update_fields=['qa_task', 'updated_at'])
    return qa


@transaction.atomic
def reconcile_focused_qa_routes(*, limit: int = 100) -> int:
    """Repair open focused-QA tasks that point at a nonexistent local QA team.

    This is intentionally event/sweep driven, org-local, capped, and idempotent.
    It never imports a QA member from another organization.
    """
    candidates = list(
        Item.objects
        .filter(source='focused-qa-automation')
        .exclude(status__in=NON_ACTIVE_STATUSES)
        .filter(
            Q(assignment_group='qa')
            | Q(assignment_group='')
            | Q(assignment_group__isnull=True)
            | Q(metadata__focused_qa_routing_mode__in=[
                'origin-team-fallback', 'unroutable-local',
            ])
        )
        .select_for_update(of=('self',), skip_locked=True)
        .select_related('project')
        .order_by('created_at')[:max(1, int(limit))]
    )
    changed = 0
    for candidate in candidates:
        current_mode = (candidate.metadata or {}).get('focused_qa_routing_mode')
        current_group = _canonical_team_name(candidate.assignment_group)
        # Review/merge may legitimately move a completed QA result out of the
        # QA execution lane. Only repair a phantom QA route or a prior fallback.
        if current_group not in {'', 'qa'} and current_mode not in {
            'origin-team-fallback', 'unroutable-local',
        }:
            continue
        origin_item = _origin_item_for_qa(candidate)
        assignment_group, routing_mode = _focused_qa_route(
            candidate.project,
            origin_item,
            preserved_assignment_group=(
                (candidate.metadata or {}).get(
                    'focused_qa_assignment_group',
                )
                or candidate.assignment_group
            ),
        )
        route_changed = not (
            candidate.assignment_group == assignment_group
            and current_mode == routing_mode
        )
        # PLS-609: if a human already direct-assigned a staffed member of the
        # target boundary team, do not clear them and re-pick least-loaded.
        # That override discarded SoD handoffs (PLAT-4948 → zen) and re-landed
        # the conflicted party.
        boundary_roster = (
            _active_org_roster(candidate.project, assignment_group or '')
            if assignment_group
            else None
        )
        assignee_in_boundary = _assignee_on_roster(
            candidate.assignee_email, boundary_roster,
        )
        if route_changed:
            candidate.assignment_group = assignment_group
            if not assignee_in_boundary:
                candidate.assignee_id = None
                candidate.assignee_email = None
            candidate.metadata = {
                **_as_mapping(candidate.metadata, item_key=str(candidate.pk)),
                'focused_qa_routing_mode': routing_mode,
                'focused_qa_assignment_group': assignment_group,
            }
            candidate.labels = _labels_with_routing_mode(candidate.labels, routing_mode)
            candidate.tags = _with_current_routing_tags(
                candidate.tags, routing_mode,
            )
            update_fields = [
                'assignment_group', 'metadata', 'labels', 'tags', 'updated_at',
            ]
            if not assignee_in_boundary:
                update_fields.extend(['assignee_id', 'assignee_email'])
            candidate.save(update_fields=update_fields)
        lease_acquired = False
        conflicted = _conflicted_verifier_emails(
            origin_item,
            existing_metadata=_as_mapping(candidate.metadata, item_key=str(candidate.pk)),
        )
        current = str(candidate.assignee_email or '').strip().lower()
        if current and current in conflicted:
            candidate.assignee_id = None
            candidate.assignee_email = None
            candidate.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
            # The PLS-609 retention decision was made before the SoD fence
            # cleared this owner. Do not let that stale verdict suppress the
            # replacement resolver pass added by PLS-592.
            assignee_in_boundary = False
        if (
            assignment_group
            and not candidate.assignee_id
            and not candidate.assignee_email
            and not assignee_in_boundary
        ):
            try:
                from tasks.services.workflow_service import WorkflowService
                uid, email = WorkflowService._resolve_team_assignee(
                    assignment_group,
                    exclude_email=_origin_assignee_email(origin_item),
                    hard_exclude_emails=conflicted,
                    enforce_cap=True,
                    project_key=candidate.project.key,
                    item=candidate,
                )
                if uid or email:
                    lease_acquired = candidate.assign(
                        {'id': uid, 'email': email},
                        save=True,
                    )
                elif conflicted:
                    meta = {
                        **_as_mapping(candidate.metadata, item_key=str(candidate.pk)),
                        'focused_qa_sod_hold': True,
                        'focused_qa_conflicted_emails': sorted(conflicted),
                        'focused_qa_origin_verifier_identity': sorted(conflicted),
                    }
                    candidate.metadata = meta
                    candidate.save(update_fields=['metadata', 'updated_at'])
            except Exception:
                logger.warning(
                    'Focused QA route resolved but assignee lease failed for %s',
                    candidate.item_key or candidate.pk,
                    exc_info=True,
                )
        if not route_changed and not lease_acquired:
            continue
        if route_changed and not assignee_in_boundary:
            Comment.objects.create(
                item=candidate,
                author_id=0,
                author_email='qa-automation@shizuha.com',
                content=_qa_routing_notice(assignment_group, routing_mode),
            )
        changed += 1
    return changed


@transaction.atomic
def ensure_focused_qa_for_item(
    item: Item,
    *,
    trigger: str = 'risk_label',
    deployment_event: Optional[DeploymentEvent] = None,
    actor_email: Optional[str] = None,
) -> List[Item]:
    # PLS-300 kill switch (interim containment).
    if not _focused_qa_enabled():
        return []
    if _is_focused_qa_artifact(item):
        logger.info("PLS-317: skip focused-QA for %s — source item is itself a focused-QA artifact",
                    getattr(item, 'item_key', item.pk))
        return []
    if _is_process_meta_origin(item):
        logger.info(
            "skip focused-QA for %s — process-meta origin (review/re-approve/deploy-verify)",
            getattr(item, 'item_key', item.pk),
        )
        _cancel_open_qa_for_origin(item)
        return []
    if not _is_live_testable_source(item, trigger=trigger, deployment_event=deployment_event):
        logger.info(
            "PLS-571: skip focused-QA for %s — no implementation, deployment, or explicit live target",
            getattr(item, 'item_key', item.pk),
        )
        return []
    labels = risk_labels_for_item(item)
    if not labels:
        return []
    # PLS-300 gate A: a finding closed WITHOUT a fix (rejected/won't-fix/FP/dup)
    # ships nothing to live-verify. A real deployment_event means something WAS
    # shipped, so the reject-gate is skipped on that path.
    if deployment_event is None and _is_non_fix_disposition(item):
        logger.info("PLS-300: skip focused-QA for %s — non-fix disposition (%s/%s)",
                    getattr(item, 'item_key', item.pk), _resolution_slug(item), getattr(item, 'status', ''))
        return []
    # PLS-300 gate B: static-scan / dependency-CVE findings verify via scanner/CI,
    # not live QA — the live-surface remediation rides the owning DEV task.
    if deployment_event is None and _is_static_scan_finding(item):
        logger.info("PLS-300: skip focused-QA for %s — static-scan/dep finding (no live surface)",
                    getattr(item, 'item_key', item.pk))
        return []
    # PLS-592: no-pr / cadence process tasks without a delivery generation must
    # not mint QA-of-QA (label_update alone is not live evidence).
    if deployment_event is None and trigger in {'label_update', 'status_transition', 'risk_label'}:
        tokens = set()
        for raw in (getattr(item, 'labels', None), getattr(item, 'tags', None)):
            if isinstance(raw, dict):
                tokens |= {str(k).lower() for k in raw.keys()}
                tokens |= {str(v).lower() for v in raw.values() if isinstance(v, str)}
            elif isinstance(raw, (list, tuple, set)):
                tokens |= {str(v).lower() for v in raw}
            elif raw:
                tokens |= {p.strip().lower() for p in str(raw).split(',') if p.strip()}
        if 'no-pr' in tokens or 'qa-cadence' in tokens:
            logger.info(
                "PLS-592: skip focused-QA for %s — no-pr/cadence without delivery generation",
                getattr(item, 'item_key', item.pk),
            )
            return []
    environment = (deployment_event.environment if deployment_event else None) or 'live'
    # Prefer explicit actor; fall back to current origin assignee (verifier).
    actor = actor_email or getattr(item, 'assignee_email', None)
    # PLS-300: coalesce to ONE task per finding — primary label is the dedupe
    # identity, all matched labels ride along as tags/metadata.
    return [
        ensure_focused_qa_task(
            origin_item=item, deployment_event=deployment_event,
            risk_label=_primary_risk_label(labels), all_risk_labels=labels,
            environment=environment, trigger=trigger,
            actor_email=actor,
        )
    ]


def ensure_focused_qa_for_deployment_event(event: DeploymentEvent, *, trigger: str = 'deployment_event') -> List[Item]:
    # PLS-300 kill switch (interim containment).
    if not _focused_qa_enabled():
        return []
    created = []
    source_keys = [str(k).strip() for k in (event.source_task_keys or []) if str(k).strip()]
    event_risks = set(_split_tokens((event.metadata or {}).get('risk_labels'))) & HIGH_RISK_LABELS
    source_items = list(Item.objects.filter(item_key__in=source_keys))
    for item in source_items:
        if _is_focused_qa_artifact(item):
            logger.info("PLS-317: skip focused-QA deployment source %s — source item is itself a focused-QA artifact",
                        getattr(item, 'item_key', item.pk))
            continue
        if _is_process_meta_origin(item):
            logger.info(
                "skip focused-QA deployment source %s — process-meta origin",
                getattr(item, 'item_key', item.pk),
            )
            _cancel_open_qa_for_origin(item)
            continue
        if not _is_live_testable_source(item, trigger=trigger, deployment_event=event):
            continue
        # Deployment metadata can carry the risk label when the source item was
        # not explicitly labelled yet. A real deployment IS live-verifiable, so no
        # reject/static-scan gate here — but PLS-300 still coalesces all matched
        # labels into ONE origin-backed task (primary label = dedupe identity).
        labels = risk_labels_for_item(item) | event_risks
        if not labels:
            continue
        created.append(
            ensure_focused_qa_task(
                origin_item=item,
                deployment_event=event,
                risk_label=_primary_risk_label(labels),
                all_risk_labels=labels,
                environment=event.environment,
                trigger=trigger,
            )
        )
    if (not source_items and event_risks
            and _is_live_testable_source(None, trigger=trigger, deployment_event=event)):
        created.append(ensure_focused_qa_task(
            deployment_event=event, risk_label=_primary_risk_label(event_risks),
            all_risk_labels=event_risks, environment=event.environment, trigger=trigger,
        ))
    return created


def _origin_item_for_qa(qa_task: Item) -> Optional[Item]:
    meta = qa_task.metadata or {}
    origin_id = meta.get('focused_qa_origin_item_id')
    if origin_id:
        try:
            return Item.objects.filter(pk=int(origin_id)).first()
        except (TypeError, ValueError):
            return None
    return None


def _find_open_qa_failure_bug(qa_task: Item) -> Optional[Item]:
    return (
        Item.objects
        .filter(
            source='focused-qa-failure',
            metadata__focused_qa_task_id=str(qa_task.pk),
        )
        .exclude(status__in=NON_ACTIVE_STATUSES)
        .order_by('-created_at')
        .first()
    )


def create_bug_from_qa_failure(qa_task: Item, *, title: str, reproduction: str, evidence_url: str = '') -> Item:
    """Helper for QA failure automation: file an owning-project autonomous bug.

    The focused QA task stays as evidence; the new bug is what engineering fixes.
    """
    meta = qa_task.metadata or {}
    origin_item = _origin_item_for_qa(qa_task)
    project = (origin_item.project if origin_item and origin_item.project_id else None) or qa_task.project
    origin_group = (origin_item.assignment_group if origin_item and origin_item.assignment_group else '')
    # Verification/review lanes identify failures; they do not own implementation.
    # Preserve a concrete product owner (identity, devops, security, etc.), but
    # route QA/review/merge/docs artifacts to Engineering instead of bouncing the
    # new autonomous bug back into a non-implementation queue.
    assignment_group = (
        origin_group
        if origin_group not in {'', 'qa', 'review', 'merge', 'documentation'}
        else 'engineering'
    )
    description = "\n".join([
        f"QA failure found from **{qa_task.item_key or qa_task.pk}**.",
        '',
        reproduction,
        '',
        f"Evidence: {evidence_url or 'see QA task comments'}",
    ])
    if origin_item:
        description += f"\n\nOriginating task: **{origin_item.item_key or origin_item.pk}** — {origin_item.title}"
    metadata = {
        'focused_qa_task_id': str(qa_task.pk),
        'focused_qa_task_key': qa_task.item_key,
        'focused_qa_origin_item_id': str(origin_item.pk) if origin_item else meta.get('focused_qa_origin_item_id'),
        'focused_qa_origin_item_key': (origin_item.item_key if origin_item else meta.get('focused_qa_origin_item_key')),
        'focused_qa_risk_label': meta.get('focused_qa_risk_label', ''),
        'focused_qa_environment': meta.get('focused_qa_environment', ''),
    }
    existing = _find_open_qa_failure_bug(qa_task)
    if existing:
        existing.title = title[:400]
        existing.description = description
        existing.source_url = evidence_url or existing.source_url
        existing.priority = qa_task.priority or 'high'
        existing.assignment_group = assignment_group
        existing.workflow_name = 'autonomous-bug'
        existing.metadata = {**_as_mapping(existing.metadata, item_key=str(existing.pk)), **metadata}
        existing.save(update_fields=['title', 'description', 'source_url', 'priority', 'assignment_group', 'workflow_name', 'metadata', 'updated_at'])
        return existing

    bug = Item.objects.create(
        project=project,
        item_key=project.get_next_item_key() if project else None,
        mode='task',
        item_type='bug.qa_failure',
        title=title[:400],
        description=description,
        source='focused-qa-failure',
        source_id=str(qa_task.pk),
        source_url=evidence_url or None,
        status='needs_triage',
        priority=qa_task.priority or 'high',
        assignment_group=assignment_group,
        workflow_name='autonomous-bug',
        created_by_id=0,
        created_by_email='qa-automation@shizuha.com',
        labels={'source': 'focused-qa-failure', 'risk_label': meta.get('focused_qa_risk_label', '')},
        tags='focused-qa-failure,qa',
        metadata=metadata,
    )
    ItemLink.objects.get_or_create(
        source_item=qa_task,
        target_item=bug,
        link_type='relates_to',
        defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
    )
    if origin_item:
        ItemLink.objects.get_or_create(
            source_item=origin_item,
            target_item=bug,
            link_type='relates_to',
            defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
        )
    Comment.objects.create(
        item=bug,
        author_id=0,
        author_email='qa-automation@shizuha.com',
        content=f"Auto-created from focused QA failure on **{qa_task.item_key or qa_task.pk}**. Repeated QA-failure transitions update this bug instead of creating duplicates.",
    )
    return bug
