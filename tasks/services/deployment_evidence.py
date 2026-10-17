"""Typed deployed-evidence authority for the security-finding gate (PLS-521).

Evidence producers write immutable local snapshots out of band.  The workflow
transition performs no network I/O: it reads one current manifest/cursor/snapshot
tuple, records an idempotent verdict + outbox row, and either passes or returns a
typed deploy-lane HOLD.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from tasks.models import (
    DeploymentEvidence,
    DeploymentEvidenceCursor,
    DeploymentEvidenceGateOutbox,
    DeploymentEvidenceGateVerdict,
    Item,
    LinkedPullRequestMergeAttestation,
    VerificationRequirementManifest,
)


SHA_RE = re.compile(r'^[0-9a-f]{40,64}$', re.IGNORECASE)
DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$', re.IGNORECASE)
LIVE_COMPONENT_PREFIXES = (
    'deployment:', 'runtime:', 'service:', 'workload:', 'monitoring:', 'metric:',
    'prometheus:', 'alert-rule:', 'k8s:', 'kubernetes:',
)
SOURCE_COMPONENT_PREFIXES = ('source:', 'docs:', 'test:')
INVALIDATION_REASONS = frozenset({
    'rollback', 'redeploy', 'workload_replacement', 'rule_change',
    'target_disappearance', 'newer_merge', 'evidence_refresh',
})
HOLD_REASONS = frozenset({
    'requirement_manifest_invalid', 'deployment_identity_mismatch',
    'rollout_not_ready', 'target_unhealthy', 'metric_missing',
    'metric_stale_generation', 'rule_digest_mismatch', 'evidence_unreachable',
})


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    reason: str
    verdict: DeploymentEvidenceGateVerdict

    @property
    def passed(self) -> bool:
        return self.outcome == 'pass'


def _clean(value: Any) -> str:
    return str(value or '').strip()


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise ValidationError('Expected a list of strings.')
    return sorted({_clean(v) for v in value if _clean(v)})


def _clean_rule_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError('prometheus_rules must be an object of rule name -> sha256 digest.')
    cleaned = {_clean(k): _clean(v).lower() for k, v in value.items() if _clean(k)}
    invalid = sorted(k for k, digest in cleaned.items() if not DIGEST_RE.fullmatch(digest))
    if invalid:
        raise ValidationError(
            f"PrometheusRule semantic digests must be sha256:<64 hex>; invalid: {', '.join(invalid)}"
        )
    return dict(sorted(cleaned.items()))


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _require_sha(value: Any, field: str) -> str:
    cleaned = _clean(value).lower()
    if not SHA_RE.fullmatch(cleaned):
        raise ValidationError({field: 'Must be a full 40-64 character hexadecimal commit SHA.'})
    return cleaned


def _require_digest(value: Any, field: str) -> str:
    cleaned = _clean(value).lower()
    if not DIGEST_RE.fullmatch(cleaned):
        raise ValidationError({field: 'Must be an immutable sha256:<64 hex> digest.'})
    return cleaned


def _require_pr_repo(value: Any) -> str:
    repo = _clean(value).strip('/').lower()
    if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repo):
        raise ValidationError({'pull_request_repo': 'Must be an exact owner/repository coordinate.'})
    return repo


@transaction.atomic
def record_merge_attestation(
    *, item: Item, provider: str, repo: str, number: int, url: str,
    head_sha: str, merge_commit: str, merged_at: Any,
    attested_by: str = 'pr-sync',
) -> tuple[LinkedPullRequestMergeAttestation, bool]:
    """Persist one immutable merge identity read from the provider API."""
    locked = Item.objects.select_for_update().get(pk=item.pk)
    repo = _require_pr_repo(repo)
    try:
        number = int(number)
    except (TypeError, ValueError) as exc:
        raise ValidationError({'pull_request_number': 'Must be a positive integer.'}) from exc
    if number < 1:
        raise ValidationError({'pull_request_number': 'Must be a positive integer.'})
    head_sha = _require_sha(head_sha, 'head_sha')
    merge_commit = _require_sha(merge_commit, 'merge_commit')
    if isinstance(merged_at, str):
        merged_at = parse_datetime(merged_at)
    if not merged_at:
        raise ValidationError({'merged_at': 'Provider merge timestamp is required.'})
    if timezone.is_naive(merged_at):
        merged_at = timezone.make_aware(merged_at, timezone.get_current_timezone())
    provider = _clean(provider).lower()
    attested_by = _clean(attested_by).lower()
    if not provider or not attested_by:
        raise ValidationError('Provider and attesting producer identity are required.')

    # PLAT-10787 (aoi review 6470): the existence check must be scoped to the
    # provider. Under the provider-scoped constraint a github mirror-era row
    # and the forgejo attestation legitimately coexist; an unordered
    # cross-provider .first() would return the shadow row and reject the
    # canonical identity at creation, re-stranding the freeze gate this
    # re-key unfreezes. Per-provider immutability stays intact below.
    existing = LinkedPullRequestMergeAttestation.objects.filter(
        item=locked, repo=repo, number=number, provider=provider,
    ).first()
    if existing:
        same = (
            existing.provider == provider
            and existing.head_sha == head_sha
            and existing.merge_commit == merge_commit
        )
        if not same:
            raise ValidationError({
                'pull_request': (
                    'Provider merge identity conflicts with the immutable existing attestation.'
                )
            })
        return existing, False
    created = LinkedPullRequestMergeAttestation.objects.create(
        item=locked,
        provider=provider,
        repo=repo,
        number=number,
        url=_clean(url),
        head_sha=head_sha,
        merge_commit=merge_commit,
        merged_at=merged_at,
        attested_by=attested_by,
    )
    try:
        from tasks.services.lane_health import maybe_record_hold_override_from_merge
        maybe_record_hold_override_from_merge(
            locked,
            head_sha=head_sha,
            repo=repo,
            number=number,
            merger=attested_by,
            merged_at=merged_at,
        )
    except Exception:
        pass
    return created, True


def derive_classification(*, affected_components, scrape_targets, metric_series, prometheus_rules) -> str:
    """Derive source-only vs live-required without accepting a caller boolean."""
    components = _clean_list(affected_components)
    has_named_live_requirement = bool(scrape_targets or metric_series or prometheus_rules)
    has_live_component = any(
        component.lower().startswith(LIVE_COMPONENT_PREFIXES)
        for component in components
    )
    if has_named_live_requirement or has_live_component:
        return 'live_required'
    if components and all(
        component.lower().startswith(SOURCE_COMPONENT_PREFIXES)
        for component in components
    ):
        return 'source_only'
    raise ValidationError({
        'affected_components': (
            'Cannot derive a fail-closed verification class. Use typed source/docs/test '
            'or deployment/runtime/service/workload/monitoring/metric/prometheus prefixes.'
        ),
    })


@transaction.atomic
def create_requirement_manifest(
    *, item: Item, merge_commit: str, pull_request_repo: str,
    pull_request_number: int, affected_components=None,
    scrape_targets=None, metric_series=None, prometheus_rules=None,
    environment: str = '', service: str = '', workload: str = '',
    cluster: str = '', namespace: str = '',
    reviewer_id=None, reviewer_email: str,
) -> VerificationRequirementManifest:
    """Create and freeze the next Security-reviewed requirement generation."""
    locked = Item.objects.select_for_update().get(pk=item.pk)
    if (locked.workflow_name or '').strip().lower().replace('_', '-') != 'security-finding':
        raise ValidationError('Deployment verification manifests are only valid for security-finding items.')

    components = _clean_list(affected_components)
    targets = _clean_list(scrape_targets)
    metrics = _clean_list(metric_series)
    rules = _clean_rule_map(prometheus_rules)
    classification = derive_classification(
        affected_components=components,
        scrape_targets=targets,
        metric_series=metrics,
        prometheus_rules=rules,
    )
    merge_commit = _require_sha(merge_commit, 'merge_commit')
    pull_request_repo = _require_pr_repo(pull_request_repo)
    try:
        pull_request_number = int(pull_request_number)
    except (TypeError, ValueError) as exc:
        raise ValidationError({'pull_request_number': 'Must be a positive integer.'}) from exc
    if pull_request_number < 1:
        raise ValidationError({'pull_request_number': 'Must be a positive integer.'})
    merge_attestation = LinkedPullRequestMergeAttestation.objects.filter(
        item=locked,
        repo=pull_request_repo,
        number=pull_request_number,
        merge_commit=merge_commit,
    ).first()
    if not merge_attestation:
        raise ValidationError({
            'merge_commit': (
                'No provider-attested merged PR matches this item, repository, number, and commit.'
            )
        })
    environment = _clean(environment)
    service = _clean(service)
    workload = _clean(workload)
    cluster = _clean(cluster)
    namespace = _clean(namespace)
    if classification == 'live_required':
        missing = [name for name, value in (
            ('environment', environment), ('service', service), ('workload', workload),
            ('cluster', cluster), ('namespace', namespace),
        ) if not value]
        if missing:
            raise ValidationError({name: 'Required for live deployment verification.' for name in missing})

    previous = (
        VerificationRequirementManifest.objects.select_for_update()
        .filter(item=locked, is_current=True).first()
    )
    generation = (previous.requirement_generation + 1) if previous else 1
    payload = {
        'item_id': locked.pk,
        'requirement_generation': generation,
        'classification': classification,
        'merge_commit': merge_commit,
        'merge_attestation_id': str(merge_attestation.pk),
        'pull_request_repo': pull_request_repo,
        'pull_request_number': pull_request_number,
        'environment': environment,
        'service': service,
        'workload': workload,
        'cluster': cluster,
        'namespace': namespace,
        'affected_components': components,
        'scrape_targets': targets,
        'metric_series': metrics,
        'prometheus_rules': rules,
        'reviewed_by_email': _clean(reviewer_email).lower(),
    }
    if not payload['reviewed_by_email']:
        raise ValidationError({'reviewer_email': 'A Security reviewer identity is required.'})

    if previous:
        VerificationRequirementManifest.objects.filter(pk=previous.pk).update(
            is_current=False, superseded_at=timezone.now(),
        )
    manifest = VerificationRequirementManifest.objects.create(
        item=locked,
        requirement_generation=generation,
        classification=classification,
        merge_commit=merge_commit,
        merge_attestation=merge_attestation,
        pull_request_repo=pull_request_repo,
        pull_request_number=pull_request_number,
        environment=environment,
        service=service,
        workload=workload,
        cluster=cluster,
        namespace=namespace,
        affected_components=components,
        scrape_targets=targets,
        metric_series=metrics,
        prometheus_rules=rules,
        manifest_digest=_canonical_digest(payload),
        reviewed_by_id=reviewer_id,
        reviewed_by_email=payload['reviewed_by_email'],
    )
    DeploymentEvidenceCursor.objects.create(
        item=locked,
        requirement_generation=generation,
        current_evidence_generation=1,
        invalidation_reason='requirement_generation_created',
        invalidated_by_email=payload['reviewed_by_email'],
    )
    return manifest


@transaction.atomic
def invalidate_evidence(*, item: Item, reason: str, actor_email: str) -> DeploymentEvidenceCursor:
    """Advance the current evidence generation before a replacement snapshot exists."""
    reason = _clean(reason).lower()
    if reason not in INVALIDATION_REASONS:
        raise ValidationError({'reason': f"Must be one of: {', '.join(sorted(INVALIDATION_REASONS))}"})
    locked = Item.objects.select_for_update().get(pk=item.pk)
    manifest = (
        VerificationRequirementManifest.objects.select_for_update()
        .filter(item=locked, is_current=True).first()
    )
    if not manifest:
        raise ValidationError('No current verification requirement manifest exists.')
    cursor = DeploymentEvidenceCursor.objects.select_for_update().get(
        item=locked, requirement_generation=manifest.requirement_generation,
    )
    cursor.current_evidence_generation += 1
    cursor.invalidation_reason = reason
    cursor.invalidated_by_email = _clean(actor_email).lower()
    cursor.save(update_fields=[
        'current_evidence_generation', 'invalidation_reason',
        'invalidated_by_email', 'updated_at',
    ])
    return cursor


def _normalise_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError({field: 'Must be a JSON object keyed by the declared requirement name.'})
    return {str(k).strip(): v for k, v in value.items() if str(k).strip()}


@transaction.atomic
def create_evidence_snapshot(
    *, item: Item, merge_commit: str, artifact_digest: str, config_digest: str,
    build_provenance: dict, environment: str, service: str, cluster: str, namespace: str,
    workload: str, workload_uid: str = '', rollout_revision: str = '',
    workload_generation: int, observed_generation: int, ready: bool,
    ready_at, target_health=None, metric_series=None, prometheus_rules=None,
    producer_id=None, producer_identity: str,
) -> tuple[DeploymentEvidence, bool]:
    """Create one complete immutable snapshot; identical retries are idempotent."""
    locked = Item.objects.select_for_update().get(pk=item.pk)
    manifest = (
        VerificationRequirementManifest.objects.select_for_update()
        .filter(item=locked, is_current=True).first()
    )
    if not manifest:
        raise ValidationError('No current verification requirement manifest exists.')
    if manifest.classification != 'live_required':
        raise ValidationError('Source-only findings do not accept live deployment evidence.')
    cursor = DeploymentEvidenceCursor.objects.select_for_update().get(
        item=locked, requirement_generation=manifest.requirement_generation,
    )

    merge_commit = _require_sha(merge_commit, 'merge_commit')
    artifact_digest = _require_digest(artifact_digest, 'artifact_digest')
    config_digest = _require_digest(config_digest, 'config_digest')
    if not isinstance(build_provenance, dict):
        raise ValidationError({'build_provenance': 'Must be a JSON object.'})
    provenance_commit = _require_sha(build_provenance.get('merge_commit'), 'build_provenance.merge_commit')
    provenance_artifact = _require_digest(
        build_provenance.get('artifact_digest'), 'build_provenance.artifact_digest',
    )
    if provenance_commit != merge_commit or provenance_artifact != artifact_digest:
        raise ValidationError('Build provenance must bind the same merge commit to the same artifact digest.')
    producer_identity = _clean(producer_identity).lower()
    if not producer_identity:
        raise ValidationError({'producer_identity': 'An authenticated evidence-producer identity is required.'})
    workload_uid = _clean(workload_uid)
    rollout_revision = _clean(rollout_revision)
    if not workload_uid and not rollout_revision:
        raise ValidationError('At least one of workload_uid or rollout_revision is required.')
    if int(workload_generation) < 1 or int(observed_generation) < 0:
        raise ValidationError('Workload generations must be non-negative and metadata.generation must be >= 1.')

    targets = _normalise_mapping(target_health or {}, 'target_health')
    metrics = _normalise_mapping(metric_series or {}, 'metric_series')
    rules = _normalise_mapping(prometheus_rules or {}, 'prometheus_rules')
    payload = {
        'item_id': locked.pk,
        'requirement_generation': manifest.requirement_generation,
        'evidence_generation': cursor.current_evidence_generation,
        'merge_commit': merge_commit,
        'artifact_digest': artifact_digest,
        'config_digest': config_digest,
        'build_provenance': build_provenance,
        'environment': _clean(environment),
        'service': _clean(service),
        'cluster': _clean(cluster),
        'namespace': _clean(namespace),
        'workload': _clean(workload),
        'workload_uid': workload_uid,
        'rollout_revision': rollout_revision,
        'workload_generation': int(workload_generation),
        'observed_generation': int(observed_generation),
        'ready': bool(ready),
        'ready_at': ready_at.isoformat() if hasattr(ready_at, 'isoformat') else _clean(ready_at),
        'target_health': targets,
        'metric_series': metrics,
        'prometheus_rules': rules,
        'producer_identity': producer_identity,
    }
    existing = DeploymentEvidence.objects.filter(
        item=locked,
        requirement_generation=manifest.requirement_generation,
        evidence_generation=cursor.current_evidence_generation,
    ).first()
    digest = _canonical_digest(payload)
    if existing:
        if existing.evidence_digest == digest:
            return existing, False

    # A gate attempt may have durably HOLDed this generation before the
    # monitoring producer converged, or a prior immutable snapshot may now be
    # superseded by fresher facts.  Never mutate that tuple: atomically advance
    # the cursor and write a new generation.  This is the production-order
    # PLAT-4133 → PLAT-4138 recovery path.
    verdict_exists = DeploymentEvidenceGateVerdict.objects.filter(
        item=locked,
        requirement_generation=manifest.requirement_generation,
        evidence_generation=cursor.current_evidence_generation,
    ).exists()
    if existing is not None or verdict_exists:
        cursor.current_evidence_generation += 1
        cursor.invalidation_reason = 'evidence_refresh'
        cursor.invalidated_by_email = producer_identity
        cursor.save(update_fields=[
            'current_evidence_generation', 'invalidation_reason',
            'invalidated_by_email', 'updated_at',
        ])
        payload['evidence_generation'] = cursor.current_evidence_generation
        digest = _canonical_digest(payload)

    evidence = DeploymentEvidence.objects.create(
        item=locked,
        requirement_generation=manifest.requirement_generation,
        evidence_generation=cursor.current_evidence_generation,
        merge_commit=merge_commit,
        artifact_digest=artifact_digest,
        config_digest=config_digest,
        build_provenance=build_provenance,
        environment=payload['environment'],
        service=payload['service'],
        cluster=payload['cluster'],
        namespace=payload['namespace'],
        workload=payload['workload'],
        workload_uid=workload_uid,
        rollout_revision=rollout_revision,
        workload_generation=payload['workload_generation'],
        observed_generation=payload['observed_generation'],
        ready=payload['ready'],
        ready_at=ready_at,
        target_health=targets,
        metric_series=metrics,
        prometheus_rules=rules,
        producer_id=producer_id,
        producer_identity=producer_identity,
        evidence_digest=digest,
    )
    return evidence, True


def _identity_matches(entry: Any, evidence: DeploymentEvidence) -> bool:
    if not isinstance(entry, dict):
        return False
    uid = _clean(entry.get('workload_uid'))
    revision = _clean(entry.get('rollout_revision'))
    return bool(
        (uid and uid == evidence.workload_uid)
        or (revision and revision == evidence.rollout_revision)
    )


def _sample_is_fresh(entry: Any, evidence: DeploymentEvidence) -> bool:
    if not _identity_matches(entry, evidence):
        return False
    sample_at = parse_datetime(_clean(entry.get('sample_at'))) if isinstance(entry, dict) else None
    if not sample_at:
        return False
    if timezone.is_naive(sample_at):
        sample_at = timezone.make_aware(sample_at, timezone.get_current_timezone())
    return sample_at > evidence.ready_at


def _evaluate_current(item: Item) -> tuple[str, str, VerificationRequirementManifest | None, DeploymentEvidence | None, int]:
    manifest = (
        VerificationRequirementManifest.objects.filter(item=item, is_current=True)
        .order_by('-requirement_generation').first()
    )
    if not manifest:
        return 'hold', 'requirement_manifest_invalid', None, None, 0
    cursor = DeploymentEvidenceCursor.objects.filter(
        item=item, requirement_generation=manifest.requirement_generation,
    ).first()
    if not cursor:
        return 'hold', 'evidence_unreachable', manifest, None, 0
    generation = cursor.current_evidence_generation
    attestation = manifest.merge_attestation
    if (
        attestation.item_id != item.pk
        or attestation.repo != manifest.pull_request_repo
        or attestation.number != manifest.pull_request_number
        or attestation.merge_commit != manifest.merge_commit
    ):
        return 'hold', 'deployment_identity_mismatch', manifest, None, generation
    if manifest.classification == 'source_only':
        return 'pass', '', manifest, None, generation
    if manifest.classification != 'live_required':
        return 'hold', 'requirement_manifest_invalid', manifest, None, 0

    evidence = DeploymentEvidence.objects.filter(
        item=item,
        requirement_generation=manifest.requirement_generation,
        evidence_generation=generation,
    ).first()
    if not evidence:
        return 'hold', 'evidence_unreachable', manifest, None, generation

    if (
        evidence.merge_commit != manifest.merge_commit
        or evidence.environment != manifest.environment
        or evidence.service != manifest.service
        or evidence.workload != manifest.workload
        # PLS-521 P1 #3: evidence must come from the reviewed cluster + namespace.
        or evidence.cluster != manifest.cluster
        or evidence.namespace != manifest.namespace
        or not evidence.artifact_digest
        or not evidence.config_digest
        or _clean(evidence.build_provenance.get('merge_commit')).lower() != evidence.merge_commit
        or _clean(evidence.build_provenance.get('artifact_digest')).lower() != evidence.artifact_digest
    ):
        return 'hold', 'deployment_identity_mismatch', manifest, evidence, generation
    if (
        not evidence.ready
        or evidence.observed_generation < evidence.workload_generation
        or (not evidence.workload_uid and not evidence.rollout_revision)
    ):
        return 'hold', 'rollout_not_ready', manifest, evidence, generation

    for target in manifest.scrape_targets:
        entry = (evidence.target_health or {}).get(target)
        if not isinstance(entry, dict) or entry.get('up') is not True or not _identity_matches(entry, evidence):
            return 'hold', 'target_unhealthy', manifest, evidence, generation
    for series in manifest.metric_series:
        entry = (evidence.metric_series or {}).get(series)
        if entry is None:
            return 'hold', 'metric_missing', manifest, evidence, generation
        if not _sample_is_fresh(entry, evidence):
            return 'hold', 'metric_stale_generation', manifest, evidence, generation
    for rule, expected_digest in manifest.prometheus_rules.items():
        entry = (evidence.prometheus_rules or {}).get(rule)
        applied = _clean(entry.get('applied_semantic_digest')).lower() if isinstance(entry, dict) else ''
        applied_generation = entry.get('applied_generation') if isinstance(entry, dict) else None
        if applied != expected_digest or not isinstance(applied_generation, int) or applied_generation < 1:
            return 'hold', 'rule_digest_mismatch', manifest, evidence, generation
    return 'pass', '', manifest, evidence, generation


def current_source_only_requirement(item: Item) -> VerificationRequirementManifest | None:
    """Return whether the current immutable requirement selects source proof.

    This is the shared bridge between the PLS-521 requirement manifest and the
    PLAT-1351 terminal-close gate.  It intentionally applies the same local
    identity/current-generation checks as the deployed-evidence evaluator, but
    does not create a verdict or outbox row: terminal close is only selecting
    which already-frozen evidence path applies.
    """
    outcome, _reason, manifest, _evidence, _generation = _evaluate_current(item)
    if (
        outcome == 'pass'
        and manifest is not None
        and manifest.classification == 'source_only'
    ):
        return manifest
    return None


@transaction.atomic
def evaluate_and_record_gate(*, item: Item, transition=None, actor=None) -> GateDecision:
    """Perform the gate's single local decision read and emit verdict + outbox."""
    # Every manifest/evidence producer takes the same Item lock first.  The
    # evaluator therefore sees one current tuple and cannot pass an old
    # snapshot while an invalidation commits concurrently.
    locked = Item.objects.select_for_update().get(pk=item.pk)
    outcome, reason, manifest, evidence, evidence_generation = _evaluate_current(locked)
    if reason and reason not in HOLD_REASONS:
        raise AssertionError(f'Unknown deployed-evidence hold reason: {reason}')
    requirement_generation = manifest.requirement_generation if manifest else 0
    defaults = {
        'outcome': outcome,
        'reason': reason,
        'manifest_digest': manifest.manifest_digest if manifest else '',
        'evidence_digest': evidence.evidence_digest if evidence else '',
        'transition_id': getattr(transition, 'id', None),
        'actor_id': getattr(actor, 'id', None),
        'actor_email': _clean(getattr(actor, 'email', '')).lower(),
    }
    try:
        verdict, created = DeploymentEvidenceGateVerdict.objects.get_or_create(
            item=locked,
            requirement_generation=requirement_generation,
            evidence_generation=evidence_generation,
            defaults=defaults,
        )
    except IntegrityError:
        verdict = DeploymentEvidenceGateVerdict.objects.get(
            item=locked,
            requirement_generation=requirement_generation,
            evidence_generation=evidence_generation,
        )
        created = False
    if not created and (
        verdict.outcome != outcome
        or verdict.reason != reason
        or verdict.manifest_digest != defaults['manifest_digest']
        or verdict.evidence_digest != defaults['evidence_digest']
    ):
        raise ValidationError(
            'Deployment gate tuple changed after an idempotent verdict; invalidate evidence or requirements first.'
        )
    outbox, outbox_created = DeploymentEvidenceGateOutbox.objects.get_or_create(
        verdict=verdict,
        defaults={'payload': {
            'item_id': locked.pk,
            'item_key': locked.item_key,
            'requirement_generation': requirement_generation,
            'evidence_generation': evidence_generation,
            'outcome': outcome,
            'reason': reason,
            'manifest_digest': defaults['manifest_digest'],
            'evidence_digest': defaults['evidence_digest'],
        }},
    )
    if outbox_created:
        # PLS-521 P2: fail-loud delivery — DM the item owner/team on a HOLD and
        # stamp published_at. The on-commit hand-off keeps the outbox row the
        # authority; sweep_deployment_gate_outbox re-enqueues if this delay is lost.
        def _enqueue_gate_delivery(_oid=outbox.id):
            try:
                from tasks.tasks import deliver_deployment_gate_outbox
                deliver_deployment_gate_outbox.delay(str(_oid))
            except Exception:
                pass
        transaction.on_commit(_enqueue_gate_delivery)
    return GateDecision(outcome=outcome, reason=reason, verdict=verdict)
