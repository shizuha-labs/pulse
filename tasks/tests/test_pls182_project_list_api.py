"""PLS-182 regression: authenticated project list must not 500."""
from unittest.mock import MagicMock, patch

import requests
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status

from tasks.models import Item, Project, ProjectMembership
from tasks.services.admin_service import admin_service
from tasks.tests.base import AuthenticatedAPITestCase


class PLS182ProjectListAPITests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Pulse',
            key='PLS',
            org_id=1,
            access_level='private',
            is_active=True,
            created_by_id=self.user.id,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='admin',
            is_active=True,
            can_browse=True,
            can_manage_project=True,
        )

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_authenticated_project_list_returns_200_on_prefixed_mount(self, get_org):
        get_org.return_value = {
            'id': 1,
            'name': 'Shizuha Global Pvt. Ltd',
            'slug': 'shizuha',
            'entity_key': 'SG',
        }

        response = self.client.get('/shizuha-pulse/api/projects/', {'page_size': 200, 'page': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['key'], 'PLS')
        self.assertEqual(response.data['results'][0]['organization_slug'], 'shizuha')

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_authenticated_project_list_returns_200_on_direct_mount(self, get_org):
        get_org.return_value = {
            'id': 1,
            'name': 'Shizuha Global Pvt. Ltd',
            'slug': 'shizuha',
            'entity_key': 'SG',
        }

        response = self.client.get('/api/projects/', {'page_size': 200, 'page': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['key'], 'PLS')

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_project_list_marks_visible_read_only_membership_not_writable(self, get_org):
        get_org.return_value = {
            'id': 1,
            'name': 'Shizuha Global Pvt. Ltd',
            'slug': 'shizuha',
            'entity_key': 'SG',
        }
        self.user.is_staff = False
        self.user.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=self.user)
        membership = ProjectMembership.objects.get(project=self.project, user_id=self.user.id)
        membership.can_create = False
        membership.save(update_fields=['can_create'])

        response = self.client.get('/api/projects/', {'page_size': 200, 'page': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertFalse(response.data['results'][0]['can_create'])

    # --- PLS-244: the project list must survive a MISBEHAVING shizuha-admin ---
    # These exercise the REAL admin_service._make_request path (only the HTTP
    # session is mocked), so they prove the org-enrichment failure is contained
    # at the source instead of 500-ing /api/projects/ and blocking agent
    # project-id discovery (the reported regression of PLS-182).

    def _get_project_list(self):
        from django.core.cache import cache
        cache.clear()  # ORG_CACHE would otherwise short-circuit _make_request
        return self.client.get('/api/projects/', {'page_size': 200, 'page': 1})

    def test_project_list_survives_admin_malformed_json_body(self):
        """A 200 from admin with an unparseable body (json() raises) must NOT 500.

        PLS-244 root cause: response.json() raises ValueError (JSONDecodeError),
        which is not a requests.RequestException, so it escaped _make_request and
        propagated through ProjectSerializer.get_organization_slug -> 500.
        """
        malformed = MagicMock()
        malformed.status_code = 200
        malformed.json.side_effect = ValueError('Expecting value: line 1 column 1')
        malformed.text = '<html>502 Bad Gateway</html>'
        with patch.object(admin_service.session, 'request', return_value=malformed):
            response = self._get_project_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['key'], 'PLS')
        # org enrichment degrades to empty, never a mock/partial object
        self.assertEqual(response.data['results'][0]['organization_slug'], '')

    def test_project_list_survives_admin_unreachable(self):
        """A hard connection failure to admin must degrade org fields, not 500."""
        with patch.object(
            admin_service.session, 'request',
            side_effect=requests.ConnectionError('connection refused'),
        ):
            response = self._get_project_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['organization_slug'], '')


class PLS358ProjectListHardeningTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.projects = []
        for idx in range(3):
            project = Project.objects.create(
                name=f'Pulse {idx}',
                key=f'PL{idx}',
                org_id=1,
                access_level='private',
                is_active=True,
                created_by_id=self.user.id,
            )
            self.projects.append(project)
            for member_idx in range(idx + 1):
                ProjectMembership.objects.create(
                    project=project,
                    user_id=1000 + idx * 10 + member_idx,
                    user_email=f'member-{idx}-{member_idx}@example.com',
                    role='member',
                    is_active=True,
                    can_browse=True,
                )
            ProjectMembership.objects.create(
                project=project,
                user_id=2000 + idx,
                user_email=f'inactive-{idx}@example.com',
                role='member',
                is_active=False,
            )
            for item_idx in range(idx + 2):
                Item.objects.create(
                    project=project,
                    mode='action',
                    item_type='action.test',
                    title=f'Item {idx}-{item_idx}',
                    created_by_id=self.user.id,
                )

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_project_list_uses_batched_count_annotations(self, get_org):
        get_org.return_value = {'id': 1, 'name': 'Shizuha', 'slug': 'shizuha'}

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get('/api/projects/', {'page_size': 200, 'page': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_key = {row['key']: row for row in response.data['results']}
        for idx in range(3):
            row = by_key[f'PL{idx}']
            self.assertEqual(row['member_count'], idx + 1)
            self.assertEqual(row['item_count'], idx + 2)
        # Pagination count + annotated page query. This should stay constant as
        # project count grows; a per-project count regression jumps by 2N.
        self.assertLessEqual(len(ctx), 4, [q['sql'] for q in ctx.captured_queries])

    @patch('tasks.services.permissions.PermissionService.get_user_projects')
    def test_project_list_db_operational_error_is_fail_loud(self, get_user_projects):
        get_user_projects.side_effect = OperationalError('transient db unavailable')

        with self.assertLogs('tasks.views', level='ERROR') as logs:
            response = self.client.get('/api/projects/', {'page_size': 200, 'page': 1})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data['code'], 'projects_db_operational_error')
        self.assertEqual(response.data['marker'], 'PLS358_PROJECTS_DB_OPERATIONAL_ERROR')
        self.assertTrue(any('PLS358_PROJECTS_DB_OPERATIONAL_ERROR' in line for line in logs.output))
