"""Closed, typed provenance for server-generated Pulse comments (PLS-478).

The public comment serializers never call this module.  Each admitted event has
one code-owned payload schema and renderer; callers cannot supply body text or
provenance fields.  Unknown/malformed rows remain direction-eligible.
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import Comment


class MechanicalCommentError(ValidationError):
    """A typed audit could not be validated or committed atomically."""


@dataclass(frozen=True)
class MechanicalTemplate:
    kind: str
    event: str
    version: int
    required: dict
    optional: dict
    render: Callable[[dict], str]


def _render_evidence_summary(evidence):
    """PLAT-8177: one-line human projection of a receipt's evidence package."""
    if not isinstance(evidence, dict):
        return 'malformed'
    if evidence.get('kind') == 'trail_pointer':
        return (
            f"pointer record={evidence.get('record_kind')}:{evidence.get('record_id')} "
            f"content_address={evidence.get('content_address')}"
        )
    if evidence.get('kind') == 'inline_package':
        return (
            f"package actor={evidence.get('actor')} origin={evidence.get('originating_key')} "
            f"head={evidence.get('target_head')} verdicts={len(evidence.get('verdict_refs') or [])}"
        )
    return 'malformed'


def _auto_unblock(p):
    body = (
        "Auto-unblocked: all linked blockers are resolved/releasing after "
        f"{p['blocker_key']} resolved. Restored `{p['old_status']}` -> "
        f"`{p['restored_status']}` and assigned to "
        f"`{p['assignee_email'] or 'unassigned'}`."
    )
    if p.get('merge_sod_reason'):
        body += (
            " Merge SoD hold is active: no safe concrete merger was assigned "
            f"(`{p['merge_sod_reason']}`); the task remains on the `merge` "
            "group queue (PLS-411)."
        )
    return body


_RELEASE_REASON_TEXT = {
    'sweep': 'no longer held by any active blocker (sweep reconciliation)',
}


def _release_reason_phrase(release_reason):
    """PLAT-9444: name the actual trigger class, never blanket-"resolved".

    ``release_reason`` is the blocker's triggering status slug, or ``sweep``
    when the restore came from the reconciliation sweep (no concrete blocker).
    Terminal slugs read as resolution; non-terminal releasing slugs (the
    PLS-436 RFC family) read as a status change — the distinction v1 blurred.
    """
    reason = (release_reason or '').strip()
    if not reason:
        return 'all linked blockers released'
    if reason in _RELEASE_REASON_TEXT:
        return _RELEASE_REASON_TEXT[reason]
    from .workflow_service import WorkflowService

    if WorkflowService._status_is_terminal(reason):
        return f"{reason} reached a terminal state"
    return f"{reason} entered a block-releasing status (not yet terminal)"


def _auto_unblock_v2(p):
    body = (
        "Auto-unblocked: all linked blockers released — "
        f"{_release_reason_phrase(p.get('release_reason'))} "
        f"({p['blocker_key']}). Restored `{p['old_status']}` -> "
        f"`{p['restored_status']}` and assigned to "
        f"`{p['assignee_email'] or 'unassigned'}`."
    )
    if p.get('merge_sod_reason'):
        body += (
            " Merge SoD hold is active: no safe concrete merger was assigned "
            f"(`{p['merge_sod_reason']}`); the task remains on the `merge` "
            "group queue (PLS-411)."
        )
    return body


def _review_starvation(p):
    caps = ', '.join(f'`{cap}`' for cap in p['missing_capabilities']) or '(none declared)'
    reasons = ', '.join(f'`{reason}`' for reason in p['reason_classes'])
    return (
        f"🔴 {p['marker']}: **{p['item_key']}** entered `in_review` at "
        f"priority `{p['priority']}` but no compatible reviewer resolved.\n"
        f"Missing capabilities: {caps}. Reason classes: {reasons}.\n"
        f"Held group-queued on `{p['team']}`; no incompatible substitute was assigned. "
        "Review lead notified immediately; the 48h stale-stage escalation remains fallback-only."
    )


def _routability_hold(p):
    attempted = p.get('attempted') or ''
    detail = p.get('detail') or ''
    labels = ', '.join(f'`{label}`' for label in p['reason_classes'])
    return (
        f"{p['dedupe']}: `{p['source']}` found no routable candidate for team `{p['team']}`; "
        f"reason classes: {labels}.{attempted}{detail}\n"
        "Holding this item group-queued/unchanged instead of assigning it to an unroutable "
        "seat. Routing owners: team lead; Admin Ops for phantom/empty teams; DevOps/Platform "
        "for runtime liveness/capability-source gaps."
    )


