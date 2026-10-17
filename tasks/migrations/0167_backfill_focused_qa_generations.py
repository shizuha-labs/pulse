"""PLS-825 (PLS-819 S2): lossless backfill of legacy focused-QA generations.

For every non-terminal ``source=focused-qa-automation`` task created by the
legacy generator, materialize exactly one immutable ``FocusedQAGeneration`` row
with a deterministic ``legacy:<task UUID>`` key. The focused-QA task is the
protected one-to-one projection; the generation carries the snapshot of the
available organization / origin / environment / deployment metadata and the
deterministic ``task-root.v1`` origin surface (HLD §2.3 fallback — legacy
triggers carry no target-map surface). An absent deployment identity is marked
explicitly incomplete in generation metadata — never invented.

Task rows are preserved losslessly: status, owner/group, stashed verifier,
metadata, links, comments, and workflow history are untouched. The only task
mutations are (a) switching the current workflow projection to the dedicated
``focused-qa`` workflow (HLD §2 — non-terminal focused-qa-automation items are
migrated to it; history is preserved, only the active workflow name changes) and
(b) appending the ``focused_qa_disposition_contract=2`` marker that declares the
task is now governed by the disposition contract.

Equivalence-key backfill (HLD §2.3) is complete-only: a legacy bug is keyed only
when its stored organization, owning project, origin surface, fingerprint
schema, and canonical value are all present. No equivalence is ever inferred
from similar titles or prose. Legacy ``focused-qa-failure`` bugs carry no
failure fingerprint, so they remain auditable but unkeyed and cannot be silently
selected by the new dedupe path. If a complete canonical key already maps to
multiple active bugs, the migration records the conflict so the enable gate
fails until it is reconciled explicitly (HLD §6).

No disposition receipts are fabricated for historical generic Request Changes;
terminal historical items stay immutable on ``reviewed-task`` and are not
backfilled. Reverse is a no-op (HLD §6 — no destructive reverse migration).
"""
import hashlib
import json
import uuid

from django.db import connection, migrations

# Matches tasks/services/focused_qa.py NON_ACTIVE_STATUSES (terminal states).
TERMINAL_STATUS_SLUGS = {
    'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred',
}

# HLD §2.3 deterministic surface fallback for legacy triggers without a
# target-map identifier (mirrors focused_qa_disposition.SURFACE_FALLBACK).
SURFACE_FALLBACK = 'task-root.v1:'

# Contract version marker appended to backfilled task metadata.
DISPOSITION_CONTRACT = 2


def _sha256(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
    ).hexdigest()


def _snapshot_hash(organization, originating_task_id, environment,
                   observed_deployment_event_id, observed_build, trigger_identity):
    """Hash of the immutable target snapshot a disposition binds to — must stay
    byte-identical to focused_qa_disposition.generation_target_snapshot_hash."""
    return _sha256({
        'organization': organization,
        'originating_task': str(originating_task_id),
        'environment': environment,
        'observed_deployment_event': (
            str(observed_deployment_event_id) if observed_deployment_event_id else None
        ),
        'observed_build': observed_build,
        'trigger_identity': trigger_identity,
    })


