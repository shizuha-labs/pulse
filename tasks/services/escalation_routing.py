"""Typed, atomic operator-bound escalation routing (PLAT-4609).

The old PLAT-1347 gate inferred routing from mutable prose after the workflow
transition had already saved and run assignment post-functions.  This service
is the exclusive mutation boundary for operator-bound transitions: validated
typed evidence, one locked transaction, one disposition, one actor-attributed
receipt, and a durable owner-attention outbox.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from ..models import (
    Comment,
    EscalationBlockerDependency,
    EscalationRoutingDisposition,
    EscalationRoutingNotificationOutbox,
    EscalationRoutingReceipt,
    EscalationRoutingRequest,
    Item,
    ItemLink,
    RoutingTeam,
)
from ..secret_scanner import has_secrets
from .permissions import PermissionService


logger = logging.getLogger(__name__)

SCHEMA = "shizuha.pulse.escalation-evidence.v1"
TOP_KEYS = {"schema", "request_id", "expected_generation", "dependency", "technical_action"}
DEPENDENCY_KEYS = {
    "evidence_class", "requested_kind", "requested_subject", "holder_type",
    "holder_ref", "acceptance_check", "source_refs",
}
TECHNICAL_KEYS = {
    "action", "service_ref", "independently_executable", "independence_evidence",
    "relationship", "acceptance_check", "source_refs",
}

OPERATOR_CLASSES = {
    "human_held_source_evidence",
    "operator_decision",
    "operator_held_credential_access",
}
EVIDENCE_CLASSES = OPERATOR_CLASSES | {
    "human_physical_action",
    "internal_capability_gap",
}
REQUESTED_KINDS = {
    "fact", "artifact", "signoff", "decision", "credential_access",
    "physical_action", "technical_action",
}
HOLDER_TYPES = {
    "operator", "admin_ops", "external_authority", "service_team",
    "physical_operator",
}
OPERATOR_TEAMS = frozenset({"admin-ops", "human-ops", "ceo-decisions"})
BLOCKER_RESULTS = ("mint", "reuse", "suppress_team")

SCHEMA_REPAIR_METADATA_VERSION = "v2"


class _PlanFailure(Exception):
    """Expected failure while materializing a disposition plan."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _PreparedEvidence:
    request_id: uuid.UUID
    source_generation: int
    canonical_without_request: str
    normalized: dict
    validation_reason: str = ""


@dataclass(frozen=True)
class _Plan:
    classification: str
    outcome: str
    reason_code: str
    target_status: str
    target_group: str
    catalog_service_ref: str = ""
    catalog_generation: int | None = None
    technical_action: dict | None = None
    allow_group_owned_destination: bool = False


@dataclass(frozen=True)
class _BlockerDecision:
    result: str = ""
    dependency_key: str = ""
    blocker: Item | None = None
    parent_group: str = ""


