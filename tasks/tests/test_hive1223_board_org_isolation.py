"""HIVE-1223 (HIVE-327 isolation): the Pulse board list must scope to the user's
org-accessible projects and the selected org — never render another org's boards.

Previously ``BoardViewSet`` / ``BoardColumnViewSet`` did ``Board.objects.all()`` with
no org/user scoping, so a selected org with zero boards rendered OTHER orgs' boards
while the org-scoped item APIs returned zero — a cross-org privacy/trust breach.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import Board, Project

User = get_user_model()

BOARDS_URL = '/shizuha-pulse/api/boards/'


def _ids(resp):
    data = resp.json()
    rows = data if isinstance(data, list) else data.get('results', [])
    # board ids serialize as UUID strings; normalize to str for comparison
    return {str(row['id']) for row in rows}


class Hive1223BoardOrgIsolationTests(APITestCase):
    def setUp(self):
        self.proj_a = Project.objects.create(
            name='Org A Project', key='OA', org_id=7001, access_level='open',
        )
        self.board_a = Board.objects.create(
            project=self.proj_a, name='A Board', board_type='kanban',
        )
        self.proj_b = Project.objects.create(
            name='Org B Project', key='OB', org_id=7002, access_level='open',
        )
        self.board_b = Board.objects.create(
            project=self.proj_b, name='B Board', board_type='kanban',
        )

    def _member(self, username, org_ids, is_staff=False):
        u = User.objects.create_user(
            username=username, email=f'{username}@shizuha.com',
            password='Pass123!x', is_staff=is_staff,
        )
        u.organization_memberships = {oid: 'member' for oid in org_ids}
        return u

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_member_of_org_b_does_not_see_org_a_boards(self):
        """The core breach: a member of only org B must NOT see org A's board."""
        client = self._client(self._member('h1223boardb', [7002]))
        resp = client.get(BOARDS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        ids = _ids(resp)
        self.assertIn(str(self.board_b.id), ids)
        self.assertNotIn(str(self.board_a.id), ids)

    def test_selected_org_scopes_the_board_list(self):
        """A member of both orgs selecting org A sees only org A's board."""
        client = self._client(self._member('h1223boardboth', [7001, 7002]))
        resp = client.get(BOARDS_URL, HTTP_X_ORGANIZATION_ID='7001')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        ids = _ids(resp)
        self.assertIn(str(self.board_a.id), ids)
        self.assertNotIn(str(self.board_b.id), ids)

    def test_staff_still_sees_all_boards(self):
        """Staff/superusers bypass scoping (preserves admin/debug behavior)."""
        client = self._client(self._member('h1223boardstaff', [], is_staff=True))
        resp = client.get(BOARDS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        ids = _ids(resp)
        self.assertIn(str(self.board_a.id), ids)
        self.assertIn(str(self.board_b.id), ids)
