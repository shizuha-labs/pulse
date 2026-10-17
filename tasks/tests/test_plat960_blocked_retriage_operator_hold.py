"""PLAT-960: regression tests for the PLS-148 operator-hold guard in
`sweep_blocked_retriage` / `_real_blocker` (tasks/tasks.py).

PLS-148 shipped without a regression test (to land the BKSG-7/8/9 re-triage loop fix
fast). These lock the guard:
  1. A `blocked` task whose blocker is itself `blocked` AND assigned to the operator is a
     legitimate human-hold → `_real_blocker` True → the sweep must NOT auto-unblock it.
  2. A `blocked` task with NO active linked blocker and not on the operator is hoarded →
     the guard must NOT over-broaden → the sweep returns it to a ready state.
"""
import os
from unittest import mock

from django.test import TestCase

from tasks.models import Item, ItemLink, Project
from tasks.tasks import sweep_blocked_retriage
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item, satisfy_blocked_invariant

OPERATOR_EMAIL = 'operator@example.test'


class BlockedRetriageOperatorHoldTests(TestCase):
    def setUp(self):
        assignment_authorization = mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'fixture-org', 'fixture-org'),
        )
        assignment_authorization.start()
        self.addCleanup(assignment_authorization.stop)
        self.project = Project.objects.create(name='Books', key='BKSG', org_id=1)

    def _task(self, key, status, assignee=''):
        factory = create_blocked_item if status == 'blocked' else Item.objects.create
        return factory(
            project=self.project, item_key=key, title=key, mode='task',
            status=status, assignee_email=assignee,
            assignee_id=(99 if assignee else None),
        )

    @mock.patch.dict(os.environ, {'PULSE_BLOCKED_RETRIAGE_APPLY': 'true'})
    def test_operator_assigned_blocked_blocker_is_not_unblocked(self):
        """PLS-148: a `blocked` blocker on the operator's queue is a legitimate
        human-hold — _real_blocker returns True, the sweep leaves the upstream blocked."""
        op_blocker = self._task('BKSG-8', 'blocked', assignee=OPERATOR_EMAIL)
        satisfy_blocked_invariant(op_blocker)
        held = create_blocked_item(
            blocker=op_blocker,
            project=self.project, item_key='BKSG-7', title='BKSG-7', mode='task',
            status='blocked', assignee_email='', assignee_id=None,
        )
        # held is_blocked_by op_blocker

        result = sweep_blocked_retriage.apply(args=[]).result

        held.refresh_from_db()
        op_blocker.refresh_from_db()
        self.assertEqual(held.status, 'blocked', 'operator-held task must stay blocked')
        self.assertNotIn('BKSG-7', result['retriaged'])
        # the operator-parked blocker itself is also legit (on_operator) and stays blocked
        self.assertEqual(op_blocker.status, 'blocked')
        self.assertNotIn('BKSG-8', result['retriaged'])
        self.assertGreaterEqual(result['legit_blocked'], 2)

    @mock.patch.dict(os.environ, {'PULSE_BLOCKED_RETRIAGE_APPLY': 'true'})
    @mock.patch.object(WorkflowService, '_resolve_unblock_assignee', return_value=(None, None))
    @mock.patch.object(WorkflowService, '_choose_resume_status', return_value='todo')
    @mock.patch.object(WorkflowService, '_pre_block_state_from_activity', return_value=None)
    def test_blocked_task_with_no_linked_blocker_is_unblocked(self, *_mocks):
        """The guard must not over-broaden: a `blocked` task with NO active linked
        blocker and not on the operator is hoarded → returned to a ready state."""
        # Seed the legacy violation below the ORM so this regression can prove
        # the reconciliation sweep repairs it. The deferred DB fence validates
        # after the sweep has restored a ready status.
        hoarded = self._task('BKSG-9', 'open', assignee='ryo@shizuha.com')
        Item.objects.filter(pk=hoarded.pk).update(status='blocked')
        hoarded.refresh_from_db()

        result = sweep_blocked_retriage.apply(args=[]).result

        hoarded.refresh_from_db()
        self.assertNotEqual(hoarded.status, 'blocked', 'hoarded block must be auto-unblocked')
        self.assertIn('BKSG-9', result['retriaged'])