def _focus_refused(p):
    """PLS-722: preserve a comment body the queue focus guard refused.

    The guard still refuses — the caller receives its 400 and no work starts —
    but the author's content is kept instead of discarded. A refusal used to
    take the body with it, so an investigation finished against a gated task
    had nowhere to land and the stall went unrecorded.

    Authored as a mechanical row (``workflow@pulse``), which is also what keeps
    it out of the activity counters: it is deliberately NOT attributed to the
    refused agent, because a refusal is evidence that agent was stopped, not
    that it produced work.
    """
    return (
        f"Focus-guard refused a comment on `{p['refused_item_key']}` "
        f"(status `{p['refused_item_status']}`) from `{p['refused_author']}`: "
        f"owned blocker root `{p['required_task_key']}` must be worked first.\n"
        "The body is preserved verbatim below so the finding is not lost. This "
        "record is NOT a progress report and is excluded from activity counters.\n\n"
        "---\n"
        f"{p['refused_body']}"
    )


def _capability_hold(p):
    attempted = f"\nAttempted assignee: `{p['attempted_email']}`." if p.get('attempted_email') else ''
    required = ', '.join(f'`{cap}`' for cap in p['required_capabilities'])
    return (
        f"{p['marker']}: `{p['source']}` found no capability-compatible seat for "
        f"team `{p['team']}`; required capabilities: {required}.{attempted}\n"
        "Holding this item group-queued instead of assigning it to an incapable runtime. "
        "Routing owners: team lead plus DevOps/Platform for runtime-substrate gaps."
    )


def _recurring_route_discrepancy(p):
    return (
        f"PLS-528 recurrence-routing alert [{p['item_id']}:{p['invalid_group']}:"
        f"{p['fingerprint']}]: recurring template route `{p['invalid_group']}` is "
        f"ineligible (`{p['reason']}`). Outcome: `{p['outcome']}`; configured "
        f"fallback: `{p['fallback']}`. This alert is deduplicated by template + "
        "invalid group + roster/template condition; moving only the current "
        "instance does not clear the template discrepancy."
    )


def _recurring_route_cleared(p):
    return (
        "PLS-528 recurrence-routing condition cleared: "
        f"`{p['invalid_group']}` — {p['reason']}."
    )


def _recurring_assignment_andon(p):
    return (
        f"[recurring-assignment ANDON] No concrete seat resolved for "
        f"team={p['team'] or 'unknown'} after {p['attempts']} bounded attempts. "
        "The cadence remains scheduled with a live timer; restore a compatible "
        "team seat before the next boundary."
    )


def _recurring_missed_fire_p1(p):
    return (
        "[recurring-cycle] missed fire: period 1 missed at boundary "
        f"{p['boundary']} — assignee notified. Advance the "
        "cycle or use Deferred to intentionally stop it. [PLS-1286]"
    )


def _qa_demand_created(p):
    return (
        f"Auto-created from settled QA demand `{p['demand_id']}` generation "
        f"{p['generation']}, DeploymentEvent `{p['event_id']}` / commit "
        f"`{p['commit_sha']}`. Newer deploys retarget this same task with an "
        "auditable supersession."
    )


def _qa_demand_superseded(p):
    return (
        f"QA demand advanced generation {p['from_generation']} → "
        f"{p['to_generation']}. Superseded DeploymentEvent "
        f"`{p['superseded_event_id']}` / commit `{p['superseded_commit_sha']}`; "
        f"this task now covers newest event `{p['current_event_id']}` / commit "
        f"`{p['current_commit_sha']}`. Prior-generation evidence cannot verify "
        "the new generation."
    )


def _qa_demand_failed(p):
    return (
        f"Cancelled: current QA demand generation {p['generation']} "
        f"(`{p['commit_sha']}`) progressed to `{p['state']}` / "
        f"`{p['smoke_result']}`. Superseded generation failures never retire "
        "a newer live verification unit."
    )


def _focused_qa_fail_committed(p):
    return (
        f"Focused-QA failure committed (receipt `{p['receipt_id']}`) from "
        f"`{p['task_key'] or p['task_id']}`. "
        f"Fingerprint: `{p['fingerprint'] or '(empty)'}`."
    )