def _canonical(value: Any) -> str:
    """Deterministic JSON used for semantic request identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_error_evidence(schema: str, reason: str) -> dict:
    return {
        "schema": schema if isinstance(schema, str) else "",
        "validation_error": reason,
    }


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _all_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _all_strings(child)


def _parse_request_id(raw: Any, *, fallback_material: str) -> tuple[uuid.UUID, str]:
    try:
        return uuid.UUID(str(raw)), ""
    except (TypeError, ValueError, AttributeError):
        # Invalid envelopes still commit one deterministic safe HOLD rather than
        # creating a fresh side effect on each retry.
        return uuid.uuid5(uuid.NAMESPACE_URL, f"pulse-escalation:{fallback_material}"), "request_id_invalid"


def _bounded_string(value: Any, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value.strip()) <= maximum


def _resolve_source_refs(item: Item, user, refs: Any) -> tuple[list[dict], str]:
    if not isinstance(refs, list) or not refs:
        return [], "source_refs_required"

    resolved: list[dict] = []
    org_id = item.project.org_id if item.project_id else None
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"kind", "id"}:
            return [], "source_ref_malformed"
        kind = str(ref.get("kind") or "")
        raw_id = ref.get("id")
        source_item = None
        opaque_id = None

        if kind == "task":
            query = Q(item_key=str(raw_id))
            try:
                query |= Q(pk=int(raw_id))
            except (TypeError, ValueError):
                pass
            source_item = Item.objects.filter(query).select_related("project").first()
            opaque_id = str(source_item.pk) if source_item else None
        elif kind == "comment":
            try:
                comment = Comment.objects.select_related("item__project").get(pk=int(raw_id))
            except (Comment.DoesNotExist, TypeError, ValueError):
                comment = None
            source_item = comment.item if comment else None
            opaque_id = str(comment.pk) if comment else None
        else:
            return [], "source_ref_kind_unknown"

        if source_item is None or not source_item.project_id:
            return [], "source_ref_not_found"
        if source_item.project.org_id != org_id:
            return [], "source_ref_cross_org"

        same_item = source_item.pk == item.pk
        linked = same_item or ItemLink.objects.filter(
            Q(source_item=item, target_item=source_item)
            | Q(source_item=source_item, target_item=item)
        ).exists()
        if not linked:
            return [], "source_ref_not_linked"
        if not same_item and not PermissionService.get_user_item_queryset(
            user, organization_id=org_id
        ).filter(pk=source_item.pk).exists():
            return [], "source_ref_forbidden"
        resolved.append({"kind": kind, "id": opaque_id})
    return resolved, ""


def _prepare_evidence(item: Item, user, raw: Any) -> _PreparedEvidence:
    raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    raw_for_digest = copy.deepcopy(raw)
    raw_for_digest.pop("request_id", None)
    canonical_material = _canonical(raw_for_digest)
    fallback = hashlib.sha256(canonical_material.encode()).hexdigest()
    request_id, request_reason = _parse_request_id(raw.get("request_id"), fallback_material=fallback)

    expected = raw.get("expected_generation", 0)
    expected_reason = ""
    if isinstance(expected, bool):
        expected_reason = "expected_generation_invalid"
        expected = 0
    else:
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            expected_reason = "expected_generation_invalid"
            expected = 0
    if expected < 0:
        expected_reason = "expected_generation_invalid"
        expected = 0

    schema = raw.get("schema") if isinstance(raw.get("schema"), str) else ""
    reason = ""
    if not isinstance(raw, dict) or not raw:
        reason = "missing_evidence_schema"
    elif set(raw) != TOP_KEYS:
        reason = "evidence_keys_invalid"
    elif schema != SCHEMA:
        reason = "missing_evidence_schema" if not schema else "unknown_evidence_schema"
    if not reason:
        reason = request_reason or expected_reason

    # Never persist credential material even in a typed HOLD.
    if any(has_secrets(value) for value in _all_strings(raw)):
        reason = "secret_material_forbidden"

    dep = raw.get("dependency")
    tech = raw.get("technical_action")
    normalized: dict = _bounded_error_evidence(schema, reason) if reason else {}

    if not reason:
        if not isinstance(dep, dict) or set(dep) != DEPENDENCY_KEYS:
            reason = "dependency_malformed"
        elif dep.get("evidence_class") not in EVIDENCE_CLASSES:
            reason = "evidence_class_invalid"
        elif dep.get("requested_kind") not in REQUESTED_KINDS:
            reason = "requested_kind_invalid"
        elif dep.get("holder_type") not in HOLDER_TYPES:
            reason = "holder_type_invalid"
        elif not _bounded_string(dep.get("requested_subject"), minimum=1, maximum=400):
            reason = "requested_subject_invalid"
        elif not _bounded_string(dep.get("holder_ref"), minimum=1, maximum=200):
            reason = "holder_ref_invalid"
        elif not _bounded_string(dep.get("acceptance_check"), minimum=1, maximum=1000):
            reason = "acceptance_check_invalid"

    if not reason:
        klass = dep["evidence_class"]
        holder = dep["holder_type"]
        kind = dep["requested_kind"]
        operator_holder = holder in {"operator", "admin_ops", "external_authority"}
        valid_pair = (
            (klass in OPERATOR_CLASSES and operator_holder)
            or (klass == "human_physical_action" and holder == "physical_operator" and kind == "physical_action")
            or (klass == "internal_capability_gap" and holder == "service_team" and kind == "technical_action")
        )
        if not valid_pair:
            reason = "evidence_holder_conflict"

    dep_refs: list[dict] = []
    if not reason:
        dep_refs, reason = _resolve_source_refs(item, user, dep.get("source_refs"))

    normalized_tech = None
    if not reason and tech is not None:
        if not isinstance(tech, dict) or set(tech) != TECHNICAL_KEYS:
            reason = "technical_action_malformed"
        elif dep["evidence_class"] not in OPERATOR_CLASSES:
            reason = "technical_action_without_operator_dependency"
        elif tech.get("independently_executable") is not True:
            reason = "technical_action_independence_unproven"
        elif tech.get("relationship") not in {"blocks", "relates_to"}:
            reason = "technical_action_relationship_invalid"
        elif not _bounded_string(tech.get("action"), minimum=1, maximum=1000):
            reason = "technical_action_invalid"
        elif not _bounded_string(tech.get("service_ref"), minimum=1, maximum=200):
            reason = "technical_service_ref_invalid"
        elif not _bounded_string(tech.get("independence_evidence"), minimum=1, maximum=1000):
            reason = "technical_independence_evidence_invalid"
        elif not _bounded_string(tech.get("acceptance_check"), minimum=1, maximum=1000):
            reason = "technical_acceptance_check_invalid"
        else:
            tech_refs, reason = _resolve_source_refs(item, user, tech.get("source_refs"))
            if not reason:
                normalized_tech = {**tech, "source_refs": tech_refs}

    if not reason:
        normalized = {
            "schema": SCHEMA,
            "expected_generation": expected,
            "dependency": {**dep, "source_refs": dep_refs},
            "technical_action": normalized_tech,
        }
    else:
        normalized = _bounded_error_evidence(schema, reason)

    return _PreparedEvidence(
        request_id=request_id,
        source_generation=expected,
        canonical_without_request=canonical_material,
        normalized=normalized,
        validation_reason=reason,
    )


def _actor_identity(item: Item, user) -> tuple[str, int | None, str]:
    org_id = item.project.org_id if item.project_id else 0
    raw_id = getattr(user, "id", None)
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError):
        user_id = None
    email = str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip().lower()
    if user_id is not None:
        return f"org:{org_id}:user:{user_id}", user_id, email
    email_digest = hashlib.sha256(email.encode()).hexdigest()
    return f"org:{org_id}:subject:{email_digest}", None, email


def _digests(item: Item, transition, prepared: _PreparedEvidence, actor_subject: str) -> tuple[str, str]:
    base = f"{item.pk}|{transition.id}|{prepared.source_generation}|{prepared.canonical_without_request}"
    disposition = hashlib.sha256(base.encode()).hexdigest()
    assertion = hashlib.sha256(f"{item.pk}|{transition.id}|{prepared.source_generation}|{actor_subject}|{prepared.canonical_without_request}".encode()).hexdigest()
    return assertion, disposition


def _resolve_catalog(item: Item, service_ref: str) -> tuple[str, int]:
    key = str(service_ref or "").strip().lower()
    if ":" in key:
        key = key.rsplit(":", 1)[-1]
    key = key.replace("_", "-").replace(" ", "-")
    team = RoutingTeam.objects.filter(
        organization_id=item.project.org_id,
        key=key,
        state=RoutingTeam.STATE_ACTIVE,
    ).first()
    if not team:
        raise _PlanFailure("catalog_service_unresolved")
    return team.key, int(team.version)


def _state(item: Item) -> dict:
    return {
        "status": item.status,
        "workflow_status": item.workflow_status,
        "assignment_group": item.assignment_group or "",
        "assignee_id": item.assignee_id,
        "assignee_email": item.assignee_email or "",
    }


def _plan_for(item: Item, transition, prepared: _PreparedEvidence, screen_data: dict) -> _Plan:
    from .workflow_service import WorkflowService, WorkflowValidationError

    def operator_decision_group() -> str:
        """Route by the escalation the actor fired, inside this organization.

        Raise to Admin Ops is the admin-ops lane. Escalate to Human Ops is
        the physical lane. Escalate to Leads, and any other operator-held
        ask, stays on ceo-decisions. The evidence class must not rewrite an
        Admin Ops or Human Ops escalation onto the CEO.
        """
        name = str(getattr(transition, "name", "") or "").strip().lower()
        if name == "raise to admin ops":
            team = "admin-ops"
        elif name == "escalate to human ops":
            team = "human-ops"
        else:
            team = "ceo-decisions"
        if WorkflowService._team_registered_for_item(team, item):
            return team
        org_id = int(getattr(getattr(item, "project", None), "org_id", 0) or 0)
        # This is an authoritative fail-closed contract error, not a classifier
        # failure.  WorkflowValidationError escapes ``execute`` without entering
        # the generic forced-HOLD path, preserving the transaction unchanged.
        raise WorkflowValidationError(
            f"Cannot block {item.item_key or item.pk}: no active exact-org "
            f"escalation team exists for organization_id={org_id}, "
            f"team=`{team}`. Parent status, child, receipt, and link were left "
            "unchanged.",
            transition_id=str(getattr(transition, "id", "") or ""),
            code="BLOCKER_TEAM_UNREGISTERED",
        )

    if prepared.validation_reason:
        hold_group = (
            item.assignment_group
            if item.assignment_group and item.assignee_id and item.assignee_email
            else "admin-ops"
        )
        return _Plan(
            "mixed_or_unknown_escalation_hold", "held",
            prepared.validation_reason, "blocked", hold_group,
        )

    text = " ".join([
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "item_type", "") or ""),
    ]).lower()
    request_text = WorkflowService._escalation_gate_explicit_request_text(screen_data)
    dep_subject = ""
    if isinstance(prepared.normalized.get("dependency"), dict):
        dep_subject = str(prepared.normalized["dependency"].get("requested_subject") or "")
    violations = WorkflowService._escalation_gate_hygiene_violations(
        item, text, request_text, evidence_subject=dep_subject,
    )
    if violations:
        return _Plan(
            "operator_request_hygiene_reject", "rejected",
            "operator_request_hygiene_reject:" + ",".join(violations),
            item.status, item.assignment_group or "",
        )

    dep = prepared.normalized["dependency"]
    klass = dep["evidence_class"]
    tech = prepared.normalized.get("technical_action")
    if klass in OPERATOR_CLASSES and tech:
        # 2026-09-24: a technical child is the owning team's work. The operator
        # does not hold the blocked parent just because a seat classified the
        # evidence as operator-held. A pure decision/credential with no
        # executable technical action still uses operator_decision_group().
        team, generation = _resolve_catalog(item, tech["service_ref"])
        return _Plan(
            "operator_held_with_technical_child", "routed", "validated_mixed_evidence",
            transition.to_status, team, team, generation, tech,
            True,
        )
    if klass in OPERATOR_CLASSES:
        return _Plan(
            "operator_held_dependency", "routed", "validated_operator_held",
            transition.to_status, operator_decision_group(),
            allow_group_owned_destination=True,
        )
    if klass == "human_physical_action":
        return _Plan(
            "human_physical_action", "routed", "validated_physical_action",
            transition.to_status, "human-ops",
            allow_group_owned_destination=True,
        )
    if klass == "internal_capability_gap":
        team, generation = _resolve_catalog(item, dep["holder_ref"])
        return _Plan(
            "internal_capability_gap", "rerouted", "validated_internal_capability",
            WorkflowService._ready_status_for_item(item), team, team, generation,
        )
    return _Plan(
        "mixed_or_unknown_escalation_hold", "held", "classification_unknown",
        "blocked", item.assignment_group or "admin-ops",
    )


def _assign_group(
    item: Item,
    group: str,
    *,
    actor,
    operator_authorized: bool = False,
    allow_group_owned: bool = False,
) -> None:
    from .workflow_service import WorkflowService

    item.assignment_group = group
    update_fields = ["assignment_group"]
    uid, email = WorkflowService._resolve_team_assignee(
        group,
        exclude_email=item.created_by_email,
        project_key=WorkflowService._item_project_key(item),
        item=item,
        allow_operator=operator_authorized,
    )
    if not uid or not email:
        # PLAT-7292: operator-bound teams (human-ops / admin-ops / ceo-decisions)
        # are allowed to stay group-queued when no live member is resolvable.
        # Raising here aborted the typed escalation AND the fail-loud HOLD
        # (503 escalation_hold_commit_failed) because the HOLD retried the
        # same unresolved assignee lookup.
        team_registered = WorkflowService._team_registered_for_item(group, item)
        if (
            allow_group_owned
            or WorkflowService._canonical_team_name(group) in OPERATOR_TEAMS
        ) and team_registered:
            item.assignee_id = None
            item.assignee_email = None
            item._activity_actor = actor
            return
        raise _PlanFailure("target_assignee_unresolved")
    item._activity_actor = actor
    WorkflowService._apply_resolved_assignee(
        item, uid, email, update_fields, operator_authorized=operator_authorized,
    )


def _restore_pre_state(item: Item, pre: dict) -> None:
    item.status = pre["status"]
    item.workflow_status = pre["workflow_status"]
    item.assignment_group = pre["assignment_group"] or None
    item.assignee_id = pre["assignee_id"]
    item.assignee_email = pre["assignee_email"] or None


def _canonicalize_hold_plan(item: Item, plan: _Plan) -> _Plan:
    """Resolve one exact-org owner for both sides of a fail-loud HOLD."""
    if (
        plan.classification != "mixed_or_unknown_escalation_hold"
        or plan.target_status != "blocked"
    ):
        return plan

    from .workflow_service import WorkflowService

    team = WorkflowService._canonical_team_name(plan.target_group or "admin-ops")
    if not WorkflowService._team_registered_for_item(team, item):
        team = "admin-ops"
    return replace(plan, target_group=team)


def _dependency_key(item: Item, plan: _Plan, prepared: _PreparedEvidence | None) -> str:
    """Return a stable, content-free key shared by equivalent operator asks.

    A valid typed dependency envelope yields a CONTENT key shared across parents
    escalating the same ask, including rollback and forced-hold recovery. A
    forced hold with no valid envelope still gets a stable PER-ITEM fallback key
    so repeated recovery for that parent dedupes to one operator child instead
    of bypassing the mint/reuse/suppress gate. The fallback is namespaced
    (``forced_hold``) so it can never collide with a structured content key.
    """
    from .workflow_service import WorkflowService

    target_team = WorkflowService._canonical_team_name(plan.target_group)
    if target_team not in OPERATOR_TEAMS:
        return ""

    def normalized_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    if prepared is not None and not prepared.validation_reason:
        dependency = prepared.normalized.get("dependency") or {}
        if dependency:
            material = {
                "organization_id": item.project.org_id,
                "target_team": target_team,
                "evidence_class": dependency.get("evidence_class"),
                "requested_kind": dependency.get("requested_kind"),
                "requested_subject": normalized_text(dependency.get("requested_subject")),
                "holder_type": dependency.get("holder_type"),
                "holder_ref": normalized_text(dependency.get("holder_ref")),
            }
            return hashlib.sha256(_canonical(material).encode()).hexdigest()
        # A valid envelope carrying no dependency block does not dedupe (unchanged).
        return ""

    fallback = {
        "forced_hold": True,
        "organization_id": item.project.org_id,
        "target_team": target_team,
        "item": item.pk,
    }
    return hashlib.sha256(_canonical(fallback).encode()).hexdigest()


def _is_active_task(item: Item) -> bool:
    from .workflow_service import WorkflowService

    return (
        (item.mode or "").strip().lower() == "task"
        and not WorkflowService._status_releases_blocks(item.status)
    )


def _is_operator_blocker(item: Item) -> bool:
    from .workflow_service import WorkflowService

    return WorkflowService._canonical_team_name(item.assignment_group) in OPERATOR_TEAMS


def _legacy_blocker_matches_dependency(
    blocker: Item,
    prepared: _PreparedEvidence | None,
) -> bool:
    """Allow a legacy claim only when its prose identifies the same typed ask.

    Pre-PLS-530 blockers do not carry the dependency key.  The accepted design
    permits a same-team fallback only at high title/dependency similarity; a
    same-team link alone is deliberately insufficient because false reuse hides
    the real dependency.  Exact subject containment covers the old blocker
    template, while the token score tolerates its ``[BLOCKER] PLS-N:`` prefix.
    """
    if prepared is None or prepared.validation_reason:
        return False
    dependency = prepared.normalized.get("dependency") or {}
    subject = str(dependency.get("requested_subject") or "").strip().lower()
    if not subject:
        return False

    def normalized(value: Any) -> str:
        text = re.sub(r"\[[^\]]+\]", " ", str(value or "").lower())
        text = re.sub(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d+\b", " ", text)
        return " ".join(re.findall(r"[a-z0-9]+", text))

    expected = normalized(subject)
    candidate_title = normalized(blocker.title)
    candidate_body = normalized(blocker.description)
    expected_tokens = set(expected.split())
    title_tokens = set(candidate_title.split())
    if len(expected_tokens) < 3:
        return False
    if expected and (
        expected in candidate_title
        or expected in candidate_body
    ):
        return True
    if not title_tokens:
        return False
    overlap = len(expected_tokens & title_tokens) / len(expected_tokens | title_tokens)
    return overlap >= 0.75


def _is_current_transition_blocker(blocker: Item, item: Item, transition) -> bool:
    """Whether main's PLS-597 repair child belongs to this routing attempt."""
    expected = f"{item.pk}:{getattr(transition, 'id', '') or ''}"
    return (blocker.metadata or {}).get("auto_blocker_intent") == expected


