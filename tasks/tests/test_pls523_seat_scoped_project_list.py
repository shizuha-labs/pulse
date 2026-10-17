"""PLS-523 regression: seat-scoped `/api/projects/` 500 + false-negative
transition 500.

Two independent defects reproduced here against production-shaped rows:

1. `ProjectViewSet.get_queryset()` performed a WRITE
   (`_resolve_pending_project_memberships`) before any org filter: it promoted an
   email-only `ProjectMembership` to `user_id=U`, which collided with an existing
   `ProjectMembership(project, user_id=U)` on `unique(project, user_id)` →
   IntegrityError → the PLS-358 unexpected-error 500. Because the write preceded
   org filtering, unscoped and every org-scoped read failed identically (tora /
   TRD orgs 3+186). GET is a safe method (RFC 9110) and must not mutate.

2. A workflow transition that already COMMITTED must never become a
   false-negative 500 because non-critical `ItemSerializer` display enrichment
   failed afterward — it must return a minimal HTTP-200 receipt marked
   `PLS523_TRANSITION_RECEIPT_DEGRADED`.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from tasks.models import (
    Item, Project, ProjectMembership, Status, Workflow, WorkflowStatus,
    WorkflowTransition,
)


class PLS523SeatScopedProjectListTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        # The bug only triggered for NON-staff seats (the resolver was guarded
        # `if not is_staff`). Staff short-circuit PermissionService entirely.
        self.user = user_model.objects.create_user(
            username='pls523', email='tora@example.com',
            password='testpass123', is_staff=False,
        )
        # Give the seat org membership claims (as the JWT layer would) so the
        # org-scoped reads are meaningful, not just empty.
        self.user.organization_memberships = {3: 'member', 186: 'member'}
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Securities Trading', key='TRD', org_id=3,
            access_level='private', is_active=True, created_by_id=self.user.id,
        )
        # Existing membership linked to the real user_id.
        ProjectMembership.objects.create(
            project=self.project, user_id=self.user.id,
            user_email=self.user.email, role='admin', is_active=True,
            can_browse=True,
        )
        # A SECOND active email-only membership for the SAME project/email
        # (user_id NULL). The removed read-time resolver would promote this to
        # user_id=self.user.id and collide on unique(project, user_id).
        self.email_only = ProjectMembership.objects.create(
            project=self.project, user_id=None,
            user_email=self.user.email, role='member', is_active=True,
            can_browse=True,
        )

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_unscoped_list_is_200_and_does_not_mutate_membership(self, get_org):
        get_org.return_value = {'id': 3, 'name': 'Securities Trading', 'slug': 'trd'}
        response = self.client.get('/api/projects/', {'page_size': 200, 'page': 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('TRD', {r['key'] for r in response.data['results']})
        # GET is read-only: the email-only row must be UNCHANGED (no promotion,
        # no unique-constraint collision).
        self.email_only.refresh_from_db()
        self.assertIsNone(
            self.email_only.user_id,
            'GET /api/projects/ must not mutate membership rows (RFC 9110 safe method)')

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_org_scoped_reads_are_200(self, get_org):
        get_org.return_value = {'id': 3, 'name': 'Securities Trading', 'slug': 'trd'}
        # org 3 (the seat's TRD org) and org 186 both returned identical 500s
        # pre-fix because the mutation preceded org filtering.
        resp3 = self.client.get('/api/projects/', {'page_size': 200, 'organization': 3, 'page': 1})
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        self.assertIn('TRD', {r['key'] for r in resp3.data['results']})
        resp186 = self.client.get('/api/projects/', {'page_size': 200, 'organization': 186, 'page': 1})
        self.assertEqual(resp186.status_code, status.HTTP_200_OK, 'org-scoped read must not 500')


class PLS523CommittedTransitionReceiptTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls523tx', email='pls523tx@example.com',
            password='testpass123', is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.awaiting_merge, _ = Status.objects.get_or_create(
            slug='awaiting_merge',
            defaults={'name': 'Awaiting Merge', 'category': 'in_progress', 'position': 1})
        self.done, _ = Status.objects.get_or_create(
            slug='done', defaults={'name': 'Done', 'category': 'done', 'position': 2})
        self.workflow = Workflow.objects.create(
            name='Autonomous Dev PLS523', slug='autonomous-dev-pls523',
            initial_status=self.awaiting_merge)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.awaiting_merge, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.done, position=2)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow, name='Merge',
            from_status='awaiting_merge', to_status='done')
        self.project = Project.objects.create(name='PLS523 Project', key='P523', org_id=1)
        self.item = Item.objects.create(
            mode='task', title='Awaiting merge task', project=self.project,
            item_key='P523-1', status='awaiting_merge',
            workflow_name=self.workflow.name, created_by_id=self.user.id)

    def test_committed_transition_survives_serializer_failure(self):
        """The transition commits; if ItemSerializer enrichment then explodes,
        the caller must get a minimal HTTP-200 degraded receipt, NOT a
        false-negative 500 that hides the durable state change."""
        real_init = None

        def _boom(*args, **kwargs):
            raise RuntimeError('display enrichment blew up after commit')

        # Patch the serializer used by the execute-transition view (imported as
        # `from .serializers import ItemSerializer` inside the handler).
        with patch('tasks.serializers.ItemSerializer', side_effect=_boom):
            response = self.client.post(
                f'/api/items/{self.item.item_key}/execute-transition/',
                {'to_status': 'done', 'transition_id': str(self.transition.id)},
                format='json',
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         'a committed transition must not 500 on enrichment failure')
        self.assertEqual(response.data.get('marker'), 'PLS523_TRANSITION_RECEIPT_DEGRADED')
        self.assertTrue(response.data.get('degraded'))
        self.assertEqual(response.data.get('status'), 'done')
        # The state change is DURABLE — the DB row is 'done' regardless of the
        # serialization failure.
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'done')