def _to_uuid(value):
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def backfill_focused_qa(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    DeploymentEvent = apps.get_model('tasks', 'DeploymentEvent')
    FocusedQAGeneration = apps.get_model('tasks', 'FocusedQAGeneration')
    FocusedQABugEquivalenceKey = apps.get_model('tasks', 'FocusedQABugEquivalenceKey')

    # ------------------------------------------------------------------
    # Part A — one generation per non-terminal legacy focused-QA task.
    # ------------------------------------------------------------------
    legacy_tasks = Item.objects.filter(source='focused-qa-automation').exclude(
        status__in=TERMINAL_STATUS_SLUGS,
    ).order_by('pk')

    generations_created = 0
    tasks_tagged = 0
    incomplete_deployment = 0

    for task in legacy_tasks.iterator():
        if FocusedQAGeneration.objects.filter(focused_qa_task=task).exists():
            # Idempotent re-run: a generation already exists for this task.
            continue

        meta = task.metadata or {}

        # Originating task: prefer the stored origin id; fall back to the task
        # itself (degenerate legacy case) — never invent a different origin.
        # Item primary keys are integers; tolerate UUID-typed values too.
        originating_task = task
        origin_ref = meta.get('focused_qa_origin_item_id')
        origin = None
        if origin_ref:
            origin_ref_str = str(origin_ref).strip()
            if origin_ref_str.isdigit():
                origin = Item.objects.filter(pk=origin_ref_str).first()
            if origin is None:
                origin_uuid = _to_uuid(origin_ref_str)
                if origin_uuid is not None:
                    origin = Item.objects.filter(pk=origin_uuid).first()
        if origin is not None:
            originating_task = origin

        project = task.project
        organization = str(project.org_id) if (project and project.org_id) else ''
        environment = meta.get('focused_qa_environment') or ''

        # Deployment identity: only when the legacy task actually recorded one.
        # Absent deployment identity is marked incomplete, never fabricated.
        observed_deployment_event = None
        deploy_uuid = _to_uuid(meta.get('focused_qa_deployment_event_id'))
        if deploy_uuid is not None:
            observed_deployment_event = DeploymentEvent.objects.filter(pk=deploy_uuid).first()

        observed_build = ''
        trigger_identity = meta.get('focused_qa_trigger') or ''

        # Deterministic task-root.v1 surface fallback (HLD §2.3): legacy
        # triggers carry no target-map surface, so the root task UUID is the
        # stable surface identity.
        origin_surface_schema = 'task-root.v1'
        origin_surface_key = f'{SURFACE_FALLBACK}{originating_task.pk}'

        snapshot_hash = _snapshot_hash(
            organization,
            originating_task.pk,
            environment,
            observed_deployment_event.pk if observed_deployment_event else None,
            observed_build,
            trigger_identity,
        )

        generation_meta = {
            'legacy_backfill': True,
            'legacy_backfill_source_status': task.status,
            'legacy_backfill_source_workflow': task.workflow_name or '',
            'legacy_backfill_origin_surface': (
                'task-root.v1 (deterministic fallback; legacy trigger had no '
                'target-map surface)'
            ),
            'legacy_backfill_incomplete_deployment': observed_deployment_event is None,
        }

        FocusedQAGeneration.objects.create(
            focused_qa_task=task,
            originating_task=originating_task,
            owning_project=project,
            organization=organization,
            environment=environment,
            observed_deployment_event=observed_deployment_event,
            observed_build=observed_build,
            trigger_identity=trigger_identity,
            generation_key=f'legacy:{task.pk}',
            origin_surface_schema=origin_surface_schema,
            origin_surface_key=origin_surface_key,
            target_snapshot_hash=snapshot_hash,
            metadata=generation_meta,
        )
        generations_created += 1
        if observed_deployment_event is None:
            incomplete_deployment += 1

        # Append the disposition-contract marker and switch the current workflow
        # projection to the dedicated focused-qa workflow (HLD §2), preserving
        # all existing metadata and history losslessly. These are the only task
        # mutations.
        new_meta = dict(meta)
        new_meta['focused_qa_disposition_contract'] = DISPOSITION_CONTRACT
        task.metadata = new_meta
        task.workflow_name = 'focused-qa'
        # PLAT-6978: the historical model has no Item.save() hook, so this save
        # would otherwise hit the PLS-467 trigger's generic fallback, which
        # allocates state_generation = OLD + 1 and inserts a generic outbox
        # event. For an outbox-ahead item (e.g. 28329: outbox max 17 >
        # state_generation 16 from a pre-existing typed event) that insert
        # collides with the existing (item_id, state_generation) row →
        # UniqueViolation → migration aborts. Arm the typed-mutation marker so
        # the trigger's marked_typed path accepts the backfill WITHOUT inserting
        # a generic event; the trigger requires NEW.state_generation = OLD + 1,
        # so bump it explicitly. This also aligns the outbox-ahead item's
        # state_generation to its outbox max without re-inserting the event.
        token = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('pulse.item_state_mutation_token', %s, true), "
                "set_config('pulse.item_state_mutation_item_id', %s, true)",
                [str(token), str(task.pk)],
            )
        task.state_generation = (task.state_generation or 0) + 1
        task.save(update_fields=[
            'metadata', 'workflow_name', 'state_generation', 'updated_at',
        ])
        tasks_tagged += 1

    # ------------------------------------------------------------------
    # Part B — complete-only equivalence-key backfill (HLD §2.3).
    # ------------------------------------------------------------------
    legacy_bugs = Item.objects.filter(source='focused-qa-failure').order_by('pk')

    equivalence_created = 0
    equivalence_incomplete = 0
    equivalence_conflicts = []

    for bug in legacy_bugs.iterator():
        meta = bug.metadata or {}
        project = bug.project

        # Completeness gate: organization, owning project, origin surface,
        # fingerprint schema, and canonical value must ALL be present. Legacy
        # bugs store no failure fingerprint, so they are auditable but unkeyed.
        organization = str(project.org_id) if (project and project.org_id) else ''
        owning_project = str(project.pk) if project else ''
        origin_surface_schema = meta.get('focused_qa_origin_surface_schema') or ''
        origin_surface_key = meta.get('focused_qa_origin_surface_key') or ''
        fingerprint_schema = meta.get('focused_qa_failure_fingerprint_schema') or ''
        fingerprint_value = meta.get('focused_qa_failure_fingerprint_value') or ''

        if not (
            organization
            and owning_project
            and origin_surface_schema
            and origin_surface_key
            and fingerprint_schema
            and fingerprint_value
        ):
            equivalence_incomplete += 1
            continue

        key, created = FocusedQABugEquivalenceKey.objects.get_or_create(
            organization=organization,
            owning_project=owning_project,
            origin_surface_schema=origin_surface_schema,
            origin_surface_key=origin_surface_key,
            failure_fingerprint_schema=fingerprint_schema,
            canonical_failure_fingerprint_value=fingerprint_value,
        )
        if created:
            equivalence_created += 1

        # Enable-gate conflict detection: one canonical key must never map to
        # multiple active bugs. Record any such conflict so the enable gate
        # fails until it is reconciled explicitly (HLD §6).
        active_bugs = Item.objects.filter(
            source='focused-qa-failure',
            metadata__focused_qa_equivalence_key=str(key.pk),
        ).exclude(status__in=TERMINAL_STATUS_SLUGS)
        if active_bugs.count() > 1:
            equivalence_conflicts.append(str(key.pk))

    print(
        'PLS-825 backfill: %d generations created (%d incomplete deployment), '
        '%d tasks tagged contract=%d; equivalence keys created=%d, '
        'incomplete-unkeyed=%d, conflicts=%d'
        % (
            generations_created,
            incomplete_deployment,
            tasks_tagged,
            DISPOSITION_CONTRACT,
            equivalence_created,
            equivalence_incomplete,
            len(equivalence_conflicts),
        )
    )
    if equivalence_conflicts:
        print('PLS-825 enable-gate conflicts (reconcile before FAIL): %s' % equivalence_conflicts)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0166_seed_focused_qa_workflow'),
    ]

    operations = [
        migrations.RunPython(backfill_focused_qa, noop),
    ]
