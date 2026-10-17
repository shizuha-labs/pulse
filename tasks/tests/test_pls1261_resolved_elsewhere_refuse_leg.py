"""PLS-1261: enforce the refuse leg on the `resolved_elsewhere` blocker release.

PLS-719 introduced `resolved_elsewhere` (non-terminal, block-releasing) for
escalation blocker children whose ask was answered on the PARENT. Live QA
(PLS-1191, probe pair B) found the admit leg works but the refuse leg was
missing: the transition's instructions state the precondition ("the blocker
child's ask was answered on the parent") but nothing enforced it — any actor
could release any blocked parent by firing the child's transition regardless
of the ask's state. The guard was artifact-text-only.

These tests pin the enforced contract:
  - REFUSE (AC1): firing `resolved_elsewhere` on a blocker child whose parent
    carries no post-link comment is refused, naming the missing datum; the
    parent stays blocked.
  - ADMIT (paired, unchanged): with a resolution comment on the parent posted
    after the link, the transition succeeds and the parent auto-resumes.
  - SCOPE (AC2): `Resolved Elsewhere` is refused — and hidden from the action
    listing — for tasks that are not blocker children.
  - VOCABULARY (AC3): `resolved_elsewhere` is pinned non-terminal +
    block-releasing (matching the Resume article), with `waiting_external` as
    the negative control (PLS-707 Constraint 4).
  - ALIGNMENT (AC4): the ORM predicate and the deferred Postgres trigger agree
    on the releasing set (the 0172 trigger carries the slug).
"""
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemLink, Status, Workflow, WorkflowTransition
from tasks.services.workflow_service import (
    WorkflowService,
    WorkflowValidationError,
)
from tasks.tests.base import create_blocked_item

_releases = WorkflowService._status_releases_blocks
_is_terminal = WorkflowService._status_is_terminal


