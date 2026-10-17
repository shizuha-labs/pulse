"""PLAT-6978: migration 0167 backfill survives an outbox-ahead item.

The PLS-467 trigger's generic fallback (migration 0133) inserts a generic outbox
event at ``state_generation = OLD + 1`` on every tracked-field save. For a
legacy focused-QA task whose outbox is already ahead of its ``state_generation``
(e.g. item 28329: outbox max 17 > state_generation 16 from a pre-existing typed
event), the migration 0167 ``task.save()`` collided with the existing
``(item_id, state_generation)`` row → UniqueViolation → migrate job aborted.

Fix: arm the typed-mutation marker (``pulse.item_state_mutation_token`` +
``pulse.item_state_mutation_item_id``) before the backfill save so the trigger's
marked_typed path accepts the write WITHOUT inserting a generic event; bump
``state_generation`` to OLD + 1 (the trigger's typed-path invariant) so the
outbox-ahead item aligns to its outbox max instead of colliding.
"""
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from tasks.models import Item, ItemStateEventOutbox, Project


class Migration0167OutboxAheadTests(TransactionTestCase):
    reset_sequences = True

    def _migration_apps(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0166_seed_focused_qa_workflow'),
        ])
        return state.apps

    def _run_forward(self, apps):
        migration = import_module(
            'tasks.migrations.0167_backfill_focused_qa_generations'
        )
        schema_editor = SimpleNamespace(connection=connection)
        # RunPython is atomic by default; the typed-mutation GUCs are
        # transaction-local (set_config is_local=true), so the backfill save
        # must run inside the same transaction to see them.
        with transaction.atomic():
            migration.backfill_focused_qa(apps, schema_editor)

    def _make_legacy_task(self, *, org_id=1):
        proj = Project.objects.create(
            name='FQA Migration', key='FQAM', org_id=org_id,
        )
        item = Item.objects.create(
            project=proj,
            mode='task',
            status='todo',
            source='focused-qa-automation',
            workflow_name='focused-qa-automation',
            title='Legacy focused-QA task',
            metadata={
                'focused_qa_origin_item_id': str(proj.pk),
                'focused_qa_environment': 'staging',
                'focused_qa_trigger': 'legacy',
            },
        )
        item.refresh_from_db()
        return item

    def test_backfill_survives_outbox_ahead_item(self):
        item = self._make_legacy_task()
        current_gen = item.state_generation
        # Manufacture the pre-existing outbox-ahead inconsistency: an outbox
        # event at current+1 while the task row still holds `current`.
        ItemStateEventOutbox.objects.create(
            item=item,
            mutation_token='00000000-0000-0000-0000-000000000001',
            state_generation=current_gen + 1,
            changed_kinds=['status'],
            envelope={'specversion': '1.0'},
            writer_kind='typed',
        )
        item.refresh_from_db()
        self.assertEqual(item.state_generation, current_gen)

        apps = self._migration_apps()
        # Must not raise UniqueViolation on (item, current_gen + 1).
        self._run_forward(apps)

        item.refresh_from_db()
        # The backfill aligned state_generation to the outbox max without
        # re-inserting the colliding event.
        self.assertEqual(item.state_generation, current_gen + 1)
        self.assertEqual(item.workflow_name, 'focused-qa')
        self.assertEqual(
            (item.metadata or {}).get('focused_qa_disposition_contract'), 2,
        )
        # The generation row was materialized.
        HistoricalFocusedQA = apps.get_model('tasks', 'FocusedQAGeneration')
        self.assertTrue(
            HistoricalFocusedQA.objects.filter(
                focused_qa_task_id=item.pk,
            ).exists()
        )

    def test_backfill_healthy_item_no_outbox_conflict(self):
        item = self._make_legacy_task()
        apps = self._migration_apps()
        self._run_forward(apps)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, 'focused-qa')
        self.assertEqual(item.state_generation, item.state_generation)
        # Exactly one outbox event exists for the item (the backfill's typed
        # marker path inserts none beyond what the task already had).
        self.assertEqual(
            ItemStateEventOutbox.objects.filter(item=item).count(), 0,
        )
