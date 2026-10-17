"""
PLS-72: pulse_create_sprint returned HTTP 500 for every project.

Root cause: SprintViewSet.perform_create called the non-existent
`PermissionService.can_create_items(...)` for non-staff users, raising
AttributeError -> 500 on POST /api/sprints/. The fix uses the canonical
`PermissionService.can_access_project(user_id, project, permission='create',
user_email=...)` (matching ItemViewSet.perform_create).

These tests exercise the non-staff branch (staff bypass the check) and mock
`can_access_project` to isolate the method-name fix from the org/project
membership + admin-service lookup.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import Project, Sprint

User = get_user_model()

SPRINTS_URL = '/shizuha-pulse/api/sprints/'


class SprintCreatePermissionTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Non-staff: must hit the permission branch that previously 500'd.
        self.user = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x', is_staff=False,
        )
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(
            name='Connect', key='CON', org_id=1, access_level='open', is_active=True,
        )

    @mock.patch('tasks.services.permissions.PermissionService.can_access_project', return_value=True)
    def test_non_staff_with_permission_creates_sprint(self, _can):
        """Regression: previously AttributeError -> 500. Now 201."""
        resp = self.client.post(SPRINTS_URL, {
            'project': self.project.id,
            'name': 'Sprint 1',
            'goal': 'ship',
            'start_date': '2026-06-20',
            'end_date': '2026-07-04',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertIn('id', resp.data)
        self.assertTrue(Sprint.objects.filter(project=self.project, name='Sprint 1').exists())
        # The request permission and create hook both fail closed through the
        # same canonical project-authority predicate.
        self.assertEqual(_can.call_count, 2)

    @mock.patch('tasks.services.permissions.PermissionService.can_access_project', return_value=False)
    def test_non_staff_without_permission_gets_403_not_500(self, _can):
        resp = self.client.post(SPRINTS_URL, {
            'project': self.project.id,
            'name': 'Sprint 2',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.content)
        _can.assert_called_once()
        self.assertFalse(Sprint.objects.filter(name='Sprint 2').exists())
