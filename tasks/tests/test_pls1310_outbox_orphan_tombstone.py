"""PLS-1310 — hard item-delete must tombstone (not orphan/destroy) the outbox.

Covers the three legs of the fix:

1. The ORM delete path: the pre_delete receiver flips the item's outbox rows
   to ``disposition='orphaned'`` and the SET_NULL FK detaches them — the rows
   SURVIVE as inert forensic record instead of vanishing (old CASCADE) or
   surviving as live ``pending`` mutations for a dead item (raw path).
2. The cascade path (project hard-delete): every member item's stream is
   tombstoned the same way (Django emits pre_delete per collected instance).
3. The reconciliation pass: the ``tombstone_orphaned_outbox`` command claims
   rows the raw/SQL path orphaned (item row gone, outbox row still pending)
   without touching live items' streams — dry-run by default.

TransactionTestCase (not TestCase) because the orphan simulation needs raw
SQL under ``session_replication_role = replica`` inside a real transaction —
the established in-repo pattern (tasks.tasks bulk-update paths).
"""
import uuid
from io import StringIO

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TransactionTestCase

from tasks.models import Item, ItemStateEventOutbox, Project
from tasks.signals import _tombstone_item_outbox


def _make_project(key):
    return Project.objects.create(name=f'PLS-1310 {key}', key=key, org_id=1)


def _make_item(project, title):
    item = Item.objects.create(
        project=project, mode='task', status='todo', title=title,
    )
    item.refresh_from_db()
    return item


def _make_outbox_row(item, gen=1, token_suffix='1'):
    return ItemStateEventOutbox.objects.create(
        item=item,
        mutation_token=uuid.UUID('00000000-0000-0000-0000-00000000000' + token_suffix),
        state_generation=gen,
        changed_kinds=['status'],
        envelope={'specversion': '1.0', 'data': {'item_id': str(item.pk)}},
        writer_kind='typed',
    )


def _raw_delete_item_row(item_pk):
    """Simulate the operator raw/SQL delete path: bypass ORM cascades AND the
    DB FK enforcement (session_replication_role=replica, the established
    in-repo pattern) so the outbox rows survive as true orphans."""
    # SET LOCAL is transaction-scoped: without the wrapping atomic block the
    # two executes run as separate autocommit transactions and the replica
    # role is discarded before the DELETE — the DB FK (NO ACTION; Django's
    # on_delete is ORM-side) then rejects the delete (run 6870 IntegrityError).
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role = 'replica'")
            cursor.execute("DELETE FROM tasks_task WHERE id = %s", [str(item_pk)])


class OutboxTombstoneOnItemDeleteTests(TransactionTestCase):
    """Leg 1 — the ORM delete path tombstones + detaches the stream."""

    def test_hard_delete_tombstones_and_detaches_outbox(self):
        project = _make_project('TOMB1')
        item = _make_item(project, 'tombstone me')
        row_a = _make_outbox_row(item, gen=1, token_suffix='1')
        row_b = _make_outbox_row(item, gen=2, token_suffix='2')
        dead_id = item.pk

        item.delete()

        # Rows SURVIVE — detached, tombstoned, forensically intact.
        row_a.refresh_from_db()
        row_b.refresh_from_db()
        for row in (row_a, row_b):
            self.assertIsNone(row.item_id)
            self.assertEqual(row.disposition, 'orphaned')
            self.assertEqual(row.stale_reason, 'item-hard-deleted')
        # The original event payload is untouched (forensic trail).
        self.assertEqual(row_a.envelope['data']['item_id'], str(dead_id))

        # No pending mutations remain for the dead item id.
        self.assertFalse(
            ItemStateEventOutbox.objects.filter(
                item_id=dead_id, disposition='pending').exists())

    def test_tombstone_helper_is_idempotent(self):
        project = _make_project('TOMB2')
        item = _make_item(project, 'idempotent tombstone')
        _make_outbox_row(item, gen=1, token_suffix='1')

        self.assertEqual(_tombstone_item_outbox(item.pk), 1)
        # Re-entrant call must not restale or double-count.
        self.assertEqual(_tombstone_item_outbox(item.pk), 0)

    def test_project_cascade_tombstones_member_item_streams(self):
        project = _make_project('TOMB3')
        item_one = _make_item(project, 'cascade member one')
        item_two = _make_item(project, 'cascade member two')
        row_one = _make_outbox_row(item_one, gen=1, token_suffix='1')
        row_two = _make_outbox_row(item_two, gen=1, token_suffix='2')

        # Collector cascade (the project hard-delete path): pre_delete fires
        # per collected Item instance, so both streams tombstone + detach.
        project.delete()

        row_one.refresh_from_db()
        row_two.refresh_from_db()
        for row in (row_one, row_two):
            self.assertIsNone(row.item_id)
            self.assertEqual(row.disposition, 'orphaned')
            self.assertEqual(row.stale_reason, 'item-hard-deleted')


