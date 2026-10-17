"""PLS-330: auto-unblock must fire when the blocker is a workflow-change /
change-request that reached a terminal state (`applied` / `change_rejected`).

Root cause: the canonical `TERMINAL_STATUS_SLUGS` (the single source of truth for
the blocker/auto-resume path) omitted the change-request workflow terminals, so a
task blocked solely by a workflow-change request stayed `blocked` forever even
after every blocker was `applied` (PLAT-3189 sat blocked ~7.5h). Both the
event-driven `_auto_unblock_tasks_blocked_by` gate and the
`sweep_resolved_blocked_items` backstop funnel through
`_active_blockers_for` → `_status_is_terminal`, so the single set fixes both.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from tasks.models import Item, ItemLink
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item

_is_terminal = WorkflowService._status_is_terminal


class ChangeRequestTerminalityTests(SimpleTestCase):
    """Pure (no-DB): the change-request terminals must read as terminal, and the
    active late-stage statuses (regression guard for PLS-234) must NOT."""

    def test_workflow_change_terminals_are_terminal(self):
        self.assertTrue(_is_terminal('applied'))
        self.assertTrue(_is_terminal('change_rejected'))
        self.assertTrue(_is_terminal('change-rejected'))  # hyphen normalized

    def test_active_statuses_still_not_terminal(self):
        for s in ('awaiting_merge', 'in_review', 'in_qa', 'open', 'in_progress',
                  'blocked', 'pending_review', 'under_review'):
            self.assertFalse(_is_terminal(s), f"{s} must remain active")


class WorkflowChangeBlockerAutoResumeTests(TestCase):
    def _item(self, title, status='in_progress', workflow_name='simple', metadata=None):
        kwargs = dict(
            title=title, mode='task', workflow_name=workflow_name,
            status=status, metadata=metadata or {},
            assignee_id=330, assignee_email='pls330-blocker@example.com',
        )
        return create_blocked_item(**kwargs) if status == 'blocked' else Item.objects.create(**kwargs)

    def test_applied_workflow_change_blocker_is_not_active(self):
        """A workflow-change request at `applied` must NOT count as an active
        blocker (the direct cause of the stale block)."""
        blocker = self._item('wf-change', status='applied', workflow_name='change-request')
        victim = self._item('victim', status='in_progress')
        ItemLink.objects.create(source_item=victim, target_item=blocker,
                                link_type='is_blocked_by', created_by_id=1)
        self.assertEqual(WorkflowService._active_blockers_for(victim), [])
        self.assertFalse(WorkflowService._has_active_blocker(victim))

    def test_pending_workflow_change_blocker_is_active(self):
        """Guard against over-broadening: a NON-terminal workflow-change request
        (e.g. pending_review) is still a real, active blocker."""
        blocker = self._item('wf-change2', status='pending_review', workflow_name='change-request')
        victim = create_blocked_item(
            blocker=blocker,
            title='victim2', mode='task', workflow_name='simple',
            status='blocked', metadata={},
            assignee_id=330, assignee_email='pls330-blocker@example.com',
        )
        self.assertTrue(WorkflowService._active_blockers_for(victim))
        self.assertTrue(WorkflowService._has_active_blocker(victim))

    @mock.patch.object(WorkflowService, '_resolve_unblock_assignee', return_value=(None, None))
    @mock.patch.object(WorkflowService, '_choose_resume_status', return_value='todo')
    def test_victim_auto_resumes_when_workflow_change_applied(self, *_mocks):
        """End-to-end event path: a task blocked SOLELY by a workflow-change
        request auto-resumes when that request is `applied`."""
        blocker = self._item('wf-change3', status='pending_review', workflow_name='change-request')
        victim = create_blocked_item(
            blocker=blocker,
            title='victim3', mode='task', workflow_name='simple', status='blocked',
            metadata={'pre_block_state': {'had_active_blocker': True, 'status': 'in_progress'}},
            assignee_id=330, assignee_email='pls330-blocker@example.com',
        )
        Item.objects.filter(pk=blocker.pk).update(status='applied')
        blocker.refresh_from_db()
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(blocker)
        victim.refresh_from_db()
        self.assertNotEqual(victim.status, 'blocked', 'victim must be auto-resumed')
        self.assertIn(victim.item_key or str(victim.id), resumed)
