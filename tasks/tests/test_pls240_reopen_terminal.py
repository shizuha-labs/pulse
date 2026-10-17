"""PLS-240: terminal autonomous tasks can re-enter review with linked PRs."""
import importlib

from django.apps import apps as django_apps
from django.utils import timezone

from tasks.models import (
    Item, Resolution, Status, Workflow, WorkflowStatus, WorkflowTransition,
)
from tasks.tests.base import AuthenticatedAPITestCase


class ReopenTerminalAutonomousTaskTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.open_status = self._status('open', 'Open', 'todo', 1)
        self.in_progress_status = self._status('in_progress', 'In Progress', 'in_progress', 2)
        self.in_review_status = self._status('in_review', 'In Review', 'in_progress', 3)
        self.done_status = self._status('done', 'Done', 'done', 4)
        self.completed_status = self._status('completed', 'Completed', 'done', 5)
        self.closed_status = self._status('closed', 'Closed', 'done', 6)

        for slug, name in (
            ('autonomous-bug', 'Autonomous Bug'),
            ('autonomous-dev', 'Autonomous Dev'),
        ):
            self._workflow(slug, name)

        migration = importlib.import_module(
            'tasks.migrations.0083_pls240_reopen_terminal_review'
        )
        migration.add_reopen_for_review(django_apps, None)

    def _status(self, slug, name, category, position):
        return Status.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'category': category,
                'position': position,
                'is_system': True,
            },
        )[0]

    def _workflow(self, slug, name):
        workflow, _ = Workflow.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'initial_status': self.open_status,
                'is_system': True,
            },
        )
        if workflow.initial_status_id is None:
            workflow.initial_status = self.open_status
            workflow.save(update_fields=['initial_status', 'updated_at'])
        for position, status in enumerate((
            self.open_status,
            self.in_progress_status,
            self.in_review_status,
            self.done_status,
            self.completed_status,
            self.closed_status,
        ), start=1):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=status,
                defaults={'position': position},
            )
        return workflow

    def test_reopen_done_autonomous_bug_routes_to_review_and_preserves_prs(self):
        resolution, _ = Resolution.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'position': 1, 'is_system': True},
        )
        item = Item.objects.create(
            mode='task',
            item_key='PLS-240-REPRO',
            title='Prematurely closed bug with real PR',
            status='done',
            workflow_status='done',
            workflow_name='autonomous-bug',
            assignee_email='kai@shizuha.com',
            assignment_group='engineering',
            completed_at=timezone.now(),
            resolution=resolution,
            resolution_date=timezone.now(),
            metadata={
                'pull_requests': [{
                    'url': 'https://github.com/shizuha-labs/shizuha-tasks/pull/300',
                    'number': 300,
                    'status': 'open',
                }],
            },
        )

        self.assertTrue(WorkflowTransition.objects.filter(
            workflow__slug='autonomous-bug',
            name='Reopen for Review',
            from_status='done',
            to_status='in_review',
        ).exists())

        response = self.client.post(f'/api/items/{item.id}/reopen/')

        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.workflow_status.replace(' ', '_').lower(), 'in_review')
        self.assertIsNone(item.completed_at)
        self.assertIsNone(item.resolution_id)
        self.assertEqual(item.metadata['pull_requests'][0]['number'], 300)
        self.assertEqual(response.data['item_key'], 'PLS-240-REPRO')
        self.assertEqual(response.data['title'], 'Prematurely closed bug with real PR')

    def test_migration_seeds_terminal_reopen_transitions_for_autonomous_workflows(self):
        for workflow_slug in ('autonomous-bug', 'autonomous-dev'):
            for terminal_slug in ('done', 'completed', 'closed'):
                self.assertTrue(WorkflowTransition.objects.filter(
                    workflow__slug=workflow_slug,
                    name='Reopen for Review',
                    from_status=terminal_slug,
                    to_status='in_review',
                ).exists(), f'{workflow_slug} {terminal_slug} -> in_review missing')

    def test_reopen_done_autonomous_bug_without_pr_is_rejected_not_noop(self):
        item = Item.objects.create(
            mode='task',
            item_key='PLS-240-NOPR',
            title='Prematurely closed bug with no PR',
            status='done',
            workflow_status='done',
            workflow_name='autonomous-bug',
        )

        response = self.client.post(f'/api/items/{item.id}/reopen/')

        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')
        self.assertIn('linked PR', response.data['error'])