def _link_reused_blocker(blocker: Item, parent: Item, *, user) -> None:
    from .workflow_service import WorkflowService

    if ItemLink.objects.filter(
        source_item=blocker, target_item=parent, link_type="blocks",
    ).exists():
        return
    if WorkflowService.would_create_block_cycle(blocker.pk, parent.pk):
        raise _PlanFailure("BLOCKER_CYCLE")
    link = ItemLink(
        source_item=blocker,
        target_item=parent,
        link_type="blocks",
        created_by_id=getattr(user, "id", None) or 0,
        created_by_email=(
            getattr(user, "email", None)
            or getattr(user, "username", None)
            or "workflow@pulse"
        ),
    )
    link._skip_auto_block = True
    link.save()


def _reconcile_reused_blocker_owner(
    blocker: Item,
    *,
    parent: Item,
    plan: _Plan,
    transition,
    user,
    team_override: str | None = None,
) -> Item:
    """Lock and restore the canonical owner before a blocker is reused.

    Registry and linked candidates may outlive a direct-assignee clear.  Reuse
    must therefore cross the same team resolver and guarded assignee-write
    boundary as a newly prepared blocker; otherwise the parent transaction
    selects the candidate only for the blocked-entry postcondition to reject it.
    """
    from .workflow_service import WorkflowService, WorkflowValidationError

    blocker = Item.objects.select_for_update().get(pk=blocker.pk)
    team = WorkflowService._canonical_team_name(
        team_override
        if team_override is not None
        else (plan.target_group or blocker.assignment_group),
    )
    existing_email = (blocker.assignee_email or "").strip()
    if (
        blocker.assignee_id
        and existing_email
        and WorkflowService._is_team_member(existing_email, team, item=blocker)
    ):
        # Reuse preserves a complete, team-valid owner.  Re-running the live
        # resolver here can churn an in-flight dependency to a least-loaded
        # peer, or reject a valid blocker merely because the resolver is
        # temporarily unavailable.  Only canonicalize the lane label.
        if blocker.assignment_group != team:
            blocker.assignment_group = team
            blocker.save(update_fields=["assignment_group", "updated_at"])
        return blocker

    # PLS-860 item 8: compute allow_operator via the same chokepoint helper as
    # _create_escalation_blocker (closes WS/ER frozenset divergence).
    allow_operator = WorkflowService._is_operator_capable_team(team)
    org_id = int(getattr(getattr(blocker, "project", None), "org_id", 0) or 0)
    transition_id = str(getattr(transition, "id", "") or "")

    def _park_group_owned(*, cause: str) -> Item:
        """PLS-860: park a registered-team reused blocker as group-owned."""
        if not WorkflowService._team_registered_for_item(team, blocker):
            raise WorkflowValidationError(
                f"Cannot block {parent.item_key or parent.pk}: escalation team "
                f"`{team}` did not resolve a concrete reused-blocker assignee. "
                "Parent status, blocker, receipt, and link were left unchanged.",
                transition_id=transition_id,
                code="BLOCKER_ASSIGNEE_UNAVAILABLE",
            )
        blocker.assignment_group = team
        blocker.assignee_id = None
        blocker.assignee_email = None
        metadata = dict(blocker.metadata or {})
        metadata.update({
            "auto_blocker_team": team,
            "escalation_group_owned_wait": True,
            "escalation_group_owned_org_id": org_id,
            "escalation_group_owned_cause": cause,
        })
        blocker.metadata = metadata
        blocker._activity_actor = (
            getattr(user, "id", None) or 0,
            getattr(user, "email", None)
            or getattr(user, "username", None)
            or "workflow@pulse",
        )
        blocker.save(update_fields=[
            "assignment_group", "assignee_id", "assignee_email",
            "metadata", "updated_at",
        ])
        return blocker

    uid, email = WorkflowService._resolve_team_assignee(
        team,
        enforce_cap=False,
        project_key=WorkflowService._item_project_key(blocker),
        allow_operator=allow_operator,
        item=blocker,
    )
    if not uid or not (email or "").strip():
        metadata = blocker.metadata if isinstance(blocker.metadata, dict) else {}
        already_group_owned = (
            bool(metadata.get("escalation_group_owned_wait"))
            and not blocker.assignee_id
            and not (blocker.assignee_email or "").strip()
            and int(metadata.get("escalation_group_owned_org_id") or 0) == org_id
            and WorkflowService._team_registered_for_item(team, blocker)
        )
        if already_group_owned:
            if blocker.assignment_group != team:
                blocker.assignment_group = team
                blocker.save(update_fields=["assignment_group", "updated_at"])
            return blocker
        # PLS-860 item 6: a previously-owned reused blocker MAY become
        # group-owned when election is empty (same rationale as #180 stage-1).
        return _park_group_owned(cause="election_empty")

    blocker.assignment_group = team
    blocker._activity_actor = (
        getattr(user, "id", None) or 0,
        getattr(user, "email", None)
        or getattr(user, "username", None)
        or "workflow@pulse",
    )
    update_fields = ["assignment_group"]
    try:
        WorkflowService._apply_resolved_assignee(
            blocker,
            uid,
            email,
            update_fields,
            operator_authorized=allow_operator,
        )
    except WorkflowValidationError as exc:
        code = str(getattr(exc, "code", "") or "")
        if code.startswith("routing_authority:"):
            # PLS-860 item 10: routing-authority raise is a write refusal.
            return _park_group_owned(cause="assignee_write_refused")
        raise
    # PLS-860 stage 1 at ER:720 — write refused after successful election.
    # Item 12: guard on assignee_id, never `if not applied:` alone.
    if not blocker.assignee_id or not (blocker.assignee_email or "").strip():
        return _park_group_owned(cause="assignee_write_refused")
    metadata = dict(blocker.metadata or {})
    metadata["auto_blocker_team"] = team
    metadata.pop("escalation_group_owned_wait", None)
    metadata.pop("escalation_group_owned_cause", None)
    blocker.metadata = metadata
    update_fields.append("metadata")
    blocker.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])
    return blocker


def _record_blocker_decision(parent: Item, decision: _BlockerDecision) -> None:
    if not decision.result:
        return
    result = decision.result
    key = decision.dependency_key
    parent_label = parent.item_key or parent.pk
    blocker_label = decision.blocker.item_key if decision.blocker else ""
    transaction.on_commit(lambda: logger.info(
        "escalation_blocker_pre_mint result=%s dependency_key=%s parent=%s blocker=%s",
        result, key, parent_label, blocker_label,
    ))


