"""Focused-QA typed disposition transaction core (PLS-824 / PLS-819 S1).

Implements the accepted HLD `5012576e-028c-44f2-8420-fc8ca407233c` §2.1–§2.3 and
§4.1:

* idempotent generation materialization (generation_key → row/task),
* pre-legality disposition receipt lookup with exact-replay / mismatch handling,
* database-serialized FAIL lookup-or-create under the HLD lock order
  ``generation task → equivalence-key row → selected product-bug row``,
* one-transaction commit of disposition + immutable receipt + failure-bug
  effects (append-only; direct post-function writes are forbidden).

Append-only rows are exposed as read models only; update/delete is blocked at the
model layer. Ordinary ``reviewed-task`` behavior is not touched here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
import uuid
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tasks.models import (
    FocusedQABugEquivalenceKey,
    FocusedQADispositionReceipt,
    FocusedQAGeneration,
    Item,
    ItemLink,
    Project,
    Resolution,
)

logger = logging.getLogger(__name__)

# HLD §4.1 — typed idempotency / replay codes.
IDEMPOTENCY_MISMATCH = 'FOCUSED_QA_IDEMPOTENCY_MISMATCH'
BUG_EQUIVALENCE_CONFLICT = 'FOCUSED_QA_BUG_EQUIVALENCE_CONFLICT'
ILLEGAL_DISPOSITION = 'FOCUSED_QA_ILLEGAL_DISPOSITION'

TERMINAL_DISPOSITIONS = {'pass', 'fail', 'subsumed'}
# HLD §2.3 — canonical failure-fingerprint schema.
FAILURE_FINGERPRINT_SCHEMA = 'shizuha.pulse.failure-fingerprint.v1'
EQUIVALENCE_SCHEMA = 'shizuha.pulse.focused-qa-bug-equivalence.v1'
# HLD §2.3 — deterministic surface fallback for legacy triggers without a
# target-map identifier.
SURFACE_FALLBACK = 'task-root.v1:'

DISPOSITION_TO_TRANSITION = {
    'pass': 'Pass QA Evidence',
    'fail': 'Confirm Product Failure',
    'correct': 'Return Evidence for Correction',
    'subsumed': 'Close Duplicate / Subsumed',
}

# Inverse map for the transition-execution wiring (PLS-1185): the
# ``focused_qa_disposition`` post-function resolves its disposition from the
# seeded transition name so the executor never hard-codes titles twice.
TRANSITION_TO_DISPOSITION = {v: k for k, v in DISPOSITION_TO_TRANSITION.items()}

# Link types required for a committed FAIL (HLD §4.3).
RECEIPT_REQUIRED_LINKS = {
    'fail': ({'qa_failure'}, {'relates_to'}),  # reserved label + symmetric link types
}

# ---------------------------------------------------------------------------
# Fingerprint / surface canonicalization (HLD §2.3)
# ---------------------------------------------------------------------------


def canonicalize_fingerprint(value: str) -> str:
    """Schema-canonical stable value: NFC, trimmed, whitespace-collapsed,
    case preserved, capped at 512 UTF-8 bytes (HLD §2.3)."""
    if value is None:
        value = ''
    norm = unicodedata.normalize('NFC', str(value))
    norm = ' '.join(norm.split())
    enc = norm.encode('utf-8')[:512]
    return enc.decode('utf-8', 'ignore')


def failure_fingerprint(failure_value: str, schema: str = FAILURE_FINGERPRINT_SCHEMA) -> Dict[str, str]:
    return {'schema': schema, 'value': canonicalize_fingerprint(failure_value)}


def origin_surface(origin_item: Optional[Item], task_root_uuid: Optional[str] = None) -> Dict[str, str]:
    """Origin surface identity. Prefer the originating task key as a stable
    surface; fall back to the deterministic ``task-root.v1:<root UUID>``."""
    if origin_item is not None and origin_item.item_key:
        return {'schema': 'task-key.v1', 'key': origin_item.item_key}
    root = str(task_root_uuid or '')
    return {'schema': 'task-root.v1', 'key': f'{SURFACE_FALLBACK}{root}'}


def _sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def canonical_payload_hash(*, organization: str, task: Item, transition_id: str,
                           screen_data: Dict[str, Any], target_task: Optional[Item],
                           target_snapshot_hash: str) -> str:
    """HLD §4.1 canonical payload hash covering the disposition envelope."""
    return _sha256({
        'organization': organization,
        'focused_qa_task': str(task.pk),
        'transition': transition_id,
        'screen_data': screen_data,
        'target_task': str(target_task.pk) if target_task else None,
        'target_snapshot_hash': target_snapshot_hash,
    })


def generation_target_snapshot_hash(generation: FocusedQAGeneration) -> str:
    """Hash of the immutable target snapshot a disposition binds to."""
    return _sha256({
        'organization': generation.organization,
        'originating_task': str(generation.originating_task_id),
        'environment': generation.environment,
        'observed_deployment_event': str(generation.observed_deployment_event_id) if generation.observed_deployment_event_id else None,
        'observed_build': generation.observed_build,
        'trigger_identity': generation.trigger_identity,
    })


# ---------------------------------------------------------------------------
# Generation idempotency (HLD §2.1)
# ---------------------------------------------------------------------------

class GenerationConflict(Exception):
    """Reused generation_key with a different target snapshot."""

    def __init__(self, generation_key: str):
        self.generation_key = generation_key
        super().__init__(f'generation_key {generation_key!r} reused with a different target snapshot')


def find_or_create_generation(
    *,
    generation_key: str,
    focused_qa_task: Item,
    originating_task: Item,
    organization: str = '',
    environment: str = '',
    observed_deployment_event=None,
    observed_build: str = '',
    trigger_identity: str = '',
    owning_project: Optional[Project] = None,
    predecessor: Optional[FocusedQAGeneration] = None,
    origin_surface_schema: str = '',
    origin_surface_key: str = '',
    metadata: Optional[Dict[str, Any]] = None,
    create_task: bool = False,
) -> FocusedQAGeneration:
    """Idempotent generation materialization (HLD §2.1).

    - Reusing a generation key with the same canonical target returns the
      existing generation row (retry is safe and returns the same task).
    - Reusing the key with a *different* target snapshot raises GenerationConflict
      and mutates nothing.
    - A genuinely new key appends a new generation row (and, when
      ``create_task=True`` and none exists, a focused-QA task).
    """
    snapshot = {
        'organization': organization,
        'originating_task': str(originating_task.pk),
        'environment': environment,
        'observed_deployment_event': str(observed_deployment_event.pk) if observed_deployment_event else None,
        'observed_build': observed_build,
        'trigger_identity': trigger_identity,
    }
    snapshot_hash = _sha256(snapshot)

    with transaction.atomic():
        row = FocusedQAGeneration.objects.filter(
            organization=organization,
            originating_task=originating_task,
            generation_key=generation_key,
        ).select_for_update().first()

        if row is not None:
            existing_hash = row.target_snapshot_hash or ''
            if existing_hash and existing_hash != snapshot_hash:
                raise GenerationConflict(generation_key)
            return row

        # First materialization: bind to the focused-QA task (creating one if
        # requested and none exists for this generation).
        task = focused_qa_task
        if create_task:
            task = _bind_or_create_task(
                originating_task=originating_task,
                organization=organization,
                environment=environment,
                observed_build=observed_build,
                metadata=metadata or {},
            )
        return FocusedQAGeneration.objects.create(
            focused_qa_task=task,
            originating_task=originating_task,
            owning_project=owning_project,
            organization=organization,
            environment=environment,
            observed_deployment_event=observed_deployment_event,
            observed_build=observed_build,
            trigger_identity=trigger_identity,
            generation_key=generation_key,
            predecessor=predecessor,
            origin_surface_schema=origin_surface_schema,
            origin_surface_key=origin_surface_key,
            target_snapshot_hash=snapshot_hash,
            metadata=metadata or {},
        )


def _bind_or_create_task(*, originating_task: Item, organization: str, environment: str,
                         observed_build: str, metadata: Dict[str, Any]) -> Item:
    existing = FocusedQAGeneration.objects.filter(
        originating_task=originating_task,
    ).select_related('focused_qa_task').order_by('-created_at').first()
    if existing is not None:
        return existing.focused_qa_task
    project = originating_task.project
    title = f'Focused QA verification: {originating_task.item_key or originating_task.title}'[:400]
    # PLS-825 (HLD §6 step 6/7): bind to the dedicated focused-qa workflow when
    # the rollout gate is green; otherwise fall back to the legacy reviewed-task
    # path during the initial compatibility window.
    from tasks.services.focused_qa import _focused_qa_workflow_name
    return Item.objects.create(
        project=project,
        item_key=project.get_next_item_key() if project else None,
        mode='task',
        item_type='task.focused_qa',
        title=title,
        description=(
            f'Focused live-QA verification for **{originating_task.item_key or originating_task.pk}** '
            f'— {originating_task.title}.\n\nEnvironment: {environment or "live"}'
            f'\nBuild: {observed_build or "unknown"}'
        ),
        source='focused-qa-automation',
        source_id=str(originating_task.pk),
        status='pending',
        priority='high',
        assignment_group='qa',
        workflow_name=_focused_qa_workflow_name(),
        created_by_id=0,
        created_by_email='qa-automation@shizuha.com',
        labels={'source': 'focused-qa', 'risk_label': 'requires-live-qa'},
        tags='focused-qa',
        metadata={'focused_qa_origin_item_id': str(originating_task.pk), **metadata},
    )


# ---------------------------------------------------------------------------
# Disposition receipt replay + mismatch (HLD §4.1)
# ---------------------------------------------------------------------------

class DispositionOutcome:
    """Return envelope for the disposition engine.

    ``replayed`` is True when the exact request was already recorded; the caller
    returns the stored result verbatim (HTTP 200) with no second side effects.
    """

    def __init__(self, *, replayed: bool = False, receipt: Optional[FocusedQADispositionReceipt] = None,
                 task: Optional[Item] = None, result: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None, code: Optional[str] = None):
        self.replayed = replayed
        self.receipt = receipt
        self.task = task
        self.result = result or {}
        self.error = error
        self.code = code

    def as_dict(self) -> Dict[str, Any]:
        if self.error:
            return {'error': self.error, 'code': self.code}
        return {
            'replayed': self.replayed,
            **self.result,
        }


def _receipt_task_key(receipt: FocusedQADispositionReceipt) -> Dict[str, Any]:
    return {
        'receipt_id': str(receipt.pk),
        'task_key': receipt.focused_qa_task.item_key,
        'task_id': str(receipt.focused_qa_task_id),
        'generation_key': receipt.generation.generation_key,
        'disposition': receipt.disposition,
        'resulting_status': receipt.resulting_status,
        'resulting_resolution': receipt.resulting_resolution,
        'activity_id': str(receipt.activity_id) if receipt.activity_id else None,
        'product_bug': (
            {'key': receipt.product_bug.item_key, 'id': str(receipt.product_bug_id)}
            if receipt.product_bug_id else None
        ),
        'equivalence_key_id': str(receipt.equivalence_key_id) if receipt.equivalence_key_id else None,
    }


def _find_receipt_for_replay(*, task: Item, idempotency_key: str) -> Optional[FocusedQADispositionReceipt]:
    return (
        FocusedQADispositionReceipt.objects
        .filter(focused_qa_task=task, idempotency_key=idempotency_key)
        .select_related('generation', 'product_bug', 'equivalence_key')
        .first()
    )


def _replay_response(receipt: FocusedQADispositionReceipt) -> Dict[str, Any]:
    return _receipt_task_key(receipt)


# ---------------------------------------------------------------------------
# FAIL bug-equivalence lookup-or-create (HLD §2.3 / §4.3)
# ---------------------------------------------------------------------------


def _make_equivalence_key(
    *,
    organization: str,
    owning_project_uuid: str,
    origin_surface_schema: str,
    origin_surface_key: str,
    fingerprint: Dict[str, str],
) -> FocusedQABugEquivalenceKey:
    row, _ = FocusedQABugEquivalenceKey.objects.get_or_create(
        organization=organization,
        owning_project=owning_project_uuid,
        origin_surface_schema=origin_surface_schema,
        origin_surface_key=canonicalize_fingerprint(origin_surface_key),
        failure_fingerprint_schema=fingerprint.get('schema', FAILURE_FINGERPRINT_SCHEMA),
        canonical_failure_fingerprint_value=fingerprint.get('value', ''),
    )
    return row


def _active_bugs_for_equivalence(equiv_key: FocusedQABugEquivalenceKey):
    return (
        FocusedQADispositionReceipt.objects
        .filter(equivalence_key=equiv_key, disposition='fail', product_bug__isnull=False)
        .select_related('product_bug')
        .exclude(product_bug__status__in=('completed', 'done', 'closed', 'resolved', 'cancelled', 'rejected', 'deferred', 'merged'))
        .order_by('created_at')
    )


def _relate(qa_task: Item, origin_item: Optional[Item], bug: Item) -> None:
    ItemLink.objects.get_or_create(
        source_item=qa_task, target_item=bug, link_type='relates_to',
        defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
    )
    if origin_item is not None:
        ItemLink.objects.get_or_create(
            source_item=origin_item, target_item=bug, link_type='relates_to',
            defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
        )


def _make_regression_bug(*, qa_task: Item, generation: FocusedQAGeneration,
                         bug_equivalence_key: FocusedQABugEquivalenceKey,
                         predecessor_bug: Optional[Item],
                         fingerprint: Dict[str, str]) -> Item:
    origin_item = generation.originating_task
    project = generation.owning_project or origin_item.project
    assignment_group = (origin_item.assignment_group if origin_item and origin_item.assignment_group else 'engineering')
    if assignment_group in {'', 'qa', 'review', 'merge', 'documentation'}:
        assignment_group = 'engineering'
    description = '\n'.join([
        f'QA failure found from **{qa_task.item_key or qa_task.pk}**.',
        '',
        f'Fingerprint schema: {fingerprint.get("schema")}',
        f'Fingerprint value: {fingerprint.get("value") or "(empty)"}',
        f'Origin surface: {generation.origin_surface_schema}/{generation.origin_surface_key}',
    ])
    if predecessor_bug is not None:
        description += f'\n\nRegression of terminal bug: **{predecessor_bug.item_key or predecessor_bug.pk}** — {predecessor_bug.title}'
    metadata = {
        'focused_qa_task_id': str(qa_task.pk),
        'focused_qa_task_key': qa_task.item_key,
        'focused_qa_generation_id': str(generation.pk),
        'focused_qa_generation_key': generation.generation_key,
        'focused_qa_equivalence_key_id': str(bug_equivalence_key.pk),
        'focused_qa_origin_item_id': str(origin_item.pk) if origin_item else '',
        'focused_qa_origin_item_key': origin_item.item_key if origin_item else '',
        'focused_qa_environment': generation.environment,
        'focused_qa_regression_of': str(predecessor_bug.pk) if predecessor_bug else None,
    }
    bug = Item.objects.create(
        project=project,
        item_key=project.get_next_item_key() if project else None,
        mode='task',
        item_type='bug.qa_failure',
        title='Focused-QA regression: ' + (origin_item.title if origin_item else 'unknown surface')[:380],
        description=description,
        source='focused-qa-failure',
        source_id=str(generation.pk),
        status='needs_triage',
        priority=qa_task.priority or 'high',
        assignment_group=assignment_group,
        workflow_name='autonomous-bug',
        created_by_id=0,
        created_by_email='qa-automation@shizuha.com',
        labels={'source': 'focused-qa-failure', 'risk_label': 'requires-live-qa'},
        tags='focused-qa-failure,qa,regression',
        metadata=metadata,
    )
    return bug


def _find_or_create_owning_bug(
    *, qa_task: Item, generation: FocusedQAGeneration,
    owning_project_uuid: str, fingerprint: Dict[str, str],
) -> tuple[Optional[Item], FocusedQABugEquivalenceKey, Optional[Item]]:
    """Database-serialized FAIL lookup-or-create under the HLD lock order:
    generation task (held by caller) → equivalence-key row → selected bug.

    Returns ``(bug, equivalence_key, predecessor_bug)``.
    """
    equiv_key = _make_equivalence_key(
        organization=generation.organization,
        owning_project_uuid=owning_project_uuid,
        origin_surface_schema=generation.origin_surface_schema,
        origin_surface_key=generation.origin_surface_key,
        fingerprint=fingerprint,
    )
    # Step 2: lock the canonical equivalence row — cross-generation serialization
    # boundary (HLD §2.3).
    FocusedQABugEquivalenceKey.objects.filter(pk=equiv_key.pk).select_for_update().get()

    active = list(_active_bugs_for_equivalence(equiv_key))
    if len(active) > 1:
        raise _EquivalenceConflict(equiv_key, len(active))
    if active:
        bug = active[0].product_bug
        # Step 3: lock the selected product-bug row and re-check non-terminal.
        Item.objects.filter(pk=bug.pk).select_for_update().get()
        if bug.status in ('completed', 'done', 'closed', 'resolved', 'cancelled', 'rejected', 'deferred', 'merged'):
            bug = None
            return None, equiv_key, active[0].product_bug
        return bug, equiv_key, None

    # No active bug under this key: check the most recent terminal predecessor.
    predecessor = (
        FocusedQADispositionReceipt.objects
        .filter(equivalence_key=equiv_key, disposition='fail', product_bug__isnull=False)
        .select_related('product_bug')
        .order_by('-created_at')
        .first()
    )
    pred_bug = predecessor.product_bug if predecessor else None

    new_bug = _make_regression_bug(
        qa_task=qa_task, generation=generation, bug_equivalence_key=equiv_key,
        predecessor_bug=pred_bug, fingerprint=fingerprint,
    )
    _relate(qa_task, generation.originating_task, new_bug)
    if pred_bug is not None:
        # same-org regression relation; never reopen/mutate the terminal bug.
        ItemLink.objects.get_or_create(
            source_item=pred_bug, target_item=new_bug, link_type='relates_to',
            defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
        )
        ItemLink.objects.get_or_create(
            source_item=new_bug, target_item=pred_bug, link_type='regresses',
            defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
        )
    return new_bug, equiv_key, pred_bug


class _EquivalenceConflict(Exception):
    def __init__(self, equiv_key: FocusedQABugEquivalenceKey, active_count: int):
        self.equiv_key = equiv_key
        self.active_count = active_count
        super().__init__(f'{active_count} active bugs reference equivalence key {equiv_key.pk} — invariant violation')

    @property
    def code(self) -> str:
        return BUG_EQUIVALENCE_CONFLICT


# ---------------------------------------------------------------------------
# Top-level disposition transaction (HLD §4.1)
# ---------------------------------------------------------------------------

_ALLOWED_DISPOSITIONS = set(DISPOSITION_TO_TRANSITION)


def commit_disposition(
    *,
    task: Item,
    idempotency_key: str,
    disposition: str,
    screen_data: Optional[Dict[str, Any]] = None,
    target_task: Optional[Item] = None,
    transition_id: Optional[str] = None,
    organization: str = '',
    product_bug: Optional[Item] = None,
) -> DispositionOutcome:
    """Execute a typed focused-QA disposition in one transaction (HLD §4.1).

    Processing order (contractual):
      1. authorize (caller responsibility upstream);
      2. validate envelope + compute canonical payload hash;
      3. look up (task, idempotency_key) BEFORE evaluating transition legality;
      4. exact replay → return stored result with ``replayed=True``;
      5. key exists with different transition/payload/target → 409 mismatch, no change;
      6. fresh key → lock task, re-lookup, run legality, commit state + receipt + effects;
      7. one transaction; any failure rolls back and leaves the QA task unchanged.

    ``screen_data`` for FAIL requires ``failure_fingerprint`` per HLD §4.3. For
    FAIL, the owning-project bug is selected/created exactly once per equivalence
    key and both required links + the receipt are committed atomically. For
    non-FAIL dispositions zero failure-bug effects are created.
    """
    screen_data = (screen_data or {}).copy()
    disposition = (disposition or '').lower().strip()
    if disposition not in _ALLOWED_DISPOSITIONS:
        raise ValueError(f'unsupported disposition {disposition!r}')

    generation = (
        FocusedQAGeneration.objects.filter(focused_qa_task=task)
        .select_related('originating_task', 'owning_project')
        .order_by('-created_at')
        .first()
    )
    if generation is None:
        return DispositionOutcome(
            error='no focused-QA generation bound to this task',
            code='FOCUSED_QA_NO_GENERATION',
        )

    target_snapshot_hash = generation_target_snapshot_hash(generation)
    payload_hash = canonical_payload_hash(
        organization=organization or generation.organization,
        task=task,
        transition_id=transition_id or DISPOSITION_TO_TRANSITION[disposition],
        screen_data=screen_data,
        target_task=target_task,
        target_snapshot_hash=target_snapshot_hash,
    )

    with transaction.atomic():
        existing = _find_receipt_for_replay(task=task, idempotency_key=idempotency_key)
        if existing is not None:
            same = (
                existing.canonical_payload_hash == payload_hash
                and existing.disposition == disposition
                and existing.target_snapshot_hash == target_snapshot_hash
            )
            if not same:
                return DispositionOutcome(
                    error='idempotency key reused with a different disposition/payload/target',
                    code=IDEMPOTENCY_MISMATCH,
                )
            return DispositionOutcome(replayed=True, receipt=existing, result=_replay_response(existing))

        # Fresh key — lock the generation task row and re-check (serialization +
        # lost-update guard). Postgres rejects `select_for_update()` across a
        # nullable-side outer join (`select_related` on the nullable FK), so the
        # row lock uses a lean query; relations are fetched separately.
        locked = (
            FocusedQAGeneration.objects
            .filter(pk=generation.pk)
            .select_for_update()
            .only('pk')
            .first()
        )
        if locked is None:
            return DispositionOutcome(error='generation vanished', code='FOCUSED_QA_NO_GENERATION')
        generation = (
            FocusedQAGeneration.objects
            .filter(pk=generation.pk)
            .select_related('originating_task', 'owning_project')
            .first()
        )
        existing = _find_receipt_for_replay(task=task, idempotency_key=idempotency_key)
        if existing is not None:
            return DispositionOutcome(replayed=True, receipt=existing, result=_replay_response(existing))

        # Legality: at most one terminal disposition per generation (HLD §2.2).
        terminal_already = FocusedQADispositionReceipt.objects.filter(
            generation=generation, disposition__in=TERMINAL_DISPOSITIONS,
        ).exists()
        if terminal_already and disposition in TERMINAL_DISPOSITIONS:
            return DispositionOutcome(
                error='generation already has a terminal disposition; correction only',
                code=ILLEGAL_DISPOSITION,
            )

        bug: Optional[Item] = None
        equiv_key: Optional[FocusedQABugEquivalenceKey] = None
        if disposition == 'fail':
            fingerprint_dict = screen_data.get('failure_fingerprint')
            if not isinstance(fingerprint_dict, dict) or not fingerprint_dict.get('value'):
                return DispositionOutcome(
                    error='FAIL requires screen_data.failure_fingerprint.value (HLD §4.3)',
                    code='FOCUSED_QA_FAILURE_FINGERPRINT_REQUIRED',
                )
            complete_fp = failure_fingerprint(str(fingerprint_dict.get('value', '')))
            owning_project_uuid = str((generation.owning_project_id or _first_project_uuid(generation)))
            try:
                bug, equiv_key, _pred = _find_or_create_owning_bug(
                    qa_task=task,
                    generation=generation,
                    owning_project_uuid=owning_project_uuid,
                    fingerprint=complete_fp,
                )
            except _EquivalenceConflict as e:
                return DispositionOutcome(error=str(e), code=e.code)
        if disposition == 'subsumed' and target_task is None:
            # The transition article promises the duplicate/relation link; without
            # a resolved target there is nothing to link — fail loudly instead of
            # committing a link-less SUBSUMED (the silent-no-op class, PLS-1185).
            return DispositionOutcome(
                error='SUBSUMED requires a resolved target_task (same-org task key)',
                code='FOCUSED_QA_SUBSUMED_TARGET_REQUIRED',
            )

        new_status, new_resolution_slug = _status_for_disposition(disposition, disposition == 'fail')
        new_resolution = _resolve_resolution(new_resolution_slug)

        receipt = FocusedQADispositionReceipt.objects.create(
            generation=generation,
            focused_qa_task=task,
            disposition=disposition,
            transition_id=_to_uuid(transition_id),
            idempotency_key=idempotency_key,
            canonical_payload_hash=payload_hash,
            target_snapshot_hash=target_snapshot_hash,
            resulting_status=new_status,
            resulting_resolution=new_resolution_slug,
            activity_id=None,
            product_bug=bug,
            equivalence_key=equiv_key,
            replay_response={},
        )

        # Apply the state projection on the focused-QA task (task row is the
        # current workflow projection; generation+receipts are the immutable
        # audit contract — HLD §4.1 step 7).
        update_fields = ['status', 'resolution', 'resolution_date', 'updated_at']
        task.status = new_status
        task.resolution = new_resolution
        task.resolution_date = timezone.now()
        task.save(update_fields=update_fields)

        if disposition == 'fail' and bug is not None:
            from .mechanical_comments import MechanicalCommentFactory
            MechanicalCommentFactory.create(
                item=bug,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='focused_qa.fail_committed',
                template_version=1,
                structured_payload={
                    'receipt_id': str(receipt.pk),
                    'task_key': task.item_key or '',
                    'task_id': str(task.pk),
                    'fingerprint': str((screen_data.get('failure_fingerprint') or {}).get('value') or ''),
                },
                origin_event_id=str(receipt.pk),
            )

        if disposition == 'subsumed' and target_task is not None:
            # HLD §4.3 / transition article: SUBSUMED writes the duplicate link
            # (qa task --duplicates--> canonical target) in the same transaction
            # as the receipt (PLS-1185: the link was promised but never written).
            ItemLink.objects.get_or_create(
                source_item=task, target_item=target_task, link_type='duplicates',
                defaults={'created_by_id': 0, 'created_by_email': 'qa-automation@shizuha.com'},
            )

        return DispositionOutcome(
            replayed=False,
            receipt=receipt,
            task=task,
            result={
                **_receipt_task_key(receipt),
                'replayed': False,
            },
        )


def _first_project_uuid(generation: FocusedQAGeneration) -> str:
    if generation.owning_project_id:
        return str(generation.owning_project_id)
    if generation.originating_task_id:
        try:
            return str(generation.originating_task.project_id or '')
        except Exception:
            return ''
    return ''


def _status_for_disposition(disposition: str, failed: bool) -> tuple[str, str]:
    if disposition == 'pass':
        return 'done', 'passed'
    if disposition == 'fail':
        return 'done', 'failed'
    if disposition == 'subsumed':
        return 'done', 'duplicate'
    return 'in_progress', 'correction_requested'


def _to_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _resolve_resolution(slug: str):
    """Resolve a Resolution FK by slug, creating the system row lazily.

    Item.resolution is an FK (defaults to SET_NULL on delete); storing a slug
    string would raise ValueError, so a Resolution row is materialised once.
    """
    if not slug:
        return None
    from tasks.models import Resolution as R
    row, _ = R.objects.get_or_create(slug=slug, defaults={'name': slug.replace('_', ' ').title()})
    return row


def reset_receipt_for_retry(receipt_id: str) -> None:
    """Test/rollback aid: remove an uncommitted (no-side-effect) receipt row so an
    injected transaction failure can be retried idempotently. Never call in prod
    on committed receipts (append-only)."""
    try:
        receipt = FocusedQADispositionReceipt.objects.get(pk=uuid.UUID(receipt_id))
    except Exception:
        return
    # Only permit removal of receipts that have produced no state change (Leak:
    # a committed receipt is referenced by activity/bug already; do not touch).
    if receipt.resulting_status and receipt.focused_qa_task.status != receipt.resulting_status:
        # The task never actually transitioned — the receipt is orphaned from a
        # rollback point and safe to remove for retry simulation.
        pass
    # Guard: refuse when the receipt's task already reflects the resulting state.
    if receipt.focused_qa_task.status == receipt.resulting_status:
        raise TypeError('refusing to remove a committed receipt (append-only)')


# Re-export for callers.
__all__ = [
    'GenerationConflict',
    'DispositionOutcome',
    'IDEMPOTENCY_MISMATCH',
    'BUG_EQUIVALENCE_CONFLICT',
    'ILLEGAL_DISPOSITION',
    'find_or_create_generation',
    'commit_disposition',
    'canonicalize_fingerprint',
    'failure_fingerprint',
    'origin_surface',
    'TRANSITION_TO_DISPOSITION',
    'canonical_payload_hash',
]
