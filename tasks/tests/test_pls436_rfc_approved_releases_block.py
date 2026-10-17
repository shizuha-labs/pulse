"""PLS-436 (operator 2026-07-11): a feature-request-rfc that blocks an
implementation task must RELEASE that block when it reaches Approved-for-
Implementation (rfc_approved) — NOT only at terminal Done. Terminal Done is
downstream of the implementation the RFC gates, so keying auto-resume on Done
alone deadlocks the pair (HIVE-624 <-> HIVE-620 lived case).
"""
from django.test import SimpleTestCase, TestCase

from tasks.models import Comment, Item, ItemLink
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item

_releases = WorkflowService._status_releases_blocks
_is_terminal = WorkflowService._status_is_terminal


class BlockReleasingStatusTests(SimpleTestCase):
    def test_rfc_approved_states_release_but_are_not_terminal(self):
        for s in ('rfc_approved', 'rfc_implementation_linked', 'rfc_validation'):
            self.assertTrue(_releases(s), f"{s} must release blocks")
            self.assertFalse(_is_terminal(s), f"{s} must NOT be terminal")

    def test_terminal_still_releases(self):
        for s in ('done', 'completed', 'cancelled'):
            self.assertTrue(_releases(s))

    def test_active_pre_approval_rfc_states_do_not_release(self):
        for s in ('rfc_triage', 'rfc_design', 'rfc_needs_info', 'intake', 'open',
                  'in_progress', 'blocked'):
            self.assertFalse(_releases(s), f"{s} must still hold the block")


class AutoResumeOnRfcApprovalTests(TestCase):
    def _item(self, title, status, workflow='simple'):
        kwargs = dict(
            title=title, mode='task', workflow_name=workflow, status=status, metadata={},
            assignee_id=436, assignee_email='pls436-blocker@example.com',
        )
        return create_blocked_item(**kwargs) if status == 'blocked' else Item.objects.create(**kwargs)

    def test_rfc_approved_auto_resumes_blocked_implementation(self):
        rfc = self._item('the RFC', status='rfc_triage', workflow='feature-request-rfc')
        impl = create_blocked_item(
            blocker=rfc, title='the implementation', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=436, assignee_email='pls436-blocker@example.com',
        )
        Item.objects.filter(pk=rfc.pk).update(status='rfc_approved')
        rfc.refresh_from_db()
        # No other blockers → the approved RFC no longer counts as active.
        self.assertFalse(WorkflowService._active_blockers_for(impl))
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(rfc)
        self.assertIn(impl.item_key or str(impl.id), resumed)
        impl.refresh_from_db()
        self.assertNotEqual(impl.status, 'blocked')
        audit_text = Comment.objects.filter(
            item=impl,
            content__startswith='Auto-unblocked:',
        ).values_list('content', flat=True).get()
        # PLAT-9444 v2 wording: the trigger is named as a block-releasing
        # STATUS, not blanket-"resolved" — rfc_approved is non-terminal.
        self.assertIn('rfc_approved entered a block-releasing status', audit_text)
        self.assertNotIn('all linked blockers are terminal', audit_text)

    def test_rfc_still_in_triage_keeps_block(self):
        rfc = self._item('unapproved RFC', status='rfc_triage', workflow='feature-request-rfc')
        impl = create_blocked_item(
            blocker=rfc, title='blocked impl', mode='task', workflow_name='simple',
            status='blocked', metadata={}, assignee_id=436,
            assignee_email='pls436-blocker@example.com',
        )
        self.assertTrue(WorkflowService._active_blockers_for(impl))
        self.assertEqual(WorkflowService._auto_unblock_tasks_blocked_by(rfc), [])
        impl.refresh_from_db()
        self.assertEqual(impl.status, 'blocked')

    def test_second_active_blocker_still_holds(self):
        rfc = self._item('approved RFC', status='rfc_triage', workflow='feature-request-rfc')
        other = self._item('other active blocker', status='in_progress')
        impl = create_blocked_item(
            blocker=rfc, title='blocked impl', mode='task', workflow_name='simple',
            status='blocked', metadata={}, assignee_id=436,
            assignee_email='pls436-blocker@example.com',
        )
        ItemLink.objects.create(source_item=impl, target_item=other,
                                link_type='is_blocked_by', created_by_id=1)
        Item.objects.filter(pk=rfc.pk).update(status='rfc_approved')
        rfc.refresh_from_db()
        # RFC releases, but the other in_progress blocker still holds it.
        self.assertTrue(WorkflowService._active_blockers_for(impl))
        self.assertEqual(WorkflowService._auto_unblock_tasks_blocked_by(rfc), [])
        impl.refresh_from_db()
        self.assertEqual(impl.status, 'blocked')