def _schema_repair_notice(item: Item, reason: str, expected_generation: int) -> str:
    parent = item.item_key or str(item.pk)
    return f"""\
## Escalation evidence repair required

This is an agent-doable typed-evidence repair, not the holder-controlled
dependency itself. Pulse rejected the parent transition with
`{reason}`. Release this repair blocker, let **{parent}** auto-resume, then
retry the operator-bound transition with a fresh UUID and this complete shape:

```json
{{
  "schema": "{SCHEMA}",
  "request_id": "<fresh UUID>",
  "expected_generation": {expected_generation},
  "dependency": {{
    "evidence_class": "human_held_source_evidence",
    "requested_kind": "artifact",
    "requested_subject": "<one exact atomic ask>",
    "holder_type": "operator",
    "holder_ref": "<exact holder>",
    "acceptance_check": "<observable completion check>",
    "source_refs": [{{"kind": "task", "id": "{parent}"}}]
  }},
  "technical_action": null
}}
```

The dependency object must contain all seven fields shown above, with no extra
keys. `source_refs` is a non-empty list of `{{"kind", "id"}}` objects, not a
list of task-key strings. The lane follows the transition you retry.
Raise to Admin Ops stays on this organization's `admin-ops` team.
Escalate to Human Ops stays on `human-ops`. Escalate to Leads stays on
`ceo-decisions`. The evidence class does not move an Admin Ops or Human
Ops escalation onto the CEO.

**Acceptance:** the retry returns `operator_held_dependency` / `routed`, names
the actual child key in its receipt, and assigns that child to the team
named by the transition.

---

"""


def _annotate_schema_repair_blocker(
    blocker: Item,
    item: Item,
    *,
    reason: str,
    expected_generation: int,
) -> None:
    """Make every typed-evidence HOLD self-repairing for the owning agent."""
    repair_marker = "## Escalation evidence repair required"
    repair_separator = "\n---\n\n"
    title = blocker.title or ""
    while title.startswith("[SCHEMA REPAIR: "):
        prefix_end = title.find("] ")
        if prefix_end < 0:
            break
        title = title[prefix_end + 2:]

    description = blocker.description or ""
    if description.startswith(repair_marker) and repair_separator in description:
        description = description.split(repair_separator, 1)[1]

    if not reason:
        metadata = dict(blocker.metadata or {})
        metadata.pop("escalation_validation_reason", None)
        changed_fields = []
        for key in ("escalation_schema_repair", "escalation_expected_generation"):
            metadata.pop(key, None)
        if metadata != (blocker.metadata or {}):
            blocker.metadata = metadata
            changed_fields.append("metadata")
        if title != (blocker.title or ""):
            blocker.title = title
            changed_fields.append("title")
        if description != (blocker.description or ""):
            blocker.description = description
            changed_fields.append("description")
        if changed_fields:
            blocker.save(update_fields=[*changed_fields, "updated_at"])
        return

    metadata = dict(blocker.metadata or {})
    metadata.update({
        "escalation_schema_repair": SCHEMA_REPAIR_METADATA_VERSION,
        "escalation_validation_reason": reason,
        "escalation_expected_generation": expected_generation,
    })
    title = f"[SCHEMA REPAIR: {reason}] {title}"
    notice = _schema_repair_notice(item, reason, expected_generation)
    description = notice + description

    blocker.title = title[:400]
    blocker.description = description
    blocker.metadata = metadata
    blocker.save(update_fields=["title", "description", "metadata", "updated_at"])


def _prepare_required_blocker(
    item: Item, plan: _Plan, transition, *, pre: dict, user, screen_data: dict,
    prepared: _PreparedEvidence | None = None,
    repair_reason: str = "", expected_generation: int = 0,
) -> _BlockerDecision:
    """Materialize the PLAT-4215 blocker before a typed plan enters blocked.

    PLS-525 / PLS-491 Option A: every path that will set parent ``blocked`` must
    mint+link an owned blocker child in the same transaction *before*
    ``item.save`` (``Item._validate_blocked_entry`` / PLAT-4215). When the
    transition lacks a ``create_blocker_task`` post-function (common on
    mixed_or_unknown_escalation_hold / forced-hold recovery), synthesize a
    blocker config for the canonical, validated plan owner (falling back to
    admin-ops) rather than failing open into a ValidationError on parent save.
    """
    if plan.target_status != "blocked":
        return _BlockerDecision()

    from .workflow_service import WorkflowService

    # PLS-710: carry the validated typed ask (requested_subject / acceptance_check)
    # through to the child mint so the holder sees the actual request. The ask
    # lives in prepared.normalized["dependency"]; on the fail-loud recovery path
    # prepared is preserved unless the dependency registry identity itself was
    # corrupt (dependency_mismatch), in which case no validated ask exists.
    ask = None
    if prepared is not None and isinstance(prepared.normalized, dict):
        dependency = prepared.normalized.get('dependency')
        if isinstance(dependency, dict) and (
            str(dependency.get('requested_subject') or '').strip()
            or str(dependency.get('acceptance_check') or '').strip()
        ):
            ask = dependency

    dependency_key = _dependency_key(item, plan, prepared)
    if dependency_key:
        active_linked = WorkflowService._active_blockers_for(item)
        # A validated typed dependency is independent of every other blocker
        # already attached to the parent.  Only fail-loud recovery retains the
        # older team-leaf suppression rule: using it for valid operator evidence
        # collapses distinct dependency identities merely because they share a
        # parent (PLS-905).
        allow_team_leaf_suppression = (
            prepared is None
            or prepared.validation_reason
            or plan.classification == "mixed_or_unknown_escalation_hold"
        )
        team_leaf = next(
            (blocker for blocker in active_linked
             if _is_active_task(blocker)
             and not _is_operator_blocker(blocker)
             and not _is_current_transition_blocker(blocker, item, transition)),
            None,
        ) if allow_team_leaf_suppression else None
        if team_leaf is not None:
            # A group-owned leaf is a valid PLAT-4215 blocker shape, but this
            # typed transition commits a concrete-owner postcondition. Lock and
            # repair the leaf through its OWN canonical team resolver before
            # suppressing the operator mint. Using plan.target_group here would
            # incorrectly rehome an Engineering dependency to Admin Ops.
            team_leaf = _reconcile_reused_blocker_owner(
                team_leaf,
                parent=item,
                plan=plan,
                transition=transition,
                user=user,
                team_override=team_leaf.assignment_group,
            )
            decision = _BlockerDecision(
                "suppress_team",
                dependency_key,
                team_leaf,
                WorkflowService._canonical_team_name(team_leaf.assignment_group),
            )
            _record_blocker_decision(item, decision)
            return decision

        dependency, _ = EscalationBlockerDependency.objects.get_or_create(
            organization_id=item.project.org_id,
            dependency_key=dependency_key,
            defaults={"target_team": plan.target_group},
        )
        dependency = EscalationBlockerDependency.objects.select_for_update().get(
            pk=dependency.pk,
        )
        expected_team = WorkflowService._canonical_team_name(plan.target_group)
        registered_team = WorkflowService._canonical_team_name(dependency.target_team)
        if registered_team != expected_team:
            raise _PlanFailure("operator_dependency_registry_team_mismatch")
        canonical = dependency.blocker
        # PLS-994: a dependency whose registered blocker IS the item being
        # re-raised (a waiting_external blocker escalated again) cannot be
        # reused as its own blocker child — linking it would create a
        # self-cycle (BLOCKER_CYCLE). Skip reuse and mint a fresh child so the
        # re-raise commits its own owned operator hold.
        if canonical and _is_active_task(canonical) and canonical.pk != item.pk:
            canonical_team = WorkflowService._canonical_team_name(
                canonical.assignment_group,
            )
            canonical_key = (canonical.metadata or {}).get(
                "escalation_blocker_dependency_key",
            )
            canonical_org_id = (
                canonical.project.org_id if canonical.project_id else 0
            )
            if canonical_org_id != item.project.org_id:
                raise _PlanFailure("operator_dependency_child_org_mismatch")
            if canonical_key != dependency_key:
                raise _PlanFailure("operator_dependency_child_key_mismatch")
            if canonical_team != expected_team:
                raise _PlanFailure("operator_dependency_child_team_mismatch")
            canonical = _reconcile_reused_blocker_owner(
                canonical,
                parent=item,
                plan=plan,
                transition=transition,
                user=user,
            )
            _link_reused_blocker(canonical, item, user=user)
            decision = _BlockerDecision("reuse", dependency_key, canonical)
            _record_blocker_decision(item, decision)
            return decision

        keyed_linked = [
            blocker for blocker in active_linked
            if _is_active_task(blocker)
            and blocker.pk != item.pk
            and (blocker.metadata or {}).get("escalation_blocker_dependency_key")
            == dependency_key
        ]
        for candidate in keyed_linked:
            candidate_org_id = (
                candidate.project.org_id if candidate.project_id else 0
            )
            if candidate_org_id != item.project.org_id:
                raise _PlanFailure("operator_dependency_child_org_mismatch")
            if WorkflowService._canonical_team_name(
                candidate.assignment_group,
            ) != expected_team:
                raise _PlanFailure("operator_dependency_child_team_mismatch")
        linked_operator = keyed_linked[0] if keyed_linked else None
        if linked_operator is not None:
            linked_operator = _reconcile_reused_blocker_owner(
                linked_operator,
                parent=item,
                plan=plan,
                transition=transition,
                user=user,
            )
            dependency.blocker = linked_operator
            dependency.target_team = plan.target_group
            dependency.save(update_fields=["blocker", "target_team", "updated_at"])
            decision = _BlockerDecision("reuse", dependency_key, linked_operator)
            _record_blocker_decision(item, decision)
            return decision

        # PLS-530 P1: reconcile a PREEXISTING (legacy) active operator blocker
        # that predates the dependency registry + metadata. A parent already
        # carrying an active linked operator ask to THIS plan's target team can
        # be claimed only when its legacy prose identifies the same typed ask.
        # Team equality alone is unsafe: it can bind the parent to an unrelated
        # dependency and hide the real holder. Once safely claimed, subsequent
        # same-ask parents dedupe via the registry pointer above.
        legacy_linked = next(
            (blocker for blocker in active_linked
             if _is_active_task(blocker) and _is_operator_blocker(blocker)
             and WorkflowService._canonical_team_name(blocker.assignment_group)
             == WorkflowService._canonical_team_name(plan.target_group)
             and not (blocker.metadata or {}).get("escalation_blocker_dependency_key")
             and _legacy_blocker_matches_dependency(blocker, prepared)),
            None,
        )
        if legacy_linked is not None:
            legacy_linked = _reconcile_reused_blocker_owner(
                legacy_linked,
                parent=item,
                plan=plan,
                transition=transition,
                user=user,
            )
            meta = dict(legacy_linked.metadata or {})
            meta["escalation_blocker_dependency_key"] = dependency_key
            legacy_linked.metadata = meta
            legacy_linked.save(update_fields=["metadata", "updated_at"])
            dependency.blocker = legacy_linked
            dependency.target_team = plan.target_group
            dependency.save(update_fields=["blocker", "target_team", "updated_at"])
            _link_reused_blocker(legacy_linked, item, user=user)  # idempotent if already linked
            decision = _BlockerDecision("reuse", dependency_key, legacy_linked)
            _record_blocker_decision(item, decision)
            return decision

    configured = WorkflowService._assigned_blocker_post_function(transition)
    blocker = WorkflowService._prepare_assigned_blocker(
        transition,
        item,
        user=user,
        screen_data=screen_data,
        # A configured escalation post-function describes the ordinary target
        # lane, but a fail-loud HOLD preserves the exact-org owner resolved by
        # the typed plan.  Use that one canonical owner for both mutations so a
        # configured admin-ops child cannot split from an engineering/devops
        # parent restored by _apply_parent_plan.
        team_override=plan.target_group if configured else None,
        ask=ask,
    )
    if blocker is None:
        # Transition has no create_blocker_task post-function — mint via a
        # synthetic post-function so hold paths still satisfy PLAT-4215.
        team = plan.target_group or "admin-ops"
        synthetic = type(
            "SyntheticEscalationTransition",
            (),
            {
                "id": getattr(transition, "id", None),
                "name": getattr(transition, "name", "Escalation hold"),
                "post_functions": [
                    {
                        "type": "create_blocker_task",
                        "team": team,
                        "workflow": "simple",
                        "priority": "urgent",
                        "source": "PLS-525",
                        "title": (
                            f"[BLOCKER] {item.item_key or item.pk}: "
                            f"escalation hold ({plan.reason_code})"
                        ),
                        "description": (
                            f"Auto-minted on escalation hold for "
                            f"**{item.item_key or item.pk}** "
                            f"(classification=`{plan.classification}`, "
                            f"reason=`{plan.reason_code}`). "
                            "PLS-525 / PLAT-4215: parent cannot enter blocked "
                            "without an owned linked blocker."
                        ),
                    }
                ],
            },
        )()
        blocker = WorkflowService._prepare_assigned_blocker(
            synthetic, item, user=user, screen_data=screen_data,
            ask=ask,
        )
    if blocker is None:
        raise _PlanFailure("assigned_blocker_post_function_missing")
    # PLS-530 + PLAT-4215/0116: a hold blocker is self-repairing (main's
    # schema-repair annotation, no-op when repair_reason is empty) AND, for an
    # operator-team ask, registered in the dedupe registry so equivalent parents
    # reuse one operator child. Both mutate metadata; annotate first, then the
    # dependency-key registration reads the merged metadata.
    _annotate_schema_repair_blocker(
        blocker,
        item,
        reason=repair_reason,
        expected_generation=expected_generation,
    )
    if dependency_key:
        meta = dict(blocker.metadata or {})
        meta["escalation_blocker_dependency_key"] = dependency_key
        blocker.metadata = meta
        blocker.save(update_fields=["metadata", "updated_at"])
        dependency.blocker = blocker
        dependency.target_team = plan.target_group
        dependency.save(update_fields=["blocker", "target_team", "updated_at"])
        result = (
            "reuse"
            if any(linked.pk == blocker.pk for linked in active_linked)
            else "mint"
        )
        decision = _BlockerDecision(result, dependency_key, blocker)
        _record_blocker_decision(item, decision)
        return decision
    # Non-operator team: no dedupe registry, but still expose the minted blocker
    # so callers can name it as the child (main's child_item_key fallback).
    return _BlockerDecision("", "", blocker)


