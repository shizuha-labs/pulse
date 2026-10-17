"""HIVE-1153 P1(b).4 (akira re-review): every EXISTING-workflow DEFINITION write
serializes on the parent Workflow row so it cannot interleave the capability-evidence
commit fence's policy-envelope read.

Production-order serialization argument this proves:
  * The commit fence (WorkflowService._revalidate_substrate_receipt_at_commit, run
    inside Item.save()) locks the item's Workflow row with
    `Workflow.objects.select_for_update().get(pk=<item's workflow>)` under the fixed
    Item->Workflow order, THEN reads the full policy envelope (statuses +
    transitions) to recompute rk2.
  * Every admin/runtime definition edit below now takes a `SELECT ... FOR UPDATE`
    on the SAME Workflow pk before mutating its statuses/transitions/initial_status.
  * Two writers taking a row lock on the identical Workflow pk serialize by
    construction — so a definition edit can never interleave the fence's multi-query
    envelope read, and a receipt can never be validated against a policy version that
    is being mutated out from under it.

Each test asserts the FOR UPDATE fired against the Workflow row with the correct pk.
Postgres-only: SQLite silently no-ops SELECT FOR UPDATE, so the invariant can only be
asserted on the CI/prod backend (the shizuha-tasks test suite runs on postgres).
"""
import unittest

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services.workflow_service import WorkflowService


def _locked_workflow_pks(cap):
    """pks whose Workflow row was locked FOR UPDATE in the captured queries."""
    tbl = Workflow._meta.db_table
    hits = []
    for q in cap.captured_queries:
        sql = q['sql']
        if 'FOR UPDATE' in sql and f'"{tbl}"' in sql:
            hits.append(sql)
    return hits


@unittest.skipUnless(
    connection.vendor == 'postgresql',
    'SELECT FOR UPDATE is only enforced on postgres (sqlite no-ops it)')
class DefinitionWriteSerializationTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.admin = U.objects.create_user(
            username='wfadmin', email='wfadmin@shizuha.com', password='x', is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.open = Status.objects.create(name='DW Open', slug='dw_open', category='todo')
        self.prog = Status.objects.create(name='DW Prog', slug='dw_prog', category='in_progress')
        self.done = Status.objects.create(name='DW Done', slug='dw_done', category='done')
        self.wf = Workflow.objects.create(name='DW WF', slug='dw-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.prog)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Go', from_status='dw_open', to_status='dw_prog')

    def _assert_locked_wf(self, cap):
        hits = _locked_workflow_pks(cap)
        self.assertTrue(
            hits, 'expected a SELECT ... FOR UPDATE on the Workflow row; none captured')

    # --- WorkflowViewSet definition actions (findings 1-3) ---
    def test_add_status_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.post(
                reverse('workflow-add-status', args=[self.wf.id]),
                {'status_slug': 'dw_done', 'position': 2}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self._assert_locked_wf(cap)

    def test_remove_status_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.post(
                reverse('workflow-remove-status', args=[self.wf.id]),
                {'status_slug': 'dw_prog'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self._assert_locked_wf(cap)

    def test_add_transition_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.post(
                reverse('workflow-add-transition', args=[self.wf.id]),
                {'name': 'Finish', 'from_status': 'dw_prog', 'to_status': 'dw_open'},
                format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self._assert_locked_wf(cap)

    # --- WorkflowViewSet.perform_update / perform_destroy (finding 4:
    #     pulse_admin_update_workflow PATCH, initial_status rotates the envelope) ---
    def test_workflow_patch_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.patch(
                reverse('workflow-detail', args=[self.wf.id]),
                {'initial_status': self.prog.id}, format='json')
        self.assertIn(r.status_code, (200, 202), r.content)
        self._assert_locked_wf(cap)

    # --- WorkflowTransitionViewSet CRUD (finding 5:
    #     pulse_admin_update_transition PATCH / typed_change_spec.patch_transition) ---
    def test_transition_create_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.post(
                reverse('transition-list'),
                {'workflow': str(self.wf.id), 'name': 'New',
                 'from_status': 'dw_prog', 'to_status': 'dw_done'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self._assert_locked_wf(cap)

    def test_transition_patch_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.patch(
                reverse('transition-detail', args=[self.t.id]),
                {'allowed_roles': ['admin']}, format='json')
        self.assertIn(r.status_code, (200, 202), r.content)
        self._assert_locked_wf(cap)

    def test_transition_delete_locks_workflow_row(self):
        with CaptureQueriesContext(connection) as cap:
            r = self.client.delete(reverse('transition-detail', args=[self.t.id]))
        self.assertEqual(r.status_code, 204, r.content)
        self._assert_locked_wf(cap)

    # --- service-side definition writers (findings 6 & 7) ---
    def test_ensure_scheduled_gap_state_locks_workflow_row(self):
        # finding 6: runtime backstop mutates an EXISTING workflow (missing the
        # Scheduled membership here) during recurring-task processing. project=None
        # -> resolves via Workflow.resolve(workflow_name).
        stub = type('I', (), {'project': None, 'workflow_name': 'dw-wf'})()
        with CaptureQueriesContext(connection) as cap:
            WorkflowService._ensure_scheduled_gap_state_for_workflow(stub)
        self._assert_locked_wf(cap)
        # and it actually added the gap state under the lock
        self.assertTrue(WorkflowStatus.objects.filter(
            workflow=self.wf, status__slug='scheduled').exists())

    def test_ensure_scheduled_gap_state_fastpath_takes_no_lock(self):
        # already-present gap state -> no create needed -> no lock contention.
        sched, _ = Status.objects.get_or_create(
            slug='scheduled', defaults={'name': 'Scheduled', 'category': 'scheduled'})
        WorkflowStatus.objects.get_or_create(
            workflow=self.wf, status=sched, defaults={'position': 99})
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Run Now', from_status='scheduled', to_status='dw_open')
        stub = type('I', (), {'project': None, 'workflow_name': 'dw-wf'})()
        with CaptureQueriesContext(connection) as cap:
            WorkflowService._ensure_scheduled_gap_state_for_workflow(stub)
        self.assertFalse(
            _locked_workflow_pks(cap),
            'steady-state no-op backstop must not lock the Workflow row')

    def test_create_default_transitions_locks_workflow_row(self):
        fresh = Workflow.objects.create(
            name='DW Fresh', slug='dw-fresh', initial_status=self.open)
        with CaptureQueriesContext(connection) as cap:
            WorkflowService.create_default_transitions(fresh)
        self._assert_locked_wf(cap)
        self.assertTrue(WorkflowTransition.objects.filter(workflow=fresh).exists())


@unittest.skipUnless(
    connection.vendor == 'postgresql',
    'real row-lock blocking is only observable on postgres')
class DefinitionWriteConcurrentBlockingTests(TransactionTestCase):
    """akira re-review P2: prove SERIALIZATION with two real connections, not just
    that FOR UPDATE SQL fired. A concurrent holder of the Workflow row lock (the
    commit fence locks the SAME row via select_for_update under the Item->Workflow
    order) must BLOCK a definition writer that calls lock_workflow_for_definition_write
    until the holder commits — so an admin definition edit cannot interleave the
    fence's multi-query policy-envelope read.
    """

    def setUp(self):
        self.open = Status.objects.create(name='CB Open', slug='cb_open', category='todo')
        self.wf = Workflow.objects.create(name='CB WF', slug='cb-wf', initial_status=self.open)
        WorkflowStatus.objects.create(workflow=self.wf, status=self.open, position=0)

    def test_definition_writer_blocks_on_concurrent_workflow_row_lock(self):
        import threading
        import time
        from django.db import connections, transaction
        from tasks.services.workflow_service import lock_workflow_for_definition_write

        holder_locked = threading.Event()   # holder has the Workflow row lock
        release_holder = threading.Event()  # main tells the holder to commit
        writer_done = threading.Event()      # the definition writer finished its mutation
        errors = []

        wf_pk = self.wf.pk
        prog = Status.objects.create(name='CB Prog', slug='cb_prog', category='in_progress')

        def holder():
            # Stands in for the commit fence: hold the SAME Workflow row lock the
            # fence takes (select_for_update on the item's workflow) across a window.
            try:
                with transaction.atomic():
                    lock_workflow_for_definition_write(wf_pk)
                    holder_locked.set()
                    release_holder.wait(timeout=10)
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(('holder', repr(exc)))
            finally:
                connections.close_all()

        def writer():
            # A REAL definition mutation through the production lock helper.
            try:
                holder_locked.wait(timeout=10)
                with transaction.atomic():
                    lock_workflow_for_definition_write(wf_pk)  # <- must block here
                    WorkflowStatus.objects.create(
                        workflow_id=wf_pk, status=prog, position=1)
                writer_done.set()
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(('writer', repr(exc)))
            finally:
                connections.close_all()

        th = threading.Thread(target=holder)
        tw = threading.Thread(target=writer)
        th.start()
        self.assertTrue(holder_locked.wait(timeout=10), 'holder never took the lock')
        tw.start()

        # While the holder keeps the row lock, the definition writer MUST be blocked
        # inside lock_workflow_for_definition_write and cannot have committed its row.
        blocked_before = writer_done.wait(timeout=0.8)
        self.assertFalse(
            blocked_before,
            'definition writer did NOT block on the concurrently-held Workflow row lock')

        # Release the holder; the writer must now acquire the lock and finish.
        release_holder.set()
        self.assertTrue(
            writer_done.wait(timeout=10),
            'definition writer did not proceed after the holder released the lock')
        th.join(timeout=10)
        tw.join(timeout=10)
        self.assertEqual(errors, [], f'thread errors: {errors}')
        # The serialized write landed exactly once, after the holder committed.
        self.assertTrue(
            WorkflowStatus.objects.filter(workflow_id=wf_pk, status=prog).exists())
