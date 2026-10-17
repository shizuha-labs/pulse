"""HIVE-1153 P1 #3 arming (HIVE-1174 (b); akira HIVE-1176 confirm): the split
substrate write-guard triggers + the ORM arming that feeds them.

These exercise the ``tasks_item_substrate_update_guard`` (BEFORE UPDATE) and
``tasks_item_substrate_insert_guard`` (AFTER INSERT) triggers directly at the SQL/GUC
layer — the security-critical contract that under ENFORCE, a concrete-owner authorizing
write MUST consume the one-use ORM-armed receipt (matched by tx-local GUC UUID + exact
tuple + assignment_generation + requirement_key + write_input_digest), so QuerySet.update
/ raw SQL (which arm nothing, set no GUC) fail closed — while off/shadow, owner-clears,
and no-op (SET x=x) writes stay exempt. A companion test drives the real ORM arming
helper end to end.

The owner is installed once under mode=off + the org-isolation proof
(``tasks_guard_item_assignment_org``); the guarded writes then change ``title`` (a guard
column the org-isolation trigger ignores because assignee/project/group are unchanged),
so these tests isolate the *substrate* trigger from the org fence.

Postgres-only: the guards are plpgsql triggers with ``FOR UPDATE`` + session GUCs that
SQLite has no equivalent for.
"""
import os
import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.backends.signals import connection_created
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, SubstrateWriteReceipt
from tasks.services.workflow_service import WorkflowService

ENFORCE_ENV = {'PULSE_SUBSTRATE_CAP_MODE': 'enforce'}
DIGEST = 'd' * 64
OWNER = 'dev@shizuha.com'
GOLDEN_RK2 = json.loads(
    (Path(__file__).parent / 'fixtures' / 'hive_capability_evidence_v2_golden.json').read_text()
)['request']['requirement_key']


def _pg():
    return connection.vendor == 'postgresql'


def _set_org_proof(cursor, *, project_id, subject_id, email, group=''):
    """Match tasks_guard_item_assignment_org's expected proof for this owner tuple."""
    cursor.execute(
        "SELECT set_config('pulse.assignment_org_proof', concat_ws('|', "
        "(SELECT org_id FROM tasks_project WHERE id=%s)::text, %s::text, %s::text, "
        "tasks_canonical_assignment_email(%s), tasks_canonical_team_key(%s)), true)",
        [project_id, project_id, subject_id, email, group])