def render_escalation_blocker_metrics() -> str:
    """Render durable pre-mint dispositions as a fixed-label counter."""
    counts = {
        row["blocker_result"]: row["count"]
        for row in EscalationRoutingDisposition.objects.exclude(
            blocker_result="",
        ).values("blocker_result").annotate(count=Count("id"))
    }
    lines = [
        "# HELP pulse_escalation_mint_total Escalation pre-mint gate dispositions.",
        "# TYPE pulse_escalation_mint_total counter",
    ]
    lines.extend(
        f'pulse_escalation_mint_total{{result="{result}"}} {counts.get(result, 0)}'
        for result in BLOCKER_RESULTS
    )
    return "\n".join(lines) + "\n"


def _arm_external_wait_exit(item: Item) -> None:
    """Arm the exit authority so a waiting_external item may transition out.

    The Item model refuses any status change away from ``waiting_external``
    without a matching ``STATE_EXIT_ARMED`` authority for the current wait
    generation (PLS-994 re-raise path). Arm the active wait so the escalation
    HOLD can commit; the authority is consumed when the item's new state is
    persisted.
    """
    from .external_wait import ExternalWaitAuthority

    authority = ExternalWaitAuthority.objects.select_for_update().filter(
        item=item,
        wait_generation=item.external_wait_generation,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    if authority is not None:
        authority.state = ExternalWaitAuthority.STATE_EXIT_ARMED
        authority.save(update_fields=["state", "updated_at"])


def _apply_parent_plan(item: Item, plan: _Plan, *, pre: dict, actor) -> None:
    from .workflow_service import WorkflowService

    # PLS-994: re-escalating a waiting_external blocker moves it out of the
    # external-wait state. The Item model requires an armed exit authority for
    # the current wait generation before any status change away from
    # waiting_external; arm it so the re-raise HOLD can commit instead of
    # 503ing with waiting_external_requires_typed_release.
    if (
        (pre.get("status") or "").strip().lower() == "waiting_external"
        and plan.target_status != "waiting_external"
    ):
        _arm_external_wait_exit(item)

    if plan.classification == "operator_request_hygiene_reject":
        _restore_pre_state(item, pre)
    elif plan.classification == "mixed_or_unknown_escalation_hold":
        _restore_pre_state(item, pre)
        item.status = "blocked"
        item.workflow_status = WorkflowService._workflow_status_display_for_item(item, "blocked")
        if (
            item.assignment_group != plan.target_group
            or not item.assignee_id
            or not item.assignee_email
        ):
            # PLAT-7292 reopen (PLAT-9276 live signature, 4x 09-19): the HOLD
            # branch omitted allow_group_owned, so a hold targeting the item's
            # own group raised target_assignee_unresolved when that group had
            # no resolvable seat (all members at WIP cap / excluded creator) —
            # the fail-loud HOLD itself 503'd with
            # escalation_hold_commit_failed. A HOLD parks the parent
            # group-owned BY DEFINITION: it must never require a live seat.
            # The 43449cc fix covered the typed-plan call site; this is the
            # second call site it missed.
            _assign_group(
                item,
                plan.target_group,
                actor=actor,
                operator_authorized=plan.target_group == "human-ops",
                allow_group_owned=True,
            )
    else:
        item.status = plan.target_status
        item.workflow_status = WorkflowService._workflow_status_display_for_item(item, plan.target_status)
        _assign_group(
            item, plan.target_group, actor=actor,
            operator_authorized=plan.target_group == "human-ops",
            allow_group_owned=plan.allow_group_owned_destination,
        )

    item.resolution = None
    item.resolution_date = None
    if item.status == "blocked" and pre["status"] != "blocked":
        WorkflowService._capture_pre_block_state(
            item=item,
            status=pre["status"],
            workflow_status=pre["workflow_status"],
            assignee_id=pre["assignee_id"],
            assignee_email=pre["assignee_email"],
            assignment_group=pre["assignment_group"],
            percent_complete=item.percent_complete,
            user=None,
        )
    item._activity_actor = actor
    item._suppress_standard_notifications = True
    item.save(update_fields=[
        "status", "workflow_status", "assignment_group", "assignee_id",
        "assignee_email", "metadata", "resolution", "resolution_date", "updated_at",
    ])


def _create_technical_child(parent: Item, plan: _Plan, *, actor) -> tuple[Item, list[str]]:
    from .workflow_service import WorkflowService

    tech = plan.technical_action or {}
    item_key = parent.project.get_next_item_key()
    child = Item.objects.create(
        mode="task",
        item_type="task.bug",
        title=f"[Escalation technical action] {str(tech.get('action') or '')[:320]}",
        description=(
            f"Generated atomically from {parent.item_key or parent.pk}.\n\n"
            f"Action: {tech.get('action')}\n\n"
            f"Independence evidence: {tech.get('independence_evidence')}\n\n"
            f"Acceptance check: {tech.get('acceptance_check')}"
        ),
        project=parent.project,
        item_key=item_key,
        parent_item=parent,
        workflow_name="autonomous-bug",
        status="open",
        workflow_status="Open",
        priority=parent.priority,
        assignment_group=plan.catalog_service_ref,
        assignee_id=None,
        assignee_email=None,
        created_by_id=parent.created_by_id,
        created_by_email=parent.created_by_email,
        source="escalation-routing",
        source_id=f"{parent.pk}:{plan.catalog_service_ref}",
        tags="PLAT-4609,escalation-technical-child",
        metadata={
            "escalation_parent": parent.item_key or str(parent.pk),
            "catalog_service_ref": plan.catalog_service_ref,
            "catalog_generation": plan.catalog_generation,
        },
    )
    child._activity_actor = actor
    child._suppress_standard_notifications = True
    uid, email = WorkflowService._resolve_team_assignee(
        plan.catalog_service_ref,
        exclude_email=parent.created_by_email,
        project_key=WorkflowService._item_project_key(parent),
        item=child,
    )
    if not uid or not email:
        raise _PlanFailure("technical_child_assignee_unresolved")
    fields: list[str] = []
    WorkflowService._apply_resolved_assignee(child, uid, email, fields)
    child.save(update_fields=[*fields, "updated_at"])

    links = [ItemLink.objects.create(
        source_item=parent,
        target_item=child,
        link_type="parent_of",
        created_by_id=actor[0] or 0,
        created_by_email=actor[1] or "system@pulse",
    )]
    if tech.get("relationship") == "blocks":
        links.append(ItemLink.objects.create(
            source_item=child,
            target_item=parent,
            link_type="blocks",
            created_by_id=actor[0] or 0,
            created_by_email=actor[1] or "system@pulse",
        ))
    else:
        links.append(ItemLink.objects.create(
            source_item=child,
            target_item=parent,
            link_type="relates_to",
            created_by_id=actor[0] or 0,
            created_by_email=actor[1] or "system@pulse",
        ))
    return child, [str(link.id) for link in links]


def _postcondition(
    item: Item,
    plan: _Plan,
    blocker_child: Item | None,
    technical_child: Item | None,
    blocker_decision: _BlockerDecision,
) -> None:
    from .workflow_service import WorkflowService

    item.refresh_from_db()
    if item.status != plan.target_status:
        raise _PlanFailure("postcondition_status_mismatch")
    if plan.target_group and (item.assignment_group or "") != plan.target_group:
        raise _PlanFailure("postcondition_team_mismatch")
    parent_group_owned = (
        plan.allow_group_owned_destination
        and not item.assignee_id
        and not item.assignee_email
        and WorkflowService._team_registered_for_item(plan.target_group, item)
    )
    if (not item.assignee_id or not item.assignee_email) and not parent_group_owned:
        raise _PlanFailure("postcondition_assignee_missing")
    if bool(plan.technical_action) != bool(technical_child):
        raise _PlanFailure("postcondition_child_mismatch")
    if plan.target_status != "blocked":
        return
    if blocker_child is None:
        raise _PlanFailure("postcondition_blocker_missing")
    blocker_child.refresh_from_db()
    active_blockers = WorkflowService._active_blockers_for(item)
    expected_group = WorkflowService._canonical_team_name(plan.target_group)
    dependency_key = (blocker_child.metadata or {}).get(
        "escalation_blocker_dependency_key",
    )
    if (
        blocker_decision.dependency_key
        and blocker_decision.result in {"mint", "reuse"}
    ):
        dependency = EscalationBlockerDependency.objects.select_for_update().filter(
            organization_id=item.project.org_id,
            dependency_key=blocker_decision.dependency_key,
        ).first()
        if dependency is None:
            raise _PlanFailure("operator_dependency_registry_missing")
        if WorkflowService._canonical_team_name(
            dependency.target_team,
        ) != expected_group:
            raise _PlanFailure("operator_dependency_registry_team_mismatch")
        if dependency.blocker_id != blocker_child.pk:
            raise _PlanFailure("operator_dependency_registry_child_mismatch")
        if dependency_key != blocker_decision.dependency_key:
            raise _PlanFailure("operator_dependency_child_key_mismatch")
        if WorkflowService._canonical_team_name(
            blocker_child.assignment_group,
        ) != expected_group:
            raise _PlanFailure("operator_dependency_child_team_mismatch")
    matching = [
        blocker
        for blocker in active_blockers
        if (blocker.mode or "").strip().lower() == "task"
        and WorkflowService._canonical_team_name(blocker.assignment_group)
        == expected_group
        and (
            (
                dependency_key
                and (blocker.metadata or {}).get(
                    "escalation_blocker_dependency_key",
                ) == dependency_key
            )
            or (not dependency_key and blocker.pk == blocker_child.pk)
        )
    ]
    if len(matching) != 1 or matching[0].pk != blocker_child.pk:
        raise _PlanFailure("postcondition_blocker_cardinality_mismatch")
    blocker_meta = blocker_child.metadata or {}
    blocker_group_owned = (
        bool(blocker_meta.get("escalation_group_owned_wait"))
        and not blocker_child.assignee_id
        and not (blocker_child.assignee_email or "").strip()
        and int(blocker_meta.get("escalation_group_owned_org_id") or 0)
        == int(getattr(getattr(blocker_child, "project", None), "org_id", 0) or 0)
        and WorkflowService._team_registered_for_item(
            blocker_child.assignment_group,
            blocker_child,
        )
    )
    if (
        not blocker_child.assignee_id
        or not (blocker_child.assignee_email or "").strip()
    ) and not blocker_group_owned:
        raise _PlanFailure("postcondition_blocker_assignee_missing")
    if blocker_group_owned:
        return
    mismatch, _roster_org, _task_org = WorkflowService.assignment_org_mismatch(
        blocker_child,
        blocker_child.assignee_email,
        expected_group,
        user_id=blocker_child.assignee_id,
    )
    if mismatch:
        raise _PlanFailure("postcondition_blocker_assignee_mismatch")


def _notice_recipients(item: Item, plan: _Plan) -> list[dict]:
    from .workflow_service import WorkflowService

    recipients: list[dict] = []
    if item.assignee_id:
        recipients.append({"user_id": item.assignee_id, "email": item.assignee_email or ""})
    coordinator_group = (
        "engineering"
        if plan.reason_code.startswith("postcondition_") or plan.reason_code == "unexpected_error"
        else "admin-ops"
    )
    uid, email = WorkflowService._resolve_team_assignee(
        coordinator_group,
        exclude_email=item.assignee_email,
        project_key=WorkflowService._item_project_key(item),
        item=item,
        # PLS-724: this is a read-only notification-recipient lookup — nothing
        # is being assigned. enforce_cap=True (the default) would reserve a WIP
        # slot against the escalated parent and drop the recipient whenever the
        # coordinator team is at its WIP cap (a full team silently stops
        # receiving escalation notices). A recipient lookup must not touch
        # capacity.
        enforce_cap=False,
    )
    if uid and all(row["user_id"] != uid for row in recipients):
        recipients.append({"user_id": uid, "email": email or ""})
    return recipients


def _comment_text(plan: _Plan, generation: int, child: Item | None) -> str:
    child_text = f" Child: **{child.item_key}**." if child else ""
    prefix = "🔴 **ANDON — typed escalation held.**" if plan.outcome == "held" else "🧭 **Typed escalation routed.**"
    return (
        f"{prefix} Classification: `{plan.classification}`; outcome: `{plan.outcome}`; "
        f"reason: `{plan.reason_code}`; generation: `{generation}`.{child_text} "
        "Routing used validated `shizuha.pulse.escalation-evidence.v1`; editable prose "
        "and legacy keyword classifiers were not authoritative."
    )


def _receipt_payload(receipt: EscalationRoutingReceipt, *, replayed: bool) -> dict:
    disposition = receipt.disposition
    final = dict(receipt.final_state or {})
    return {
        "receipt_id": str(receipt.id),
        "actor_subject_id": receipt.actor_subject_id,
        "disposition_id": str(disposition.id),
        "generation": disposition.generation,
        "classification": receipt.classification,
        "outcome": receipt.outcome,
        "reason_code": receipt.reason_code,
        "final_status": final.get("status"),
        "final_team": final.get("assignment_group"),
        "final_assignee_id": final.get("assignee_id"),
        "final_assignee_email": final.get("assignee_email"),
        "child_item_key": final.get("child_item_key"),
        "actor_assertion_digest": receipt.actor_assertion_digest,
        "replayed": bool(replayed),
        "replayed_disposition": bool(receipt.replayed_disposition),
    }


class EscalationRoutingService:
    """Exclusive execution service for operator-bound transition routing."""

    @classmethod
    def execute(cls, item_id: int, transition, *, user, screen_data: dict | None = None) -> Item:
        screen_data = screen_data or {}
        try:
            return cls._execute(item_id, transition, user=user, screen_data=screen_data)
        except Exception as exc:
            # Contract errors are authoritative client outcomes (403/409/503),
            # not classifier failures to be converted into a routing mutation.
            from .workflow_service import WorkflowValidationError
            if isinstance(exc, WorkflowValidationError):
                raise
            if isinstance(exc, _PlanFailure):
                logger.exception("Typed escalation plan failed before commit: %s", exc.reason_code)
                return cls._commit_unexpected_hold(
                    item_id, transition, user=user, screen_data=screen_data,
                    reason_code=exc.reason_code,
                )
            logger.exception("Typed escalation failed unexpectedly; committing owned hold")
            return cls._commit_unexpected_hold(
                item_id, transition, user=user, screen_data=screen_data,
                reason_code="unexpected_error",
            )

    @classmethod
    def _authorize(cls, item: Item, transition, user) -> None:
        from .workflow_service import WorkflowService, WorkflowValidationError

        if not PermissionService.get_user_item_queryset(
            user, organization_id=item.project.org_id
        ).filter(pk=item.pk).exists():
            raise WorkflowValidationError("Task is not visible to this actor", code="escalation_forbidden")
        if not WorkflowService._check_transition_conditions(transition, item, user):
            raise WorkflowValidationError(
                f"You do not have permission to execute transition '{transition.name}'",
                transition_id=str(transition.id), code="escalation_forbidden",
            )

    @classmethod
    def _execute(cls, item_id: int, transition, *, user, screen_data: dict) -> Item:
        from .workflow_service import WorkflowService, WorkflowValidationError

        with transaction.atomic():
            item = Item.objects.select_for_update().get(pk=item_id)
            cls._authorize(item, transition, user)
            from .package_guard import package_of_tasks_reason
            evidence = screen_data.get("escalation_evidence")
            subject = ""
            if isinstance(evidence, dict):
                dependency = evidence.get("dependency")
                if isinstance(dependency, dict):
                    subject = str(dependency.get("requested_subject") or "")
            package_reason = package_of_tasks_reason(
                "\n".join(part for part in (
                    item.title or "",
                    subject,
                    str(screen_data.get("comment") or ""),
                ) if part),
                item.description or "",
            )
            if package_reason:
                raise WorkflowValidationError(
                    f"Cannot escalate {item.item_key or item.pk}: {package_reason}",
                    transition_id=str(getattr(transition, "id", "") or ""),
                    code="PACKAGE_FORBIDDEN",
                )
            prepared = _prepare_evidence(item, user, screen_data.get("escalation_evidence"))
            actor_subject, actor_user_id, actor_email = _actor_identity(item, user)
            actor = (actor_user_id, actor_email)
            assertion_digest, disposition_digest = _digests(
                item, transition, prepared, actor_subject,
            )

            # PLS-1195: idempotency is (item, request_id) only. A fresh UUID
            # must never be compared to a prior row on the same item.
            request = EscalationRoutingRequest.objects.select_related(
                "receipt__disposition"
            ).filter(item=item, request_id=prepared.request_id).first()
            if request:
                if request.actor_assertion_digest != assertion_digest:
                    raise WorkflowValidationError(
                        "Escalation request_id was already used with different evidence",
                        code="idempotency_key_reused",
                    )
                item._escalation_receipt_payload = _receipt_payload(
                    request.receipt, replayed=True,
                )
                return item

            same_actor = EscalationRoutingReceipt.objects.select_related(
                "disposition"
            ).filter(
                item=item,
                actor_subject_id=actor_subject,
                source_generation=prepared.source_generation,
                actor_assertion_digest=assertion_digest,
            ).first()
            if same_actor:
                EscalationRoutingRequest.objects.create(
                    item=item,
                    request_id=prepared.request_id,
                    actor_assertion_digest=assertion_digest,
                    receipt=same_actor,
                )
                item._escalation_receipt_payload = _receipt_payload(same_actor, replayed=True)
                return item

            disposition = EscalationRoutingDisposition.objects.filter(
                item=item,
                source_generation=prepared.source_generation,
                disposition_digest=disposition_digest,
            ).first()
            if disposition:
                receipt = EscalationRoutingReceipt.objects.create(
                    organization_id=item.project.org_id,
                    project=item.project,
                    item=item,
                    transition_id=transition.id,
                    actor_subject_id=actor_subject,
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    actor_assertion_digest=assertion_digest,
                    primary_request_id=prepared.request_id,
                    source_generation=prepared.source_generation,
                    disposition=disposition,
                    classification=disposition.classification,
                    outcome=disposition.outcome,
                    reason_code=disposition.reason_code,
                    final_state=disposition.final_state,
                    replayed_disposition=True,
                )
                EscalationRoutingRequest.objects.create(
                    item=item,
                    request_id=prepared.request_id,
                    actor_assertion_digest=assertion_digest,
                    receipt=receipt,
                )
                item._escalation_receipt_payload = _receipt_payload(receipt, replayed=False)
                return item

            latest_generation = EscalationRoutingDisposition.objects.filter(item=item).aggregate(
                value=Max("generation")
            )["value"] or 0
            if prepared.source_generation != latest_generation:
                raise WorkflowValidationError(
                    f"Stale escalation generation: expected {latest_generation}",
                    code="stale_escalation_generation",
                )

            # Validate the live state only for a new disposition. Replays above
            # intentionally work after the first transition changed status.
            WorkflowService.validate_transition(
                item, transition.to_status, user, screen_data,
                transition_id=transition.id,
                _typed_escalation_plan=True,
            )

            pre = _state(item)
            plan = _plan_for(item, transition, prepared, screen_data)
            plan = _canonicalize_hold_plan(item, plan)
            blocker_child = None
            technical_child = None
            link_ids: list[str] = []
            try:
                with transaction.atomic():
                    blocker_decision = _prepare_required_blocker(
                        item, plan, transition, pre=pre, user=user,
                        screen_data=screen_data, prepared=prepared,
                        repair_reason=prepared.validation_reason,
                        expected_generation=latest_generation + 1,
                    )
                    blocker_child = blocker_decision.blocker
                    if blocker_decision.parent_group:
                        plan = replace(plan, target_group=blocker_decision.parent_group)
                    _apply_parent_plan(item, plan, pre=pre, actor=actor)
                    if plan.technical_action:
                        technical_child, link_ids = _create_technical_child(
                            item, plan, actor=actor,
                        )
                    _postcondition(
                        item, plan, blocker_child, technical_child, blocker_decision,
                    )
            except _PlanFailure as exc:
                # Roll back the failed plan savepoint and commit one owned HOLD
                # in the still-locked outer transaction.
                item = Item.objects.select_for_update().get(pk=item_id)
                dependency_mismatch = exc.reason_code.startswith(
                    "operator_dependency_",
                )
                plan = _Plan(
                    "mixed_or_unknown_escalation_hold", "held", exc.reason_code,
                    "blocked", (
                        pre.get("assignment_group")
                        if pre.get("assignment_group") and pre.get("assignee_id") and pre.get("assignee_email")
                        else "admin-ops"
                    ),
                )
                plan = _canonicalize_hold_plan(item, plan)
                blocker_decision = _prepare_required_blocker(
                    item, plan, transition, pre=pre, user=user,
                    screen_data=screen_data,
                    # Never re-enter the corrupt registry identity that caused
                    # this fail-closed path. The HOLD gets a per-item recovery
                    # identity while the attempted routed mutations remain
                    # rolled back in the inner savepoint.
                    prepared=None if dependency_mismatch else prepared,
                    repair_reason=prepared.validation_reason,
                    expected_generation=latest_generation + 1,
                )
                blocker_child = blocker_decision.blocker
                if blocker_decision.parent_group:
                    plan = replace(plan, target_group=blocker_decision.parent_group)
                _apply_parent_plan(item, plan, pre=pre, actor=actor)
                technical_child = None
                link_ids = []
                _postcondition(item, plan, blocker_child, None, blocker_decision)

            primary_child = technical_child or blocker_child
            generation = latest_generation + 1
            final = _state(item)
            final["child_item_key"] = primary_child.item_key if primary_child else None
            recipients = _notice_recipients(item, plan)
            outbox = EscalationRoutingNotificationOutbox.objects.create(
                item=item,
                disposition_digest=disposition_digest,
                recipients=recipients,
                payload={
                    "item_key": item.item_key,
                    "classification": plan.classification,
                    "outcome": plan.outcome,
                    "reason_code": plan.reason_code,
                    "blocker_result": blocker_decision.result,
                    "generation": generation,
                    "final_state": final,
                },
            )
            disposition_id = uuid.uuid4()
            receipt_id = uuid.uuid4()

            Comment.objects.create(
                item=item,
                author_id=actor_user_id or 0,
                author_email=actor_email or "system@pulse",
                content=_comment_text(plan, generation, primary_child),
            )
            meta = dict(item.metadata or {})
            meta["escalation_routing_gate"] = {
                "mechanism": "PLAT-4609 typed escalation routing",
                "disposition_id": str(disposition_id),
                "receipt_id": str(receipt_id),
                "generation": generation,
                "classification": plan.classification,
                "outcome": plan.outcome,
                "reason_code": plan.reason_code,
            }
            item.metadata = meta
            item._suppress_standard_notifications = True
            item.save(update_fields=["metadata", "updated_at"])

            disposition = EscalationRoutingDisposition.objects.create(
                id=disposition_id,
                organization_id=item.project.org_id,
                project=item.project,
                item=item,
                transition_id=transition.id,
                source_generation=prepared.source_generation,
                generation=generation,
                disposition_digest=disposition_digest,
                schema_version=SCHEMA,
                classification=plan.classification,
                outcome=plan.outcome,
                reason_code=plan.reason_code,
                blocker_dependency_key=blocker_decision.dependency_key,
                blocker_result=blocker_decision.result,
                normalized_evidence=prepared.normalized,
                pre_state=pre,
                final_state=final,
                child_item=primary_child,
                link_ids=link_ids,
                catalog_service_ref=plan.catalog_service_ref,
                catalog_generation=plan.catalog_generation,
                notification_outbox_id=outbox.id,
            )
            receipt = EscalationRoutingReceipt.objects.create(
                id=receipt_id,
                organization_id=item.project.org_id,
                project=item.project,
                item=item,
                transition_id=transition.id,
                actor_subject_id=actor_subject,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                actor_assertion_digest=assertion_digest,
                primary_request_id=prepared.request_id,
                source_generation=prepared.source_generation,
                disposition=disposition,
                classification=plan.classification,
                outcome=plan.outcome,
                reason_code=plan.reason_code,
                final_state=final,
            )
            EscalationRoutingRequest.objects.create(
                item=item,
                request_id=prepared.request_id,
                actor_assertion_digest=assertion_digest,
                receipt=receipt,
            )
            item._escalation_receipt_payload = _receipt_payload(receipt, replayed=False)
            if (
                blocker_child is not None
                and not plan.reason_code.startswith("operator_dependency_")
            ):
                from .external_wait import admit_human_escalation
                admit_human_escalation(blocker_child, disposition, user=user)
            transaction.on_commit(lambda: cls._enqueue_notice(outbox.id))
            return item

    @classmethod
    def _commit_unexpected_hold(
        cls, item_id: int, transition, *, user, screen_data: dict, reason_code: str,
    ) -> Item:
        """Fresh narrow transaction after an unexpected attempted mutation rollback."""
        from .workflow_service import WorkflowValidationError

        # Reuse the normal engine with a validation-error envelope.  Keeping the
        # original request/generation makes the resulting receipt payload-bound.
        raw = copy.deepcopy(screen_data.get("escalation_evidence") or {})
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("schema", SCHEMA)
        raw.setdefault("request_id", str(uuid.uuid4()))
        raw.setdefault("expected_generation", 0)
        raw["_forced_hold_reason"] = reason_code
        fallback_data = dict(screen_data)
        fallback_data["escalation_evidence"] = raw
        try:
            return cls._execute_forced_hold(
                item_id, transition, user=user, screen_data=fallback_data,
                reason_code=reason_code,
            )
        except Exception as exc:
            logger.exception("Typed escalation fail-loud HOLD could not commit")
            raise WorkflowValidationError(
                "Typed escalation failed and its fail-loud HOLD could not commit",
                code="escalation_hold_commit_failed",
            ) from exc

    @classmethod
    def _execute_forced_hold(
        cls, item_id: int, transition, *, user, screen_data: dict, reason_code: str,
    ) -> Item:
        # The forced path is intentionally small: generate an otherwise valid
        # envelope identity, then commit the same append-only records as a hold.
        with transaction.atomic():
            item = Item.objects.select_for_update().get(pk=item_id)
            cls._authorize(item, transition, user)
            raw = copy.deepcopy(screen_data.get("escalation_evidence") or {})
            dependency_evidence = copy.deepcopy(raw) if isinstance(raw, dict) else {}
            # _commit_unexpected_hold adds one private recovery marker that is
            # intentionally outside the public evidence schema.  Strip only
            # that marker before validation so a valid typed dependency keeps
            # its cross-parent content identity on the forced path.  Missing or
            # otherwise invalid envelopes still use _dependency_key's scoped
            # per-item fallback.
            dependency_evidence.pop("_forced_hold_reason", None)
            try:
                prepared = _prepare_evidence(item, user, dependency_evidence)
            except Exception:
                # This path may have been entered because evidence preparation
                # itself failed. Recovery must not call the same failing parser
                # into a second exception; the per-item forced_hold key is the
                # fail-loud identity when typed evidence cannot be recovered.
                logger.exception(
                    "Typed escalation evidence could not be recovered for forced HOLD",
                )
                prepared = None
            request_id, _ = _parse_request_id(raw.get("request_id"), fallback_material=str(uuid.uuid4()))
            latest = EscalationRoutingDisposition.objects.filter(item=item).aggregate(
                value=Max("generation")
            )["value"] or 0
            actor_subject, actor_user_id, actor_email = _actor_identity(item, user)
            actor = (actor_user_id, actor_email)
            canonical = _canonical({"reason": reason_code, "source_generation": latest})
            disposition_digest = hashlib.sha256(
                f"{item.pk}|{transition.id}|{latest}|{canonical}".encode()
            ).hexdigest()
            assertion_digest = hashlib.sha256(
                f"{item.pk}|{transition.id}|{latest}|{actor_subject}|{canonical}".encode()
            ).hexdigest()
            existing = EscalationRoutingReceipt.objects.select_related("disposition").filter(
                item=item, actor_subject_id=actor_subject,
                source_generation=latest, actor_assertion_digest=assertion_digest,
            ).first()
            if existing:
                item._escalation_receipt_payload = _receipt_payload(existing, replayed=True)
                return item

            pre = _state(item)
            # A valid human ask with no exact-org seat used to park the
            # carrier as waiting_external. That status is retired: mint an
            # owned blocker on the holding team instead, so the chain root
            # stays workable. An envelope that is not a valid human ask
            # stays on the carrier so the agent can repair it.
            dependency = None
            if prepared is not None and isinstance(getattr(prepared, 'normalized', None), dict):
                dependency = prepared.normalized.get('dependency')
            from .external_wait import HUMAN_CLASSES
            from .escalation_canary_probe import CANARY_WORKFLOW_SLUG
            valid_human = (
                isinstance(dependency, dict)
                and dependency.get('evidence_class') in HUMAN_CLASSES
                and str(dependency.get('holder_ref') or '').strip()
                and str(dependency.get('requested_subject') or '').strip()
                and str(dependency.get('acceptance_check') or '').strip()
            )
            # The channel canary uses this reason code to exercise the commit
            # path without minting an admin-ops child or paging a human.
            # A real unresolved ask mints an owned workable blocker instead.
            is_canary = (item.workflow_name or '') == CANARY_WORKFLOW_SLUG
            preserve_without_routing = (
                reason_code == "org_local_operator_destination_unresolved"
                and (is_canary or not valid_human)
            )
            if preserve_without_routing:
                parked = None
                if prepared is not None:
                    from .external_wait import admit_unresolved_operator_destination
                    parked = admit_unresolved_operator_destination(
                        item,
                        evidence=prepared.normalized,
                        user=user,
                    )
                    item.refresh_from_db()
                plan = _Plan(
                    "mixed_or_unknown_escalation_hold", "held", reason_code,
                    (
                        item.status
                        if parked is not None
                        else pre["status"]
                    ),
                    pre.get("assignment_group") or "",
                )
                blocker_decision = _BlockerDecision()
                blocker_child = None
            else:
                plan = _Plan(
                    "mixed_or_unknown_escalation_hold", "held", reason_code,
                    "blocked", (
                        pre.get("assignment_group")
                        if pre.get("assignment_group") and pre.get("assignee_id") and pre.get("assignee_email")
                        else "admin-ops"
                    ),
                )
                plan = _canonicalize_hold_plan(item, plan)
                blocker_decision = _prepare_required_blocker(
                    item, plan, transition, pre=pre, user=user,
                    screen_data=screen_data,
                    # A registry-agreement failure must not recursively consult
                    # the same corrupt identity while committing its owned HOLD.
                    prepared=(
                        None
                        if reason_code.startswith("operator_dependency_")
                        else prepared
                    ),
                )
                blocker_child = blocker_decision.blocker
                if blocker_decision.parent_group:
                    plan = replace(plan, target_group=blocker_decision.parent_group)
                _apply_parent_plan(item, plan, pre=pre, actor=actor)
                _postcondition(item, plan, blocker_child, None, blocker_decision)
            generation = latest + 1
            final = _state(item)
            final["child_item_key"] = (
                blocker_child.item_key if blocker_child else None
            )
            outbox = EscalationRoutingNotificationOutbox.objects.create(
                item=item,
                disposition_digest=disposition_digest,
                recipients=_notice_recipients(item, plan),
                payload={
                    "item_key": item.item_key,
                    "classification": plan.classification,
                    "outcome": plan.outcome,
                    "reason_code": reason_code,
                    "blocker_result": blocker_decision.result,
                    "generation": generation,
                    "final_state": final,
                },
            )
            disposition = EscalationRoutingDisposition.objects.create(
                organization_id=item.project.org_id,
                project=item.project,
                item=item,
                transition_id=transition.id,
                source_generation=latest,
                generation=generation,
                disposition_digest=disposition_digest,
                schema_version=SCHEMA,
                classification=plan.classification,
                outcome=plan.outcome,
                reason_code=reason_code,
                blocker_dependency_key=blocker_decision.dependency_key,
                blocker_result=blocker_decision.result,
                # Preserve valid typed dependency evidence so a genuine human
                # hold can be admitted.  If evidence parsing itself caused the
                # recovery path, retain the pre-existing bounded error proof;
                # such an ambiguous child must remain runnable.
                normalized_evidence=(
                    prepared.normalized if prepared is not None
                    else _bounded_error_evidence(SCHEMA, reason_code)
                ),
                pre_state=pre,
                final_state=final,
                child_item=blocker_child,
                notification_outbox_id=outbox.id,
            )
            receipt = EscalationRoutingReceipt.objects.create(
                organization_id=item.project.org_id,
                project=item.project,
                item=item,
                transition_id=transition.id,
                actor_subject_id=actor_subject,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                actor_assertion_digest=assertion_digest,
                primary_request_id=request_id,
                source_generation=latest,
                disposition=disposition,
                classification=plan.classification,
                outcome=plan.outcome,
                reason_code=reason_code,
                final_state=final,
            )
            EscalationRoutingRequest.objects.create(
                item=item, request_id=request_id,
                actor_assertion_digest=assertion_digest, receipt=receipt,
            )
            Comment.objects.create(
                item=item, author_id=actor_user_id or 0,
                author_email=actor_email or "system@pulse",
                content=_comment_text(plan, generation, blocker_child),
            )
            item._escalation_receipt_payload = _receipt_payload(receipt, replayed=False)
            if (
                blocker_child is not None
                and not reason_code.startswith("operator_dependency_")
            ):
                from .external_wait import admit_human_escalation
                admit_human_escalation(blocker_child, disposition, user=user)
            transaction.on_commit(lambda: cls._enqueue_notice(outbox.id))
            return item

    @staticmethod
    def _enqueue_notice(outbox_id: uuid.UUID) -> None:
        try:
            from ..tasks import deliver_escalation_routing_outbox
            deliver_escalation_routing_outbox.delay(str(outbox_id))
        except Exception:
            # The committed outbox is the recovery authority; a periodic/manual
            # sweep can claim it even when broker enqueue fails here.
            logger.exception("Could not enqueue escalation outbox %s", outbox_id)