class OrphanedOutboxBackfillTests(TransactionTestCase):
    """Leg 3 — the reconciliation pass claims raw-path orphans only."""

    def _orphan_an_item(self, project, title, token_suffix='1'):
        item = _make_item(project, title)
        _make_outbox_row(item, gen=1, token_suffix=token_suffix)
        _raw_delete_item_row(item.pk)
        # The outbox row now points at a dead item id (true orphan).
        row = ItemStateEventOutbox.objects.get(
            mutation_token=uuid.UUID(
                '00000000-0000-0000-0000-00000000000' + token_suffix))
        self.assertIsNotNone(row.item_id)
        return row

    def test_dry_run_reports_without_writing(self):
        project = _make_project('ORPH1')
        orphan_row = self._orphan_an_item(project, 'dry-run orphan')

        out = StringIO()
        call_command('tombstone_orphaned_outbox', stdout=out)

        orphan_row.refresh_from_db()
        self.assertEqual(orphan_row.disposition, 'pending')  # untouched
        self.assertIn('dry-run', out.getvalue())

    def test_commit_tombstones_raw_path_orphans(self):
        project = _make_project('ORPH2')
        orphan_row = self._orphan_an_item(project, 'commit orphan')

        call_command('tombstone_orphaned_outbox', commit=True)

        orphan_row.refresh_from_db()
        self.assertEqual(orphan_row.disposition, 'orphaned')
        self.assertEqual(orphan_row.stale_reason, 'orphaned-by-hard-delete')
        # The command does NOT detach (the raw path already left the parent
        # gone); it only flips the disposition so no pending mutation survives.
        self.assertIsNotNone(orphan_row.item_id)

    def test_live_item_streams_untouched(self):
        project = _make_project('ORPH3')
        live_item = _make_item(project, 'live item stays pending')
        live_row = _make_outbox_row(live_item, gen=1, token_suffix='1')
        self._orphan_an_item(project, 'true orphan', token_suffix='2')

        call_command('tombstone_orphaned_outbox', commit=True)

        live_row.refresh_from_db()
        self.assertEqual(live_row.disposition, 'pending')
        self.assertEqual(live_row.stale_reason, '')
        self.assertEqual(live_row.item_id, live_item.pk)

    def test_already_orphaned_rows_not_reclaimed(self):
        project = _make_project('ORPH4')
        item = _make_item(project, 'receiver-tombstoned first')
        row = _make_outbox_row(item, gen=1, token_suffix='1')
        # Delete-time tombstone shape: already orphaned by the receiver.
        ItemStateEventOutbox.objects.filter(pk=row.pk).update(
            disposition='orphaned', stale_reason='item-hard-deleted')
        _raw_delete_item_row(item.pk)

        call_command('tombstone_orphaned_outbox', commit=True)

        row.refresh_from_db()
        # The receiver's marker is preserved — forensically distinguishable
        # from the backfill's 'orphaned-by-hard-delete'.
        self.assertEqual(row.stale_reason, 'item-hard-deleted')
