"""
Exhaustive E2E tests for Phase 5: Security Schemes, Permission Schemes.
"""
from rest_framework import status as http_status
from tasks.models import (
    SecurityScheme, SecurityLevel,
    PermissionScheme, PermissionGrant,
)
from .base import AuthenticatedAPITestCase


class SecuritySchemeAPITests(AuthenticatedAPITestCase):
    """CRUD + nested level tests for SecurityScheme API."""

    def test_create_security_scheme(self):
        resp = self.client.post('/shizuha-pulse/api/security-schemes/', {
            'name': 'Default Security',
            'slug': 'default-security',
            'description': 'Standard security scheme',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['name'], 'Default Security')

    def test_add_security_level(self):
        """Add a security level to a scheme."""
        scheme = SecurityScheme.objects.create(
            name='Test Security', slug='test-sec-level'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/security-schemes/{scheme.id}/add_level/',
            {
                'name': 'Internal Only',
                'slug': 'internal',
                'description': 'Visible only to internal team',
                'position': 0,
                'members': [
                    {'type': 'role', 'value': 'admin'},
                    {'type': 'role', 'value': 'member'},
                ],
            },
            format='json'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.levels.count(), 1)
        level = scheme.levels.first()
        self.assertEqual(level.name, 'Internal Only')
        self.assertEqual(len(level.members), 2)

    def test_add_multiple_levels(self):
        """Create a scheme with public → internal → confidential levels."""
        scheme = SecurityScheme.objects.create(
            name='Multi Level', slug='multi-level-sec'
        )
        levels_data = [
            {'name': 'Public', 'slug': 'public', 'position': 0,
             'members': [{'type': 'anyone'}]},
            {'name': 'Internal', 'slug': 'internal', 'position': 1,
             'members': [{'type': 'role', 'value': 'member'}, {'type': 'role', 'value': 'admin'}]},
            {'name': 'Confidential', 'slug': 'confidential', 'position': 2,
             'members': [{'type': 'role', 'value': 'admin'}, {'type': 'project_lead'}]},
        ]
        for ld in levels_data:
            resp = self.client.post(
                f'/shizuha-pulse/api/security-schemes/{scheme.id}/add_level/',
                ld, format='json'
            )
            self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.levels.count(), 3)

    def test_remove_security_level(self):
        scheme = SecurityScheme.objects.create(
            name='Remove Level', slug='remove-level-sec'
        )
        level = SecurityLevel.objects.create(
            scheme=scheme, name='Temp', slug='temp', position=0
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/security-schemes/{scheme.id}/remove_level/',
            {'level_id': str(level.id)}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(scheme.levels.count(), 0)

    def test_set_default_level(self):
        scheme = SecurityScheme.objects.create(
            name='Default Level', slug='default-level-sec'
        )
        level = SecurityLevel.objects.create(
            scheme=scheme, name='Internal', slug='internal', position=0
        )
        resp = self.client.patch(f'/shizuha-pulse/api/security-schemes/{scheme.id}/', {
            'default_level': str(level.id),
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        scheme.refresh_from_db()
        self.assertEqual(scheme.default_level, level)

    def test_security_level_members_types(self):
        """Test all member types: role, user, group, project_lead, reporter, assignee."""
        scheme = SecurityScheme.objects.create(
            name='Members Test', slug='members-test-sec'
        )
        members = [
            {'type': 'role', 'value': 'admin'},
            {'type': 'user', 'user_id': 42},
            {'type': 'group', 'group_name': 'security-team'},
            {'type': 'project_lead'},
            {'type': 'reporter'},
            {'type': 'assignee'},
        ]
        resp = self.client.post(
            f'/shizuha-pulse/api/security-schemes/{scheme.id}/add_level/',
            {'name': 'All Types', 'slug': 'all-types', 'position': 0, 'members': members},
            format='json'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        level = scheme.levels.first()
        self.assertEqual(len(level.members), 6)

    def test_list_security_schemes(self):
        SecurityScheme.objects.create(name='S1', slug='s1-sec')
        SecurityScheme.objects.create(name='S2', slug='s2-sec')
        resp = self.client.get('/shizuha-pulse/api/security-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_delete_security_scheme(self):
        scheme = SecurityScheme.objects.create(name='Del', slug='del-sec')
        resp = self.client.delete(f'/shizuha-pulse/api/security-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class PermissionSchemeAPITests(AuthenticatedAPITestCase):
    """CRUD + grant tests for PermissionScheme API."""

    def test_seeded_scheme_exists(self):
        """Default permission scheme should exist with grants."""
        resp = self.client.get('/shizuha-pulse/api/permission-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [s['slug'] for s in results]
        self.assertIn('default', slugs)

    def test_default_scheme_has_grants(self):
        scheme = PermissionScheme.objects.get(slug='default')
        self.assertGreater(scheme.grants.count(), 0)

    def test_create_permission_scheme(self):
        resp = self.client.post('/shizuha-pulse/api/permission-schemes/', {
            'name': 'Restricted',
            'slug': 'restricted',
            'description': 'Limited permissions',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_add_grant(self):
        """Add a permission grant to a scheme."""
        scheme = PermissionScheme.objects.create(
            name='Grant Test', slug='grant-test'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/permission-schemes/{scheme.id}/add_grant/',
            {
                'permission': 'create_issues',
                'holder_type': 'role',
                'holder_value': 'member',
            }
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.grants.count(), 1)
        grant = scheme.grants.first()
        self.assertEqual(grant.permission, 'create_issues')
        self.assertEqual(grant.holder_type, 'role')

    def test_add_multiple_grants_same_permission(self):
        """Same permission can have multiple holders."""
        scheme = PermissionScheme.objects.create(
            name='Multi Grant', slug='multi-grant'
        )
        # Grant to role member
        self.client.post(
            f'/shizuha-pulse/api/permission-schemes/{scheme.id}/add_grant/',
            {'permission': 'edit_issues', 'holder_type': 'role', 'holder_value': 'member'}
        )
        # Grant to project lead
        self.client.post(
            f'/shizuha-pulse/api/permission-schemes/{scheme.id}/add_grant/',
            {'permission': 'edit_issues', 'holder_type': 'project_lead', 'holder_value': ''}
        )
        self.assertEqual(scheme.grants.filter(permission='edit_issues').count(), 2)

    def test_remove_grant(self):
        scheme = PermissionScheme.objects.create(
            name='Remove Grant', slug='remove-grant'
        )
        grant = PermissionGrant.objects.create(
            scheme=scheme, permission='delete_issues',
            holder_type='role', holder_value='admin'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/permission-schemes/{scheme.id}/remove_grant/',
            {'grant_id': str(grant.id)}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(scheme.grants.count(), 0)

    def test_all_33_permissions_valid(self):
        """Verify all 33 permission types exist."""
        expected_permissions = [
            'browse_projects', 'administer_projects',
            'create_issues', 'edit_issues', 'delete_issues', 'close_issues',
            'transition_issues', 'assign_issues', 'assignable_user',
            'move_issues', 'schedule_issues', 'set_security',
            'modify_reporter', 'link_issues', 'bulk_change',
            'add_comments', 'edit_comments', 'edit_own_comments',
            'delete_comments', 'delete_own_comments',
            'create_attachments', 'delete_attachments', 'delete_own_attachments',
            'work_on_issues', 'edit_own_worklogs', 'edit_all_worklogs',
            'delete_own_worklogs', 'delete_all_worklogs',
            'manage_watchers', 'view_voters_and_watchers',
            'manage_sprints', 'view_read_only_workflow', 'view_dev_tools',
        ]
        valid_choices = [c[0] for c in PermissionGrant.PERMISSION_CHOICES]
        for perm in expected_permissions:
            self.assertIn(perm, valid_choices, f"Missing permission: {perm}")
        self.assertEqual(len(valid_choices), 33)

    def test_all_8_holder_types_valid(self):
        """Verify all 8 holder types."""
        expected = ['role', 'group', 'user', 'project_lead', 'reporter',
                     'assignee', 'anyone', 'logged_in']
        valid_choices = [c[0] for c in PermissionGrant.HOLDER_TYPE_CHOICES]
        for ht in expected:
            self.assertIn(ht, valid_choices)
        self.assertEqual(len(valid_choices), 8)

    def test_delete_permission_scheme(self):
        scheme = PermissionScheme.objects.create(name='Del', slug='del-perm')
        resp = self.client.delete(f'/shizuha-pulse/api/permission-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)

    def test_update_permission_scheme(self):
        scheme = PermissionScheme.objects.create(name='Old', slug='upd-perm')
        resp = self.client.patch(f'/shizuha-pulse/api/permission-schemes/{scheme.id}/', {
            'name': 'Updated',
            'description': 'Updated desc',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        scheme.refresh_from_db()
        self.assertEqual(scheme.name, 'Updated')
