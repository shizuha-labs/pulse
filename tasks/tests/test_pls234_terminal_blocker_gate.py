"""PLS-234 (regression of PLS-164): auto-resume must treat a NON-terminal
linked blocker (awaiting_merge / in_review / awaiting_*) as ACTIVE — it must not
drop the block. Root cause was `_status_is_terminal`'s `category == 'done'`
fallback classifying `awaiting_merge` (categorized 'done') as terminal.
"""
from django.test import SimpleTestCase, TestCase

from tasks.models import Item, ItemLink
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item

_is_terminal = WorkflowService._status_is_terminal


class StatusTerminalityTests(SimpleTestCase):
    """Pure (no-DB) — the single source of truth for blocker terminality."""

    def test_active_late_stage_statuses_are_not_terminal(self):
        # The load-bearing regression: these are ACTIVE, must NOT count terminal.
        for s in ('awaiting_merge', 'awaiting-merge', 'in_review', 'awaiting_qa',
                  'open', 'in_progress', 'blocked', 'needs_triage', 'rfc_validation'):
            self.assertFalse(_is_terminal(s), f"{s} must be non-terminal (active)")

    def test_canonical_terminal_statuses_are_terminal(self):
        for s in ('done', 'completed', 'closed', 'cancelled', 'canceled', 'resolved',
                  'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred'):
            self.assertTrue(_is_terminal(s), f"{s} must be terminal")

    def test_empty_and_none_are_not_terminal(self):
        self.assertFalse(_is_terminal(None))
        self.assertFalse(_is_terminal(''))

    def test_matches_pulse_list_tasks_terminal_set(self):
        from tasks.management.commands.backfill_assignee_ids import TERMINAL_STATUSES
        # Every canonical terminal status the tool layer hides must also read as
        # terminal here, so the two can never diverge (the bug this fixes).
        for s in TERMINAL_STATUSES:
            self.assertTrue(_is_terminal(s), f"canonical terminal {s} not recognized")


class AutoResumeBlockerTerminalityTests(TestCase):
    def _item(self, title, status='in_progress', metadata=None):
        kwargs = dict(
            title=title, mode='task', workflow_name='simple',
            status=status, metadata=metadata or {},
            assignee_id=234, assignee_email='pls234-blocker@example.com',
        )
        return create_blocked_item(**kwargs) if status == 'blocked' else Item.objects.create(**kwargs)

    def test_awaiting_merge_blocker_does_not_auto_resume(self):
        """HIVE-358/HIVE-365 live case: blocker in awaiting_merge (non-terminal)
        must NOT drop the block."""
        blocker = self._item('blocker', status='awaiting_merge')
        victim = self._item('victim', status='blocked')
        ItemLink.objects.create(source_item=victim, target_item=blocker,
                                link_type='is_blocked_by', created_by_id=1)
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(blocker)
        self.assertEqual(resumed, [])
        victim.refresh_from_db()
        self.assertEqual(victim.status, 'blocked')

    def test_active_blockers_for_counts_awaiting_merge_as_active(self):
        blocker = self._item('blocker2', status='awaiting_merge')
        victim = self._item('victim2', status='blocked')
        ItemLink.objects.create(source_item=victim, target_item=blocker,
                                link_type='is_blocked_by', created_by_id=1)
        # awaiting_merge is a real, active blocker → still present.
        self.assertTrue(WorkflowService._active_blockers_for(victim))
