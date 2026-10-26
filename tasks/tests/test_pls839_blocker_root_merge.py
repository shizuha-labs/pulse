"""PLS-839: unblocked blocker root with owned outgoing dependents can terminalize.

Production-order: root R at awaiting_merge, R blocks A and B (both blocked+owned),
no incoming blocker on R. Merge/terminal transition must succeed and resume A/B
without inventing links or unlinking dependents. Negative: naked blocked entry
still fails closed.
"""

from __future__ import annotations

from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from tasks.models import (
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

User = get_user_model()


def _status(slug, name, category):
    obj, _ = Status.objects.get_or_create(
        slug=slug, defaults={'name': name, 'category': category},
    )
    return obj


@skipUnless(connection.vendor == 'postgresql', 'PLAT-4215 constraint is PostgreSQL-only')
class Pls839BlockerRootMergeTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        # PLS-1164 hermetic containment: classify_roster_email performs a live
        # shizuha-id principal lookup (c47f4fe6). These tests assert roster
        # semantics, not identity resolution — stub the raw lookup so the
        # resolver takes its documented unresolvable/fail-open path without
        # touching the network (the hermetic socket guard redirects to a
        # blackhole port, which surfaced as ConnectionError noise + reds in
        # premerge run 9755).
        patcher = patch('tasks.services.canonical_identity._raw_principal_lookup',
                        return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.actor = User.objects.create_user(
            username='pls839-actor', email='pls839-actor@shizuha.com',
            password='x', is_staff=True,
        )
        self.owner_a = User.objects.create_user(
            username='pls839-a', email='pls839-a@shizuha.com', password='x',
        )
        self.owner_b = User.objects.create_user(
            username='pls839-b', email='pls839-b@shizuha.com', password='x',
        )
        self.project = Project.objects.create(
            name='PLS839', key='P839', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='engineering',
            defaults={
                'organization_slug': 'pls839',
                'tiers': [[self.actor.email, self.owner_a.email, self.owner_b.email]],
                'is_active': True,
            },
        )
        specs = {
            'todo': ('To Do', 'todo'),
            'in_progress': ('In Progress', 'in_progress'),
            'in_review': ('In Review', 'in_progress'),
            'awaiting_merge': ('Awaiting Merge', 'in_progress'),
            'blocked': ('Blocked', 'in_progress'),
            'done': ('Done', 'done'),
        }
        statuses = {s: _status(s, n, c) for s, (n, c) in specs.items()}
        self.wf = Workflow.objects.create(
            name='PLS839 WF', slug='pls839-wf',
            initial_status=statuses['todo'], is_active=True,
        )
        for i, slug in enumerate(specs):
            WorkflowStatus.objects.create(
                workflow=self.wf, status=statuses[slug], position=i,
            )
        self.block_t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Blocked by Linked Issue',
            from_status='in_progress', to_status='blocked', position=0,
        )
        self.merge_t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Merge & Close',
            from_status='awaiting_merge', to_status='done', position=1,
        )
        self.complete_t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Complete',
            from_status='in_progress', to_status='done', position=2,
        )
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Blocker Resolved - Resume In Progress',
            from_status='blocked', to_status='in_progress', position=3,
        )

    def _item(self, key, status, *, email, uid):
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=key,
            mode='task',
            status=status,
            workflow_name=self.wf.slug,
            workflow_status=status,
            assignment_group='engineering',
            assignee_email=email,
            assignee_id=uid,
            created_by_email=self.actor.email,
            created_by_id=self.actor.id,
        )

    def _link_blocks(self, root, dependent):
        ItemLink.objects.create(
            source_item=root, target_item=dependent, link_type='blocks',
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')

    def test_merge_unblocked_root_with_two_owned_outgoing_dependents(self):
        """Acceptance #1: R awaiting_merge, R→A, R→B; merge succeeds; A/B resume."""
        root = self._item(
            'P839-R', 'awaiting_merge',
            email=self.actor.email, uid=self.actor.id,
        )
        a = self._item(
            'P839-A', 'in_progress',
            email=self.owner_a.email, uid=self.owner_a.id,
        )
        b = self._item(
            'P839-B', 'in_progress',
            email=self.owner_b.email, uid=self.owner_b.id,
        )
        self._link_blocks(root, a)
        self._link_blocks(root, b)
        # Root itself is NOT blocked and has no incoming blocker.
        self.assertNotEqual(root.status, 'blocked')
        self.assertFalse(WorkflowService._has_active_assigned_blocker(root))

        with transaction.atomic():
            WorkflowService.execute_transition(
                item=root,
                new_status='done',
                user=self.actor,
                screen_data={},
                transition_id=str(self.merge_t.id),
            )
            with connection.cursor() as cursor:
                cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        root.refresh_from_db()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(root.status, 'done')
        self.assertEqual(a.status, 'in_progress')
        self.assertEqual(b.status, 'in_progress')
        # Links preserved — no unlink required.
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=root, target_item=a, link_type='blocks',
            ).exists()
        )
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=root, target_item=b, link_type='blocks',
            ).exists()
        )

    def test_outgoing_blocks_never_count_as_incoming(self):
        """Acceptance #3 directionality: root with only outgoing edges is unblocked."""
        root = self._item(
            'P839-R2', 'in_progress',
            email=self.actor.email, uid=self.actor.id,
        )
        leaf = self._item(
            'P839-L2', 'in_progress',
            email=self.owner_a.email, uid=self.owner_a.id,
        )
        self._link_blocks(root, leaf)
        self.assertFalse(WorkflowService._has_active_assigned_blocker(root))
        self.assertTrue(WorkflowService._has_active_assigned_blocker(leaf))
        # Active blockers for leaf exclude a releasing root by id.
        self.assertEqual(
            WorkflowService._active_blockers_for(leaf, exclude_blocker_id=root.pk),
            [],
        )

    def test_naked_blocked_entry_still_rejected(self):
        """Acceptance #2: entering blocked without incoming owned blocker fails."""
        lone = self._item(
            'P839-NAKED', 'in_progress',
            email=self.actor.email, uid=self.actor.id,
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item=lone,
                new_status='blocked',
                user=self.actor,
                screen_data={},
                transition_id=str(self.block_t.id),
            )
        lone.refresh_from_db()
        self.assertEqual(lone.status, 'in_progress')


class Pls839UnitDirectionTests(TestCase):
    def test_active_blockers_for_accepts_exclude_kwarg(self):
        import inspect
        sig = inspect.signature(WorkflowService._active_blockers_for)
        self.assertIn('exclude_blocker_id', sig.parameters)