class SubstrateWriteTriggerMatrixTests(TestCase):
    """Raw-SQL/GUC matrix against the two split substrate guards."""

    def setUp(self):
        self.project = Project.objects.create(
            name='SWP', key='SWP', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='sw', project=self.project, workflow_name='')
        self.item.refresh_from_db()
        self.wf = self.item.workflow_name          # auto-attached 'simple'
        self.status = self.item.status
        self._install_owner()

    # --- helpers ---------------------------------------------------------------
    def _install_owner(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','off',true)")
            _set_org_proof(cursor, project_id=self.project.id, subject_id=88, email=OWNER)
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=88, assignee_email=%s WHERE id=%s",
                [OWNER, self.item.pk])
        self.item.refresh_from_db()

    def _expected_update_digest(self, new_title):
        """The digest the UPDATE guard will compute for OLD(current row) -> NEW
        (same content, title=new_title) — via the same SQL function (P1 #3)."""
        with connection.cursor() as c:
            c.execute(
                "SELECT tasks_substrate_reqinput_digest("
                "(SELECT labels FROM tasks_task WHERE id=%(pk)s),(SELECT tags FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT metadata FROM tasks_task WHERE id=%(pk)s),(SELECT title FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT description FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT labels FROM tasks_task WHERE id=%(pk)s),(SELECT tags FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT metadata FROM tasks_task WHERE id=%(pk)s),%(nt)s,(SELECT description FROM tasks_task WHERE id=%(pk)s))",
                {'pk': self.item.pk, 'nt': new_title})
            return c.fetchone()[0]

    def _expected_insert_digest(self):
        """The digest the INSERT guard computes: NULL OLD block, NEW = the copied
        row's requirement inputs (title stays 'sw')."""
        with connection.cursor() as c:
            c.execute(
                "SELECT tasks_substrate_reqinput_digest(NULL,NULL,NULL,NULL,NULL,"
                "(SELECT labels FROM tasks_task WHERE id=%(pk)s),(SELECT tags FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT metadata FROM tasks_task WHERE id=%(pk)s),(SELECT title FROM tasks_task WHERE id=%(pk)s),"
                "(SELECT description FROM tasks_task WHERE id=%(pk)s))",
                {'pk': self.item.pk})
            return c.fetchone()[0]

    def _arm_receipt(self, *, item=None, item_id_null=False, generation=0,
                     requirement_key='', digest=None, new_title='t2', consumed=False,
                     expired=False, assignee_email=OWNER, subject_id=88, group='',
                     status=None, org=1, project=None):
        if digest is None:
            digest = (self._expected_insert_digest() if item_id_null
                      else self._expected_update_digest(new_title))
        return SubstrateWriteReceipt.objects.create(
            id=uuid.uuid4(),
            item=None if item_id_null else (item or self.item),
            organization_id=org,
            project_id=(self.project.id if project is None else project),
            assignment_group=group,
            workflow_name=self.wf,
            status=self.status if status is None else status,
            subject_id=subject_id,
            assignee_email=assignee_email,
            assignment_generation=generation,
            requirement_key=requirement_key,
            write_input_digest=digest,
            mode='enforce',
            consumed_at=(timezone.now() if consumed else None),
            expires_at=timezone.now() + timedelta(seconds=(-10 if expired else 30)),
        )

    def _gucs(self, cursor, *, mode='enforce', rid=None, generation=0,
              requirement_key=''):
        # NOTE: no digest GUC — the trigger recomputes the digest from the actual
        # OLD/NEW row (P1 #3), so it can no longer be spoofed via a session var.
        cursor.execute("SELECT set_config('pulse.substrate_cap_mode', %s, true)", [mode])
        cursor.execute("SELECT set_config('pulse.substrate_write_receipt', %s, true)",
                       [str(rid) if rid else ''])
        cursor.execute("SELECT set_config('pulse.substrate_write_generation', %s, true)",
                       [str(generation)])
        cursor.execute(
            "SELECT set_config('pulse.substrate_write_requirement_key', %s, true)",
            [requirement_key])

    def _touch(self, cursor, title='t2'):
        # Change a guard column (title) with the concrete owner unchanged: the
        # org-isolation trigger early-returns, the substrate guard fires.
        cursor.execute("UPDATE tasks_task SET title=%s WHERE id=%s",
                       [title, self.item.pk])

    # --- UPDATE guard ----------------------------------------------------------
    def test_update_with_matching_receipt_passes_and_consumes(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt()
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor)
        r.refresh_from_db()
        self.assertIsNotNone(r.consumed_at)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, 't2')

    def test_update_without_receipt_fails_closed(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=None)
            self._touch(cursor)
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, 'sw')

    def test_update_replay_of_consumed_receipt_fails(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(new_title='a')
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor, title='a')
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor, title='b')

    def test_update_cross_item_receipt_fails(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        other = Item.objects.create(
            mode='task', title='other', project=self.project, workflow_name='')
        r = self._arm_receipt(item=other)  # bound to a different item_id
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor)

    def test_update_generation_mismatch_fails(self):
        """0114 verifies assignment_generation (0113 stored but did not check it)."""
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(generation=5)
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id, generation=6)  # GUC != receipt
            self._touch(cursor)

    def test_update_requirement_key_mismatch_fails(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(requirement_key='rk-a')
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id, requirement_key='rk-other')  # GUC != receipt
            self._touch(cursor)

    def test_update_wrong_digest_receipt_fails(self):
        """A receipt whose write_input_digest does not equal the digest the trigger
        computes from the ACTUAL OLD/NEW row cannot be consumed (P1 #3)."""
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(digest='e' * 64)  # not the real OLD->NEW digest
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor)

    def test_guarded_input_mutated_after_arming_is_rejected(self):
        """P1 #3: a receipt armed for one requirement-input state cannot be consumed
        by a write that mutates a guarded input (e.g. a post-arm pre_save receiver)
        — the trigger recomputes the digest from the ACTUAL NEW row."""
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(new_title='t2')  # digest over metadata={} -> title=t2
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            # the actual write also changes metadata -> digest no longer matches
            cursor.execute(
                "UPDATE tasks_task SET title='t2', metadata='{\"needs\":\"kubeconfig\"}'::jsonb "
                "WHERE id=%s", [self.item.pk])

    def test_update_expired_receipt_fails(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(expired=True)
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._touch(cursor)

    def test_update_noop_same_value_is_exempt(self):
        """SET x=x fires UPDATE OF but changes nothing -> no receipt required."""
        if not _pg():
            self.skipTest('postgres-only trigger')
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=None)
            self._touch(cursor, title='sw')  # current value: no actual change
        self.item.refresh_from_db()  # survives — no exception

    def test_update_owner_clear_is_exempt(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=None)
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=NULL, assignee_email='' WHERE id=%s",
                [self.item.pk])
        self.item.refresh_from_db()
        self.assertFalse(self.item.assignee_email)

    def test_offmode_write_is_noop(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        for i, mode in enumerate(('off', 'shadow', '')):
            with self.subTest(mode=mode or 'unset'):
                with connection.cursor() as cursor:
                    self._gucs(cursor, mode=mode, rid=None)
                    self._touch(cursor, title=f'off{i}')
                self.item.refresh_from_db()
                self.assertEqual(self.item.title, f'off{i}')  # passed, no receipt

    def test_non_guarded_column_update_does_not_fire(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        # priority is not in the substrate UPDATE OF list -> the guard never fires.
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=None)
            cursor.execute("UPDATE tasks_task SET priority=%s WHERE id=%s",
                           ['high', self.item.pk])
        self.item.refresh_from_db()
        self.assertEqual(self.item.priority, 'high')

    # --- INSERT guard (AFTER INSERT claim/backfill) ----------------------------
    def _insert_owned_copy(self, cursor, *, item_key, email='ins@shizuha.com',
                           subject_id=88):
        """Copy the template into a fresh owned row (AFTER INSERT substrate fires)."""
        _set_org_proof(cursor, project_id=self.project.id, subject_id=subject_id,
                       email=email)
        desc = connection.introspection.get_table_description(cursor, 'tasks_task')
        cols = [d.name for d in desc if d.name != 'id']
        overrides = {
            'assignee_id': str(int(subject_id)),
            'assignee_email': "'%s'" % email,
            'item_key': "'%s'" % item_key,
        }
        quoted = ['"%s"' % c for c in cols]
        selected = [overrides.get(c, '"%s"' % c) for c in cols]
        cursor.execute(
            'INSERT INTO tasks_task (%s) SELECT %s FROM tasks_task WHERE id=%%s '
            'RETURNING id' % (','.join(quoted), ','.join(selected)),
            [self.item.pk])
        return cursor.fetchone()[0]

    def test_insert_with_prearmed_receipt_backfills_and_consumes(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(item_id_null=True, assignee_email='ins@shizuha.com')
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            new_id = self._insert_owned_copy(cursor, item_key='SWP-INS-1')
        r.refresh_from_db()
        self.assertEqual(r.item_id, new_id)
        self.assertIsNotNone(r.consumed_at)

    def test_insert_without_receipt_fails_closed(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=None)
            self._insert_owned_copy(cursor, item_key='SWP-INS-2')

    def test_insert_second_same_receipt_fails(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        r = self._arm_receipt(item_id_null=True, assignee_email='ins@shizuha.com')
        with connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._insert_owned_copy(cursor, item_key='SWP-INS-3')
        with self.assertRaises(DatabaseError), transaction.atomic(), \
                connection.cursor() as cursor:
            self._gucs(cursor, rid=r.id)
            self._insert_owned_copy(cursor, item_key='SWP-INS-4')

    def test_insert_ownerless_is_exempt(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','enforce',true)")
            Item.objects.create(mode='task', title='ol', project=self.project,
                                workflow_name='')  # owner-less INSERT: no receipt needed


class SubstrateWriteArmingHelperTests(TestCase):
    """The ORM arming helper feeds the trigger: it arms + publishes matching GUCs."""

    def setUp(self):
        self.project = Project.objects.create(
            name='AHP', key='AHP', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='ah', project=self.project, workflow_name='')
        self.item.refresh_from_db()

    def test_offmode_arms_nothing(self):
        with mock.patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'off'}):
            self.item.assignee_email = OWNER
            self.item.assignee_id = 88
            out = WorkflowService._arm_substrate_write_receipt_at_commit(
                self.item, is_insert=False, guarded_write=True)
        self.assertIsNone(out)
        self.assertEqual(SubstrateWriteReceipt.objects.count(), 0)

    def test_owner_clear_arms_nothing(self):
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            self.item.assignee_email = ''
            self.item.assignee_id = None
            out = WorkflowService._arm_substrate_write_receipt_at_commit(
                self.item, is_insert=False, guarded_write=True)
        self.assertIsNone(out)
        self.assertEqual(SubstrateWriteReceipt.objects.count(), 0)

    def test_unguarded_update_arms_nothing(self):
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            self.item.assignee_email = OWNER
            self.item.assignee_id = 88
            out = WorkflowService._arm_substrate_write_receipt_at_commit(
                self.item, is_insert=False, guarded_write=False)
        self.assertIsNone(out)
        self.assertEqual(SubstrateWriteReceipt.objects.count(), 0)

    def test_enforce_arms_receipt_and_gucs_that_the_update_guard_accepts(self):
        if not _pg():
            self.skipTest('postgres-only trigger')
        # Install the concrete owner first (mode off + org proof), so the guarded write
        # below is a title change the org fence ignores.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','off',true)")
            _set_org_proof(cursor, project_id=self.project.id, subject_id=88, email=OWNER)
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=88, assignee_email=%s WHERE id=%s",
                [OWNER, self.item.pk])
        self.item.refresh_from_db()
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            self.item.title = 'changed'
            receipt = WorkflowService._arm_substrate_write_receipt_at_commit(
                self.item, is_insert=False, guarded_write=True)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.item_id, self.item.pk)
        self.assertEqual(receipt.mode, 'enforce')
        # The arming set the tx-local GUCs; a matching guarded raw write in the SAME
        # transaction is accepted + consumes the just-armed receipt.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','enforce',true)")
            cursor.execute("UPDATE tasks_task SET title=%s WHERE id=%s",
                           ['changed', self.item.pk])
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.consumed_at)

    def test_exact_golden_rk2_arms_and_is_consumed_by_postgres_guard(self):
        """Security P1: drive the real 182-character golden rk2 through the ORM
        armer and PostgreSQL trigger, not a short placeholder that misses the
        receipt-column width boundary."""
        if not _pg():
            self.skipTest('postgres-only trigger')
        self.assertEqual(len(GOLDEN_RK2), 182)
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','off',true)")
            _set_org_proof(cursor, project_id=self.project.id, subject_id=88, email=OWNER)
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=88, assignee_email=%s WHERE id=%s",
                [OWNER, self.item.pk])
        self.item.refresh_from_db()
        self.item.title = 'golden-rk2-consumed'
        self.item._substrate_commit_binding = {
            'requirement_key': GOLDEN_RK2,
            'assignment_generation': 7,
        }
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            receipt = WorkflowService._arm_substrate_write_receipt_at_commit(
                self.item, is_insert=False, guarded_write=True)
        self.assertEqual(receipt.requirement_key, GOLDEN_RK2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','enforce',true)")
            cursor.execute(
                "UPDATE tasks_task SET title=%s WHERE id=%s",
                ['golden-rk2-consumed', self.item.pk])
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.consumed_at)


