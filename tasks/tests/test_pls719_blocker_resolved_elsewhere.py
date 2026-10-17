"""PLS-719 (2026-08-18): a blocker child whose ask is resolved on the PARENT can
occupy a block-releasing NON-terminal status (`resolved_elsewhere`) so the
parent resumes without the child terminating.

PLAT-4215 and the resume article form an unsatisfiable cycle when a blocker
child's ask is answered on the parent: the child cannot terminate while the
parent is blocked (PLAT-4215 requires an owned active blocker), and the parent
should not resume until the child terminates (the resume article's
precondition). For escalation blocker children (Raise to Admin Ops / Escalate
to Leads) the child is a token for an ask whose answer is recorded on the
PARENT — nothing ever licenses the child to terminate on its own evidence.

The fix introduces `resolved_elsewhere` (non-terminal, block-releasing) and
seeds it into the `simple` workflow (migration 0164). These tests pin:
  - `resolved_elsewhere` releases blocks but is NOT terminal;
  - `waiting_external` is the negative control — it explicitly does NOT release
    (PLS-707 Constraint 4: it means "nobody here can act on this", the opposite
    claim on the same axis);
  - moving the blocker child to `resolved_elsewhere` auto-resumes the blocked
    parent while the child stays non-terminal;
  - a blocker child still in an active non-terminal status keeps the parent
    blocked (paired refuse);
  - the deferred Postgres trigger's NOT IN list agrees with the ORM predicate.
"""
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase

from tasks.models import Item, ItemLink
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item

_releases = WorkflowService._status_releases_blocks
_is_terminal = WorkflowService._status_is_terminal


class BlockReleasingStatusTests(SimpleTestCase):
    def test_resolved_elsewhere_releases_but_is_not_terminal(self):
        self.assertTrue(_releases('resolved_elsewhere'))
        self.assertFalse(_is_terminal('resolved_elsewhere'))

    def test_waiting_external_does_not_release(self):
        """PLS-707 Constraint 4 negative control: waiting_external means "nobody
        here can act on this" — the opposite claim on the same axis. It must NOT
        release the parent."""
        self.assertNotIn('waiting_external', WorkflowService.BLOCK_RELEASING_NONTERMINAL_SLUGS)
        self.assertFalse(_releases('waiting_external'))

    def test_active_working_statuses_still_hold(self):
        for s in ('todo', 'in_progress', 'open', 'blocked'):
            self.assertFalse(_releases(s), f"{s} must still hold the block")

    def test_terminal_still_releases(self):
        for s in ('done', 'completed', 'cancelled'):
            self.assertTrue(_releases(s))


class AutoResumeOnResolvedElsewhereTests(TestCase):
    def _item(self, title, status, workflow='simple'):
        kwargs = dict(
            title=title, mode='task', workflow_name=workflow, status=status, metadata={},
            assignee_id=719, assignee_email='pls719-blocker@example.com',
        )
        return create_blocked_item(**kwargs) if status == 'blocked' else Item.objects.create(**kwargs)

    def test_resolved_elsewhere_auto_resumes_blocked_parent(self):
        """Paired ADMIT: the blocker child's ask was resolved on the parent, so
        the child moves to `resolved_elsewhere` (block-releasing) and the parent
        resumes while the child stays NON-terminal."""
        child = self._item('escalation blocker child', status='todo')
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=719, assignee_email='pls719-blocker@example.com',
        )
        # The child is an active blocker while it sits in todo.
        self.assertTrue(WorkflowService._active_blockers_for(parent))
        # Ask resolved on the parent -> child moves to resolved_elsewhere.
        Item.objects.filter(pk=child.pk).update(status='resolved_elsewhere')
        child.refresh_from_db()
        self.assertFalse(_is_terminal(child.status))
        # No other blockers -> the releasing child no longer counts as active.
        self.assertFalse(WorkflowService._active_blockers_for(parent))
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(child)
        self.assertIn(parent.item_key or str(parent.id), resumed)
        parent.refresh_from_db()
        self.assertNotEqual(parent.status, 'blocked')

    def test_active_child_keeps_parent_blocked(self):
        """Paired REFUSE: the blocker child's ask is NOT resolved, so it stays in
        an active non-terminal status and the parent must remain blocked."""
        child = self._item('unresolved escalation blocker child', status='todo')
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=719, assignee_email='pls719-blocker@example.com',
        )
        self.assertTrue(WorkflowService._active_blockers_for(parent))
        self.assertEqual(WorkflowService._auto_unblock_tasks_blocked_by(child), [])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')

    def test_second_active_blocker_still_holds(self):
        """The child releasing does not release the parent if another active
        blocker still holds it."""
        child = self._item('releasing escalation blocker child', status='todo')
        other = self._item('other active blocker', status='in_progress')
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=719, assignee_email='pls719-blocker@example.com',
        )
        ItemLink.objects.create(source_item=parent, target_item=other,
                                link_type='is_blocked_by', created_by_id=1)
        Item.objects.filter(pk=child.pk).update(status='resolved_elsewhere')
        child.refresh_from_db()
        # Child releases, but the other in_progress blocker still holds it.
        self.assertTrue(WorkflowService._active_blockers_for(parent))
        self.assertEqual(WorkflowService._auto_unblock_tasks_blocked_by(child), [])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')

    def test_waiting_external_child_keeps_parent_blocked(self):
        """A workable root still holds its parent. The retired status cannot
        be entered, and it is still not block-releasing if a legacy row exists."""
        child = self._item('external blocker child', status='todo')
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=719, assignee_email='pls719-blocker@example.com',
        )
        child.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            child.save(update_fields=['status'])
        child.refresh_from_db()
        self.assertEqual(child.status, 'todo')
        self.assertFalse(WorkflowService._status_releases_blocks('waiting_external'))
        self.assertTrue(WorkflowService._active_blockers_for(parent))
        self.assertEqual(WorkflowService._auto_unblock_tasks_blocked_by(child), [])
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')


class TriggerAlignmentTests(TestCase):
    """The deferred Postgres trigger's NOT IN list must include
    `resolved_elsewhere` so it agrees with the ORM predicate (migration 0164)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pg = connection.vendor == 'postgresql'

    def test_trigger_not_in_list_includes_resolved_elsewhere(self):
        if not self._pg:
            self.skipTest('postgres-only trigger')
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_functiondef('tasks_assert_blocked_has_owned_blocker'::regproc)"
            )
            definition = cursor.fetchone()[0]
        self.assertIn('resolved_elsewhere', definition)
        # And the negative control: waiting_external is NOT block-releasing, so
        # it must remain OUTSIDE the NOT IN list (it is a real active blocker).
        self.assertNotIn('waiting_external', definition)