_SPECS = (
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'assignment.identity_bound_wake', 1,
        {'priority': str, 'prior_email': str}, {},
        lambda p: (
            f"[identity-bound wake requested] {p['priority']} card stays on "
            f"{p['prior_email'].split('@')[0]}: this identity-bound task must not "
            "auto-failover to a non-co-signer (gate cannot green from a "
            "different identity) and no other named co-signer is active. "
            "Wake/lease signal raised — co-signer needs a slot to post "
            "APPROVED/REQUEST_CHANGES."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'link.auto_block', 1,
        {'blocker_key': str}, {},
        lambda p: (
            f"Auto-blocked because linked blocker **{p['blocker_key']}** is active. "
            "When all linked blockers reach a terminal state, Pulse will "
            "auto-resume this task to its pre-block status and owner."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'link.auto_unblock', 1,
        {'blocker_key': str, 'old_status': str, 'restored_status': str,
         'assignee_email': str},
        {'merge_sod_reason': str}, _auto_unblock,
    ),
    MechanicalTemplate(
        # PLAT-9444 v2: v1 said "after {blocker} resolved" even when the
        # trigger was a NON-TERMINAL block-releasing status (rfc_approved per
        # PLS-436) — wording that misattributed the mechanism and produced a
        # wrong engine-defect filing. v2 names the actual trigger class.
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'link.auto_unblock', 2,
        {'blocker_key': str, 'old_status': str, 'restored_status': str,
         'assignee_email': str, 'release_reason': str},
        {'merge_sod_reason': str}, _auto_unblock_v2,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'assignment.review_changes_requested', 1,
        {'assignee_email': str, 'return_generation': int}, {},
        lambda p: (
            f"Changes requested in review — routed back to {p['assignee_email']} to address "
            "the feedback and re-submit. Read the reviewer's comments (and any linked "
            "Origin PR review), make the fixes, then fire **Submit for Review** again. "
            f"[return generation {p['return_generation']}]"
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'assignment.repo_write_blocked', 1,
        {'login': str, 'repo': str, 'status_code': str, 'permission': str,
         'grant_group': str}, {},
        lambda p: (
            f"Seat `{p['login']}` has no write scope on `{p['repo']}` "
            f"(HTTP {p['status_code']}, permission `{p['permission']}`). "
            f"Routed to the `{p['grant_group']}` grant queue. "
            "The in_progress status is unchanged. [PLAT-10558]"
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'assignment.cleared', 1,
        {'prior_email': str, 'reason': str, 'action': str, 'result_group': str}, {},
        lambda p: (
            f"Assignment cleared: `{p['prior_email']}` removed "
            f"({p['action']}, reason `{p['reason']}`). "
            f"Live owner is now the `{p['result_group'] or '(no group)'}` queue, "
            "not the earlier force-assign receipt. [PLAT-10496]"
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'assignment.unpark_rerouted', 1,
        {'parked_email': str, 'team': str, 'assignee_email': str}, {},
        lambda p: (
            f"Auto-rerouted after un-defer: parked assignee `{p['parked_email']}` "
            f"was cleared and `{p['team']}` resolved to `{p['assignee_email']}`. [PLS-206]"
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'status_lane.normalized', 1,
        {'status': str, 'old_group': str, 'old_email': str,
         'new_group': str, 'new_email': str}, {},
        lambda p: (
            f"Normalized status-lane ownership for `{p['status']}`: "
            f"`{p['old_group'] or '(none)'}` / `{p['old_email'] or '(none)'}` "
            f"→ `{p['new_group']}` / `{p['new_email'] or '(group queue)'}` (PLS-188)."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'status_lane.merge_sod_hold', 1,
        {'status': str, 'reason': str, 'current_head_reviewers': list,
         'excluded_assignees': list}, {},
        lambda p: (
            f"Merge SoD hold for `{p['status']}`: no safe concrete merger was assigned; "
            f"reason=`{p['reason']}`; current-head reviewers="
            f"`{','.join(p['current_head_reviewers']) or 'unknown'}`; "
            f"excluded=`{','.join(p['excluded_assignees']) or 'none'}`. "
            "Task remains on the `merge` group queue instead of assigning the reviewer "
            "(PLS-411)."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'external_wait.human_release', 1,
        {'receipt': str}, {},
        lambda p: (
            'External holder dependency released; restored the pre-wait workflow state. '
            f"Receipt: `{p['receipt']}`."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'external_wait.human_settlement', 1,
        {'receipt': str, 'wait_generation': int, 'evidence_sha256': str,
         'evidence': dict}, {},
        lambda p: (
            'External holder ask fulfilled; resolved elsewhere without resuming new work. '
            f"{p['evidence']['summary']} Receipt: `{p['receipt']}`, generation {p['wait_generation']}; "
            f"evidence SHA-256: `{p['evidence_sha256']}`."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'external_wait.cross_org_revoked', 1,
        {'receipt': str, 'reason': str}, {},
        lambda p: (
            f"External dependency binding revoked locally: {p['reason']} "
            f"Receipt: `{p['receipt']}`."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'external_wait.human_revalidation', 1,
        {'receipt': str, 'holder': str, 'subject': str}, {},
        lambda p: (
            '⏳ **External dependency revalidation due.** Confirm the ask '
            f"is still wanted and `{p['holder']}` is still its holder. "
            f"Subject: {p['subject']}"
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'operator_wait.evidence_reactivated', 1,
        {'fingerprint': str, 'source': str, 'generation': int}, {},
        lambda p: (
            '## Operator-evidence reactivation (PLS-527)\n\n'
            f"Evidence fingerprint changed (`{p['fingerprint']}` via "
            f"`{p['source'] or 'event'}`). Operator-wait marker cleared for this "
            f"digest revision (generation {p['generation']}) — queue exactly one "
            'verification pass. Do not re-probe unchanged external evidence.'
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_VALIDATOR_AUDIT, 'validator.deploy_verify_blocked', 1,
        {'marker': str, 'item_key': str, 'target': str, 'invariant': str}, {},
        lambda p: (
            f"{p['marker']}: blocked `{p['item_key']}` from `{p['target']}` because no "
            "successful production DeploymentEvent with `smoke_result=passed`, no "
            "`deploy-verified:` live probe comment, and no audited "
            f"`deploy-verify: waived - <reason>` comment were present. Invariant: {p['invariant']}. "
            "(Kill-switch: PULSE_DEPLOY_VERIFY_GATE_ENABLED.)"
        ),
    ),
    # PLS-675 / PLAT-1351 v4: named missing-evidence classes + acceptance_mode.
    MechanicalTemplate(
        Comment.ORIGIN_VALIDATOR_AUDIT, 'validator.deploy_verify_blocked', 2,
        {
            'marker': str, 'item_key': str, 'target': str, 'invariant': str,
            'acceptance_mode': str, 'missing_classes': list,
        },
        {},
        lambda p: (
            f"{p['marker']}: blocked `{p['item_key']}` from `{p['target']}` "
            f"(acceptance_mode={p['acceptance_mode']}; "
            f"missing={','.join(p['missing_classes'] or [])}). "
            f"Invariant: {p['invariant']}. "
            "Terminal close for deployed/live requires a correlated production "
            "DeploymentEvent (task+repo+merged-head-SHA+env+image/digest+replicas "
            "Ready); live also needs a typed QA/live-probe receipt. Free-form "
            "comments are never authority. Role-gated durable waiver receipts only. "
            "(Kill-switch: PULSE_DEPLOY_VERIFY_GATE_ENABLED.)"
        ),
    ),
    # PLS-675 / Rei P1: immutable typed live-probe receipt (append-only).
    # issuer_email + issuer_team are snapshotted at mint time — gate does NOT
    # re-check live roster membership (roster drift must not rewrite history).
    # rui P1: also snapshot the exact correlated production deployment so a
    # pre-deploy / old-head receipt cannot authorize a later roll.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.live_probe_passed', 1,
        {
            'item_key': str,
            'result': str,
            'issuer_email': str,
            'issuer_team': str,
            'issued_at': str,
            'evidence': str,
            'deployment_event_id': str,
            'deployment_repo': str,
            'deployment_commit_sha': str,
            'deployment_image': str,
            'deployment_image_digest': str,
            'deployment_image_tag': str,
            # rui P1: mint only after serving-replica convergence; snapshot so a
            # pre-convergence receipt cannot be legitimized by later same-row upsert.
            'deployment_replicas_ready': bool,
            'deployment_converged_at': str,
        },
        # PLAT-6165: the runtime promotion lane is a second, tightly-scoped
        # producer.  These fields are absent on historical human-QA receipts;
        # when present they are fixed by Pulse after Kubernetes TokenReview.
        {
            'workload_identity': str,
            'probe_contract': str,
        },
        lambda p: (
            f"Typed live-probe receipt: result={p['result']} "
            f"issuer={p['issuer_email']} team={p['issuer_team']} "
            f"at {p['issued_at']} deploy_event={p['deployment_event_id']} "
            f"sha={(p.get('deployment_commit_sha') or '')[:12]} "
            f"image={p.get('deployment_image') or p.get('deployment_image_digest') or '-'} "
            f"replicas_ready={p.get('deployment_replicas_ready')} "
            f"converged_at={p.get('deployment_converged_at')}. "
            f"Evidence: {p['evidence']}"
        ),
    ),
    # PLS-675 / Rei P1: immutable typed deploy-verify waiver receipt.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.deploy_verify_waiver', 1,
        {
            'item_key': str,
            'result': str,
            'issuer_email': str,
            'issuer_team': str,
            'issued_at': str,
            'reason': str,
        },
        {},
        lambda p: (
            f"Typed deploy-verify waiver: result={p['result']} "
            f"issuer={p['issuer_email']} team={p['issuer_team']} "
            f"at {p['issued_at']}. Reason: {p['reason']}"
        ),
    ),
    # PLAT-8177: self-auditing waiver receipts. A receipt that cannot be
    # interpreted without off-object context is a provenance gap, so v2 mints
    # carry EITHER the full evidence package inline OR an immutable,
    # content-addressed trail-pointer to the canonical trail record.
    # GRANDFATHERING: v1 receipts (template_version=1) remain valid; their
    # composite-record location is the item trail — structured_payload.item_key
    # resolves the item whose trail (receipt + correlated authorization
    # comments) forms the composite record. Never DM history.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.deploy_verify_waiver', 2,
        {
            'item_key': str,
            'result': str,
            'issuer_email': str,
            'issuer_team': str,
            'issued_at': str,
            'reason': str,
            'evidence_kind': str,  # 'inline_package' | 'trail_pointer'
            'evidence': dict,
        },
        {},
        lambda p: (
            f"Typed deploy-verify waiver: result={p['result']} "
            f"issuer={p['issuer_email']} team={p['issuer_team']} "
            f"at {p['issued_at']}. Reason: {p['reason']} "
            f"Evidence ({p['evidence_kind']}): {_render_evidence_summary(p['evidence'])}"
        ),
    ),
    # PLS-675 / Rei P1: stranded merge-event retry (fail-loud, not a success heartbeat).
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.merge_event_retry_pending', 1,
        {
            'item_key': str,
            'result': str,
            'pr_url': str,
            'error': str,
            'observed_at': str,
        },
        {},
        lambda p: (
            f"PLS-675 merge-event retry PENDING: item={p['item_key']} "
            f"result={p['result']} pr={p['pr_url']} at {p['observed_at']}. "
            f"Error: {p['error']}. Item remains at awaiting_merge; the "
            f"Origin reconciler will retry with bounded backoff. "
            f"(statusz: deploy_verify_merge_event_last_failure)"
        ),
    ),
    # PLAT-7066 (Retro 27Aug): a merged security-finding cannot advance
    # awaiting_merge → verification until Security freezes a
    # VerificationRequirementManifest (PLS-521 gate). When the manifest is
    # missing the transition returns 400 requirement_manifest_invalid — a
    # Security-side requirement-manifest issue, NOT a merge action. The merge
    # lane must not retry it; this typed receipt records the Security routing.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.merge_event_requirement_manifest_pending', 1,
        {
            'item_key': str,
            'result': str,
            'pr_url': str,
            'observed_at': str,
        },
        {},
        lambda p: (
            f"PLS-521 merge-event ROUTED TO SECURITY: item={p['item_key']} "
            f"result={p['result']} pr={p['pr_url']} at {p['observed_at']}. "
            f"Merge→Verification is blocked because no "
            f"VerificationRequirementManifest is frozen (requirement_manifest_"
            f"invalid). A Security-team reviewer must freeze the requirement "
            f"manifest before this finding can advance. The merge lane will "
            f"NOT retry this (it cannot succeed until the manifest exists)."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.review_starvation', 1,
        {'marker': str, 'item_key': str, 'priority': str, 'missing_capabilities': list,
         'reason_classes': list, 'team': str}, {}, _review_starvation,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.routability_hold', 1,
        {'dedupe': str, 'source': str, 'team': str, 'reason_classes': list},
        {'attempted': str, 'detail': str}, _routability_hold,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_FOCUS_REFUSED, 'focus_guard.comment_refused', 1,
        {'refused_item_key': str, 'refused_item_status': str,
         'refused_author': str, 'required_task_key': str, 'refused_body': str},
        {}, _focus_refused,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.capability_hold', 1,
        {'marker': str, 'source': str, 'team': str, 'required_capabilities': list},
        {'attempted_email': str}, _capability_hold,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.recurring_assignment_andon', 1,
        {'team': str, 'attempts': int}, {}, _recurring_assignment_andon,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.recurring_route_discrepancy', 1,
        {
            'item_id': str, 'invalid_group': str, 'reason': str,
            'outcome': str, 'fallback': str, 'fingerprint': str,
        }, {}, _recurring_route_discrepancy,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'resolver.recurring_route_cleared', 1,
        {'invalid_group': str, 'reason': str}, {}, _recurring_route_cleared,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_RESOLVER_AUDIT, 'recurring.missed_fire_period1', 1,
        {'boundary': str}, {}, _recurring_missed_fire_p1,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'qa_demand.created', 1,
        {'demand_id': str, 'generation': int, 'event_id': str, 'commit_sha': str},
        {}, _qa_demand_created,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'qa_demand.superseded', 1,
        {
            'from_generation': int, 'to_generation': int,
            'superseded_event_id': str, 'superseded_commit_sha': str,
            'current_event_id': str, 'current_commit_sha': str,
        },
        {}, _qa_demand_superseded,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'qa_demand.failed', 1,
        {
            'generation': int, 'commit_sha': str, 'state': str,
            'smoke_result': str,
        },
        {}, _qa_demand_failed,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'focused_qa.fail_committed', 1,
        {
            'receipt_id': str, 'task_key': str, 'task_id': str,
            'fingerprint': str,
        },
        {}, _focused_qa_fail_committed,
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'merge_event.skip_live_state', 1,
        {'verdict': str, 'item_key': str, 'pr_url': str, 'detail': str},
        {},
        lambda p: (
            f"PLAT-7047 merge-event SKIPPED ({p['verdict']}) for "
            f"{p['item_key']}: queued merge event for {p['pr_url']} was not "
            "confirmed against the live PR state "
            f"({p['detail']}). The item remains at awaiting_merge; the "
            "Origin reconciler re-fires the post-merge transition when the "
            "live state supports it."
        ),
    ),
    MechanicalTemplate(
        # PLAT-8967: the deliberate-exception path of the merge-time review
        # gate — an immutable typed record naming the authority and the reason
        # a merge landed without a qualifying review entry.
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.emergency_merge', 1,
        {
            'item_key': str, 'result': str,
            'issuer_email': str, 'issuer_team': str, 'reason': str,
        },
        {'schema': str, 'issued_at': str},
        lambda p: (
            f"PLAT-8967 EMERGENCY MERGE MARKER for {p['item_key']}: merge "
            f"without a qualifying review entry APPROVED by {p['issuer_email']} "
            f"({p['issuer_team']}). Reason: {p['reason']}. This is the "
            "deliberate-exception path of the merge review gate — the "
            "authority and reason are recorded immutably."
        ),
    ),
    MechanicalTemplate(
        # PLAT-8967: durable mechanical breach record for a merge event
        # refused by the review gate. Deduped per PR by the gate; the breach
        # is recorded, never silently terminalized.
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'merge_gate.empty_review_refused', 1,
        {
            'item_key': str, 'pr_url': str, 'merged_by': str,
            'detail': str, 'remediation': str,
        },
        {},
        lambda p: (
            f"PLAT-8967 merge-event REFUSED (empty review board) for "
            f"{p['item_key']}: the merge of {p['pr_url']} (by "
            f"{p['merged_by']}) carried no qualifying review entry "
            f"({p['detail']}). The item remains at awaiting_merge — the "
            f"breach is recorded, never silently terminalized. Remediation: "
            f"{p['remediation']}."
        ),
    ),
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'evidence_blocker.escalation_sent', 1,
        {'generation': str, 'delivered_recipients': list, 'required_evidence': str},
        {},
        lambda p: (
            "⏰ Evidence deadline escalation sent once for generation "
            f"`{p['generation'][:12]}` to {', '.join(p['delivered_recipients'])}. "
            f"Required evidence: {p['required_evidence']}"
        ),
    ),
    # PLAT-9427: seat-accessible acceptance-mode reclassification receipt —
    # immutable audit for the narrow live → code record-class correction.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'receipt.acceptance_mode_reclassification', 2,
        {'item_key': str, 'result': str, 'actor_email': str, 'from_mode': str,
         'to_mode': str, 'reason': str, 'issued_at': str, 'contract': str},
        {},
        lambda p: (
            "🔀 Acceptance mode reclassified for "
            f"{p['item_key']}: `{p['from_mode']}` → `{p['to_mode']}` by "
            f"{p['actor_email']} ({p['contract']}). Reason: {p['reason']}"
        ),
    ),
    # PLAT-9427: create-time filing-mode warning — a retro-finding row filed
    # acceptance_mode=live is likely a record deliverable; loud but
    # non-blocking signal at create.
    MechanicalTemplate(
        Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'warning.retro_acceptance_mode_live', 2,
        {'item_key': str, 'result': str, 'filed_acceptance_mode': str,
         'record_class_tag': str, 'detail': str},
        {},
        lambda p: (
            "⚠️ Filing-mode warning on "
            f"{p['item_key']}: acceptance_mode=`{p['filed_acceptance_mode']}` "
            f"on a `{p['record_class_tag']}` row. {p['detail']}"
        ),
    ),
)

MECHANICAL_COMMENT_SPECS = {
    (spec.kind, spec.event, spec.version): spec for spec in _SPECS
}


def _validate_payload(spec, payload):
    if not isinstance(payload, dict):
        raise ValidationError('Mechanical comment payload must be an object.')
    allowed = set(spec.required) | set(spec.optional)
    missing = set(spec.required) - set(payload)
    extra = set(payload) - allowed
    if missing or extra:
        raise ValidationError(
            f'Invalid payload for {spec.event}: missing={sorted(missing)}, extra={sorted(extra)}'
        )
    for field, expected in {**spec.required, **spec.optional}.items():
        if field in payload and not isinstance(payload[field], expected):
            raise ValidationError(f'{spec.event}.{field} must be {expected.__name__}.')
    for value in payload.values():
        if isinstance(value, list) and not all(isinstance(v, str) for v in value):
            raise ValidationError('Mechanical comment lists may contain strings only.')
    return dict(payload)


def _causal_actor(actor):
    if actor is None:
        return None, ''
    if isinstance(actor, dict):
        return actor.get('id'), str(actor.get('email') or '')
    if isinstance(actor, (tuple, list)):
        return (actor[0] if actor else None), (str(actor[1] or '') if len(actor) > 1 else '')
    return getattr(actor, 'id', None), str(getattr(actor, 'email', '') or '')


class MechanicalCommentFactory:
    """The only writer of comments that may be excluded from direction."""

    @staticmethod
    def create(*, item, origin_kind, origin_event, template_version,
               structured_payload, origin_event_id, causal_actor=None):
        key = (origin_kind, origin_event, template_version)
        spec = MECHANICAL_COMMENT_SPECS.get(key)
        if not spec:
            raise MechanicalCommentError(f'Unknown mechanical comment template: {key!r}')
        event_id = str(origin_event_id or '').strip()
        if not event_id or len(event_id) > 255:
            raise MechanicalCommentError('A stable origin_event_id is required.')
        try:
            payload = _validate_payload(spec, structured_payload)
        except ValidationError as exc:
            raise MechanicalCommentError(exc.messages) from exc
        content = spec.render(payload)
        caused_by_user_id, caused_by_email = _causal_actor(causal_actor)

        # PLS-675 / Rei P1: role-gated receipts bind issuer identity AND team to
        # the immutable causal-actor snapshot at mint. Direct factory callers
        # cannot mint a qualifying receipt with a forged issuer email OR a
        # forged issuer_team (same-email + claimed qa/devops without roster
        # membership). Gate consumers trust the snapshotted team string and do
        # not re-check live roster.
        if origin_event in (
            'receipt.live_probe_passed',
            'receipt.deploy_verify_waiver',
        ):
            # Lazy import avoids circular import at module load
            # (workflow_service → mechanical_comments → workflow_service).
            from .workflow_service import WorkflowService

            issuer = str(payload.get('issuer_email') or '').strip()
            claimed_team = str(payload.get('issuer_team') or '').strip().lower()
            if not issuer:
                raise MechanicalCommentError(
                    f'{origin_event} requires issuer_email in the payload.'
                )
            runtime_automation = (
                origin_event == 'receipt.live_probe_passed'
                and claimed_team == 'qa-automation'
                and issuer == 'runtime-evidence@shizuha.internal'
                and payload.get('workload_identity')
                == 'system:serviceaccount:rt-fleet:shizuha-runtime-fleet'
                and payload.get('probe_contract')
                == 'runtime-promotion-live-probe-v1'
                and causal_actor is None
            )
            if not caused_by_email and not runtime_automation:
                raise MechanicalCommentError(
                    f'{origin_event} requires a causal_actor with email; '
                    f'direct minting without an authenticated actor is refused.'
                )
            email = caused_by_email.strip()
            if not runtime_automation and issuer.lower() != email.lower():
                raise MechanicalCommentError(
                    f'{origin_event} issuer_email={issuer!r} must equal '
                    f'causal_actor email={email!r}.'
                )
            if not runtime_automation and WorkflowService._is_deploy_verify_automation_email(email):
                raise MechanicalCommentError(
                    f'{origin_event} refuses automation causal_actor {email!r}.'
                )

            if origin_event == 'receipt.live_probe_passed':
                allowed_teams = ('qa', 'qa-automation')
            else:
                # PLS-675 operator waiver authority: the human operator (CEO)
                # is an additional authorized issuer for deploy-verify waivers.
                allowed_teams = tuple(WorkflowService._DEPLOY_WAIVER_TEAMS) + ('operator',)

            if not claimed_team:
                raise MechanicalCommentError(
                    f'{origin_event} requires issuer_team in the payload.'
                )
            if claimed_team not in allowed_teams:
                raise MechanicalCommentError(
                    f'{origin_event} issuer_team={claimed_team!r} is not in '
                    f'allowed teams {allowed_teams}.'
                )
            # Authoritative mint-time membership check — caller-supplied team
            # string alone is never trusted. The operator issuer_team is
            # verified against the operator email, not a team roster.
            if claimed_team == 'operator':
                if not WorkflowService._is_operator_email(email):
                    raise MechanicalCommentError(
                        f'{origin_event} causal_actor {email!r} is not the '
                        f'operator; forged operator team claims are refused.'
                    )
            elif not runtime_automation and not WorkflowService._is_team_member(
                email, claimed_team, item=item,
            ):
                raise MechanicalCommentError(
                    f'{origin_event} causal_actor {email!r} is not a member of '
                    f'issuer_team={claimed_team!r} at mint time; forged team '
                    f'claims are refused.'
                )

            # Canonicalize so gate equality is exact after mint.
            payload['issuer_email'] = issuer if runtime_automation else email
            payload['issuer_team'] = claimed_team
            content = spec.render(payload)

        try:
            with transaction.atomic():
                comment, created = Comment.objects.get_or_create(
                    origin_event_id=event_id,
                    defaults={
                        'item': item,
                        'author_id': 0,
                        'author_email': 'workflow@pulse',
                        'author_name': 'Pulse Workflow',
                        'content': content,
                        'origin_kind': origin_kind,
                        'origin_event': origin_event,
                        'template_version': template_version,
                        'origin_payload': payload,
                        'caused_by_user_id': caused_by_user_id,
                        'caused_by_email_snapshot': caused_by_email,
                    },
                )
                if not created:
                    expected = (
                        comment.item_id == item.pk
                        and comment.author_id == 0
                        and comment.author_email == 'workflow@pulse'
                        and comment.origin_kind == origin_kind
                        and comment.origin_event == origin_event
                        and comment.template_version == template_version
                        and comment.origin_payload == payload
                        and comment.content == content
                        and comment.caused_by_user_id == caused_by_user_id
                        and comment.caused_by_email_snapshot == caused_by_email
                    )
                    if not expected:
                        raise MechanicalCommentError(
                            'origin_event_id replay does not match the existing mechanical comment.'
                        )
                return comment
        except MechanicalCommentError:
            raise
        except Exception as exc:
            raise MechanicalCommentError(
                f'Could not commit mechanical comment {origin_event!r} atomically.'
            ) from exc

    @staticmethod
    def is_allowlisted(comment):
        key = (
            getattr(comment, 'origin_kind', None),
            getattr(comment, 'origin_event', None),
            getattr(comment, 'template_version', None),
        )
        spec = MECHANICAL_COMMENT_SPECS.get(key)
        if not spec:
            return False
        try:
            payload = _validate_payload(spec, getattr(comment, 'origin_payload', None))
            return (
                comment.author_id == 0
                and comment.author_email == 'workflow@pulse'
                and bool(comment.origin_event_id)
                and comment.content == spec.render(payload)
            )
        except (ValidationError, TypeError, ValueError):
            return False


def activity_event_id(item, origin_event, *, action=None, new_value=None):
    """Bind a typed comment to an already-durable ItemActivity.

    Callers whose mutation can roll back must select the activity for the
    *preceding* state boundary (for example the activity entering ``blocked``
    before an auto-unblock). Binding to an activity created by the mutation
    itself is not retry-stable: a rollback removes that row and a retry creates
    a new primary key.
    """
    qs = item.activities.all()
    if action:
        qs = qs.filter(action=action)
    if new_value is not None:
        qs = qs.filter(new_value=str(new_value))
    activity = qs.order_by('-created_at').first()
    if not activity:
        raise ValidationError(
            f'No durable ItemActivity exists for {origin_event} on item {item.pk}.'
        )
    return f'item-activity:{activity.pk}:{origin_event}'


def deterministic_event_id(namespace, item, *parts):
    """Stable identity for a deduped server event without its own event table."""
    material = json.dumps(
        [str(getattr(item, 'pk', item)), *[str(part) for part in parts]],
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode()
    return f'{namespace}:{hashlib.sha256(material).hexdigest()}'
