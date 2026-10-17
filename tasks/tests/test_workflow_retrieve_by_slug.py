"""Task-page workflow diagram must resolve the stored slug, not only page 1 of the list.

Item.workflow_name is the slug (``simple``) or a display name. The map button
used GET /workflows/ (page_size=10) and matched in memory — once the catalogue
grew past one page the match missed and the modal never mounted.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition


class WorkflowRetrieveBySlugTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='wfviewer', email='wfviewer@shizuha.com', password='x',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.todo = Status.objects.create(name='DG To Do', slug='dg_todo', category='todo')
        self.prog = Status.objects.create(
            name='DG In Progress', slug='dg_in_progress', category='in_progress',
        )
        self.done = Status.objects.create(name='DG Done', slug='dg_done', category='done')
        self.wf = Workflow.objects.create(
            name='CTX Diagram WF', slug='ctx938-simple', initial_status=self.todo,
            is_active=True,
        )
        for pos, st in enumerate((self.todo, self.prog, self.done)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        WorkflowTransition.objects.create(
            workflow=self.wf, name='Start Progress',
            from_status='dg_todo', to_status='dg_in_progress',
        )
        # Crowd the default first page (page_size=10) so a list-and-match
        # client would miss this workflow.
        for i in range(12):
            Workflow.objects.create(
                name=f'DG Other {i}', slug=f'dg-other-{i}', initial_status=self.todo,
                is_active=True,
            )

    def test_retrieve_by_slug_returns_statuses_and_transitions(self):
        r = self.client.get(reverse('workflow-detail', args=['ctx938-simple']))
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['slug'], 'ctx938-simple')
        slugs = {row.get('status_slug') or row.get('slug') for row in body['statuses']}
        self.assertTrue({'dg_todo', 'dg_in_progress', 'dg_done'} <= slugs)
        self.assertTrue(any(t.get('name') == 'Start Progress' for t in body['transitions']))

    def test_retrieve_by_uuid_still_works(self):
        r = self.client.get(reverse('workflow-detail', args=[str(self.wf.id)]))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['id'], str(self.wf.id))

    def test_retrieve_by_display_name(self):
        r = self.client.get(reverse('workflow-detail', args=['CTX Diagram WF']))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['slug'], 'ctx938-simple')

    def test_unknown_slug_is_404(self):
        r = self.client.get(reverse('workflow-detail', args=['does-not-exist']))
        self.assertEqual(r.status_code, 404)

    def test_system_simple_slug_if_seeded(self):
        if not Workflow.objects.filter(slug='simple').exists():
            self.skipTest('simple workflow not seeded')
        r = self.client.get(reverse('workflow-detail', args=['simple']))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['slug'], 'simple')
        self.assertGreater(len(r.json().get('statuses') or []), 0)

    def test_default_list_page_does_not_include_every_workflow(self):
        r = self.client.get(reverse('workflow-list'))
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        results = body.get('results') or body
        self.assertLess(len(results), 13)
        self.assertGreaterEqual(body.get('count', 0), 13)