class SubstrateWriteAuthorizingWriteTests(TestCase):
    """Review P1 #1 + P1 #2: a requirement-input-only change to a retained owner is
    an authorizing write that fails closed without a signed receipt, and the sole
    env switch (PULSE_SUBSTRATE_CAP_MODE) actually activates the DB floor for raw
    writers via the connection GUC."""

    def setUp(self):
        self.project = Project.objects.create(
            name='AWP', key='AWP', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='aw', project=self.project, workflow_name='')
        self.item.refresh_from_db()
        # Install a concrete owner under mode=off + the org-isolation proof.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pulse.substrate_cap_mode','off',true)")
            _set_org_proof(cursor, project_id=self.project.id, subject_id=88, email=OWNER)
            cursor.execute(
                "UPDATE tasks_task SET assignee_id=88, assignee_email=%s WHERE id=%s",
                [OWNER, self.item.pk])
        self.item.refresh_from_db()

    def _reset_session_guc(self):
        with connection.cursor() as c:
            c.execute("SELECT set_config('pulse.substrate_cap_mode','off',false)")

    def test_metadata_change_introducing_substrate_requirement_rolls_back(self):
        # An ordinary save(update_fields=['metadata']) that introduces needs:kubeconfig
        # on a retained owner under enforce, with NO carried receipt, must roll back.
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            self.item.metadata = {'needs': 'kubeconfig'}
            with self.assertRaisesMessage(ValidationError, 'missing_substrate_receipt'):
                self.item.save(update_fields=['metadata'])
        self.item.refresh_from_db()
        self.assertNotIn('needs', (self.item.metadata or {}))  # rolled back

    def test_title_change_on_substrate_seat_requires_receipt(self):
        # title/description are requirement inputs too: a title-only change on a seat
        # whose labels already require substrate is an authorizing write.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks_task SET labels='{\"needs:kubeconfig\": true}'::jsonb WHERE id=%s",
                [self.item.pk])
        self.item.refresh_from_db()
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            self.item.title = 'retitled'
            with self.assertRaisesMessage(ValidationError, 'missing_substrate_receipt'):
                self.item.save(update_fields=['title'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, 'aw')  # rolled back

    def test_env_switch_activates_db_floor_for_raw_writers(self):
        # Review P1 #2: setting ONLY PULSE_SUBSTRATE_CAP_MODE=enforce must activate
        # the DB guards on a real connection (via the connection_created handler
        # registered in apps.ready), so raw QuerySet.update fails without a receipt.
        if not _pg():
            self.skipTest('postgres-only trigger')
        self.addCleanup(self._reset_session_guc)
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            # Fire the PRODUCTION handler (not a hardcoded set_config) — it derives
            # the session GUC from the env switch alone.
            connection_created.send(sender=connection.__class__, connection=connection)
            with connection.cursor() as c:
                c.execute("SHOW pulse.substrate_cap_mode")
                self.assertEqual(c.fetchone()[0], 'enforce')
            with self.assertRaises(DatabaseError), transaction.atomic():
                Item.objects.filter(pk=self.item.pk).update(status='reopened')
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.status, 'reopened')  # raw write blocked

    def test_enforce_connection_setup_fails_closed_on_guc_error(self):
        # Re-review P1 #1 / P2: under enforce, if the connection_created handler
        # cannot install the pulse.substrate_cap_mode GUC, acquisition must FAIL
        # CLOSED (close + re-raise) — never hand back an unprotected enforce session
        # whose guard triggers no-op on the unset GUC. Directly exercises the
        # production handler's connection lifecycle on receiver failure.
        from tasks.apps import _apply_substrate_cap_mode_guc
        conn = mock.MagicMock()
        conn.vendor = 'postgresql'
        conn.cursor.side_effect = RuntimeError('set_config unavailable')
        with mock.patch.dict(os.environ, ENFORCE_ENV):
            with self.assertRaises(RuntimeError):
                _apply_substrate_cap_mode_guc(sender=None, connection=conn)
        conn.close.assert_called_once()  # failed closed

    def test_offmode_connection_setup_degrades_to_warning(self):
        # off/shadow have no enforcement to lose: a GUC-install failure is a warning,
        # not a fail-closed abort (no raise, no connection close).
        from tasks.apps import _apply_substrate_cap_mode_guc
        conn = mock.MagicMock()
        conn.vendor = 'postgresql'
        conn.cursor.side_effect = RuntimeError('set_config unavailable')
        with mock.patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'off'}):
            _apply_substrate_cap_mode_guc(sender=None, connection=conn)  # no raise
        conn.close.assert_not_called()

    def test_reqinput_digest_is_injective_over_delimiter_bearing_text(self):
        # Re-review P1 #2: the earlier \x01/\x02-delimited concat let control bytes
        # in title/description straddle a field boundary and collide — e.g.
        # (new_title='x', new_description='\x01y') and (new_title='x\x01',
        # new_description='y') both concat to 'x\x01\x01y'. The jsonb_build_array
        # encoding is injective, so these distinct tuples MUST get distinct digests.
        if not _pg():
            self.skipTest('postgres-only function')
        with connection.cursor() as c:
            c.execute(
                "SELECT tasks_substrate_reqinput_digest("
                "NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,%s,%s)", ['x', '\x01y'])
            d1 = c.fetchone()[0]
            c.execute(
                "SELECT tasks_substrate_reqinput_digest("
                "NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,%s,%s)", ['x\x01', 'y'])
            d2 = c.fetchone()[0]
        self.assertNotEqual(d1, d2)
        self.assertEqual((len(d1), len(d2)), (64, 64))  # sha256 hex
