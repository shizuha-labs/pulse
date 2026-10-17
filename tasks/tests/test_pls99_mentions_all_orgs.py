"""PLS-99: @-mention member search spans all accessible orgs when unscoped."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.views import ItemViewSet


class _OrgIdQS(list):
    def exclude(self, **kwargs):
        return self
    def values_list(self, *args, **kwargs):
        return self
    def distinct(self):
        return self


class MentionOrgMemberSearchTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = ItemViewSet.as_view({'get': 'search_org_members'})
        self.user = SimpleNamespace(
            id=7,
            email='operator@shizuha.com',
            is_authenticated=True,
            is_staff=False,
            is_superuser=False,
            organization_memberships={'1': 'member', '2': 'admin'},
            is_active=True,
        )

    def _get(self, query=''):
        request = self.factory.get('/api/items/search-org-members/' + query)
        force_authenticate(request, user=self.user)
        return self.view(request)

    def test_unscoped_mentions_union_all_accessible_orgs_and_dedupe(self):
        def members(org_id, search=None):
            data = {
                1: [
                    {'user_id': 10, 'user_email': 'a@example.com', 'user_name': 'A One', 'role': 'member'},
                    {'user_id': 11, 'user_email': 'shared@example.com', 'user_name': 'Shared', 'role': 'member'},
                ],
                2: [
                    {'user_id': 11, 'user_email': 'shared@example.com', 'user_name': 'Shared Again', 'role': 'admin'},
                    {'user_id': 12, 'user_email': 'b@example.com', 'user_name': 'B Two', 'role': 'member'},
                ],
            }
            return data[org_id]

        with patch('tasks.views.PermissionService.get_user_projects', return_value=_OrgIdQS([1, 2])) as projects, \
             patch('tasks.services.admin_service.admin_service.get_org_members', side_effect=members) as get_members:
            response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 3)
        self.assertEqual(
            [m['user_email'] for m in response.data['members']],
            ['a@example.com', 'shared@example.com', 'b@example.com'],
        )
        projects.assert_called_once()
        self.assertEqual([call.args[0] for call in get_members.call_args_list], [1, 2])

    def test_explicit_org_keeps_single_org_scope(self):
        with patch('tasks.views.PermissionService.get_user_projects') as projects, \
             patch('tasks.services.admin_service.admin_service.get_org_members', return_value=[
                 {'user_id': 20, 'user_email': 'only@example.com', 'user_name': 'Only Org'},
             ]) as get_members:
            response = self._get('?organization=2&search=on')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['members'][0]['user_email'], 'only@example.com')
        projects.assert_not_called()
        get_members.assert_called_once_with(2, search='on')