class ResolvedElsewhereGuardTests(TestCase):
    def _child_and_parent(self, *, answer_on_parent=False):
        child = Item.objects.create(
            title='escalation blocker child', mode='task',
            workflow_name='simple', status='todo', metadata={},
            assignee_id=1261, assignee_email='pls1261-child@example.com',
        )
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=1261, assignee_email='pls1261-parent@example.com',
        )
        if answer_on_parent:
            # The ask's answer is recorded on the PARENT, after the link.
            Comment.objects.create(
                item=parent, author_id=1261,
                author_email='pls1261-answerer@example.com',
                content='Ask resolved: routed to the platform team (PLS-719 exception).',
            )
        self._seed_transition(child)
        return child, parent

    def _seed_transition(self, child):
        from tasks.models import Status
        # The status row itself (migration 0172 seeds it in production; the
        # local migration-less run must create it explicitly). Non-terminal,
        # matching the article's vocabulary.
        Status.objects.get_or_create(
            slug='resolved_elsewhere',
            defaults={'name': 'Resolved Elsewhere', 'category': 'in_progress'},
        )
        wf = Workflow.objects.filter(name__iexact='simple').first() or \
            Workflow.objects.filter(slug__iexact='simple').first()
        if wf is None:
            todo_status, _ = Status.objects.get_or_create(
                slug='todo', defaults={'name': 'To Do', 'category': 'todo'},
            )
            wf = Workflow.objects.create(
                name='simple', slug='simple', initial_status=todo_status,
            )
        # The production simple workflow already carries seeded 'Resolved
        # Elsewhere' transitions (migration 0172) — possibly more than one.
        # Tolerate any count: only seed when none exists.
        if not WorkflowTransition.objects.filter(
            workflow=wf, name='Resolved Elsewhere',
        ).exists():
            WorkflowTransition.objects.create(
                workflow=wf, name='Resolved Elsewhere',
                from_status='todo', to_status='resolved_elsewhere', position=99,
            )

    def test_refuse_leg_blocks_unanswered_ask(self):
        """AC1: no resolution recorded on the parent -> the child's transition
        is refused, naming the missing datum; the parent stays blocked."""
        child, parent = self._child_and_parent(answer_on_parent=False)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(child, 'resolved_elsewhere')
        self.assertIn('no recorded resolution', str(ctx.exception))
        self.assertIn(str(getattr(parent, 'item_key', None) or parent.pk), str(ctx.exception))

        child.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(child.status, 'todo', 'refused transition must not mutate the child')
        self.assertEqual(parent.status, 'blocked', 'the parent must stay blocked')

    def test_admit_leg_with_recorded_resolution_still_releases(self):
        """Paired ADMIT (unchanged behavior): a parent comment posted after the
        link satisfies the guard; the child moves and the parent auto-resumes.

        Asserts the guard admits (no refusal), then exercises the release via
        the same status-update + auto-unblock path the PLS-719 tests use — the
        full execute_transition path additionally runs a focus query with a
        Postgres-only JSONField lookup that a sqlite local run cannot compile
        (CI's Postgres stack is unaffected)."""
        child, parent = self._child_and_parent(answer_on_parent=True)

        # Guard admits: no refusal raised.
        WorkflowService._guard_resolved_elsewhere(child, 'resolved_elsewhere')

        Item.objects.filter(pk=child.pk).update(status='resolved_elsewhere')
        child.refresh_from_db()
        self.assertFalse(_is_terminal(child.status), 'child stays non-terminal')
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(child)
        self.assertIn(
            str(getattr(parent, 'item_key', None) or parent.pk), resumed,
        )
        parent.refresh_from_db()
        self.assertNotEqual(parent.status, 'blocked', 'parent auto-resumes')

    def test_scope_refuses_non_blocker_task(self):
        """AC2: a task that holds no dependent cannot self-mark
        'resolved elsewhere' — refused with the scope message."""
        plain = Item.objects.create(
            title='plain task, no dependents', mode='task',
            workflow_name='simple', status='todo', metadata={},
            assignee_id=1261, assignee_email='pls1261-plain@example.com',
        )
        self._seed_transition(plain)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(plain, 'resolved_elsewhere')
        self.assertIn('only available to blocker children', str(ctx.exception))

        plain.refresh_from_db()
        self.assertEqual(plain.status, 'todo')

    def test_scope_hides_transition_from_non_blocker_listing(self):
        """AC2: the action listing hides 'Resolved Elsewhere' from tasks that
        are not blocker children, and shows it to actual blocker children."""
        transition = WorkflowTransition.objects.create(
            workflow=Workflow.objects.create(
                name='pls1261-scope-wf', slug='pls1261-scope-wf',
                initial_status=Status.objects.get_or_create(
                    slug='todo', defaults={'name': 'To Do', 'category': 'todo'},
                )[0],
            ),
            name='Resolved Elsewhere', from_status='todo',
            to_status='resolved_elsewhere', position=1,
        )
        plain = Item.objects.create(
            title='plain task', mode='task', workflow_name='pls1261-scope-wf',
            status='todo', metadata={},
            assignee_id=1261, assignee_email='pls1261-plain@example.com',
        )
        self.assertFalse(
            WorkflowService._transition_is_visible_for_item(transition, plain),
        )

        child = Item.objects.create(
            title='blocker child', mode='task', workflow_name='pls1261-scope-wf',
            status='todo', metadata={},
            assignee_id=1261, assignee_email='pls1261-child@example.com',
        )
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='pls1261-scope-wf', status='blocked', metadata={},
            assignee_id=1261, assignee_email='pls1261-parent@example.com',
        )
        self.assertTrue(
            WorkflowService._transition_is_visible_for_item(transition, child),
        )
        # The child is a real blocker of the parent (link exists).
        self.assertTrue(WorkflowService._resolved_elsewhere_blocker_parents(child))

    def test_is_blocked_by_orientation_is_guarded_too(self):
        """The guard covers both link orientations: a child linked via
        `is_blocked_by` (child = target) is also scope-checked and
        ask-resolution-checked."""
        child = Item.objects.create(
            title='blocker child (inverse link)', mode='task',
            workflow_name='simple', status='todo', metadata={},
            assignee_id=1261, assignee_email='pls1261-child@example.com',
        )
        parent = create_blocked_item(
            blocker=child, title='blocked parent', mode='task',
            workflow_name='simple', status='blocked', metadata={},
            assignee_id=1261, assignee_email='pls1261-parent@example.com',
        )
        # Normalize to the inverse orientation: parent --is_blocked_by--> child
        # (parent is the source, child the target — the blocker is the TARGET
        # of an is_blocked_by link).
        link = ItemLink.objects.filter(
            link_type='blocks', source_item=child, target_item=parent,
        ).first()
        self.assertIsNotNone(link)
        ItemLink.objects.filter(pk=link.pk).update(
            link_type='is_blocked_by', source_item=parent, target_item=child,
        )
        self._seed_transition(child)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(child, 'resolved_elsewhere')
        self.assertIn('no recorded resolution', str(ctx.exception))
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')


class ResolvedElsewhereVocabularyPins(SimpleTestCase):
    """AC3: pin the vocabulary one way, matching the Resume article text."""

    def test_resolved_elsewhere_is_block_releasing_nonterminal(self):
        self.assertIn(
            'resolved_elsewhere', WorkflowService.BLOCK_RELEASING_NONTERMINAL_SLUGS,
        )
        self.assertTrue(_releases('resolved_elsewhere'))
        self.assertFalse(_is_terminal('resolved_elsewhere'))

    def test_waiting_external_negative_control(self):
        """PLS-707 Constraint 4: waiting_external must never drift into the
        releasing set — it means "nobody here can act on this"."""
        self.assertNotIn(
            'waiting_external', WorkflowService.BLOCK_RELEASING_NONTERMINAL_SLUGS,
        )
        self.assertFalse(_releases('waiting_external'))

    def test_orm_predicate_and_deferred_trigger_agree(self):
        """AC4: the 0172 deferred trigger's NOT IN list carries the slug, so
        the ORM predicate and the DB trigger stay aligned (PLS-719 AC5)."""
        import importlib

        mig = importlib.import_module(
            'tasks.migrations.0172_pls719_blocker_resolved_elsewhere'
        )
        trigger_sql = (mig.TRIGGER_SQL or '').lower()
        self.assertIn('resolved_elsewhere', trigger_sql)
        # And the ORM side releases it — the two authorities agree.
        self.assertTrue(_releases('resolved_elsewhere'))
