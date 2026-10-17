"""
Saved-filter round-trip tests (PLS-818).

Covers the Pulse F0 contract: create / name / reopen / execute / edit /
delete a saved JQL filter, plus AC-3 guards:
  - duplicate names are rejected (per owner + project scope, case-insensitive)
  - malformed JQL is rejected with an explicit non-partial error
  - non-owners cannot edit/delete a saved filter
"""
from unittest import mock

from django.contrib.auth import get_user_model

from tasks.models import Item, Project, SavedFilter
from tasks.tests.base import AuthenticatedAPITestCase

User = get_user_model()

VALID_JQL = 'status = "pending" AND assignee = currentUser()'
MALFORMED_JQL = 'status = "pending" AND (assignee = currentUser()'


class SavedFilterCreateTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Saved Filter Proj', key='SFIL', org_id=1,
            access_level='open',
        )

    def _create(self, data=None):
        payload = {
            'name': 'My Open Tasks',
            'jql': VALID_JQL,
            'project': self.project.id,
        }
        if data:
            payload.update(data)
        return self.client.post('/api/saved-filters/', payload, format='json')

    def test_create_saved_filter_round_trip(self):
        """Create -> retrieve -> execute a saved filter."""
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body['name'], 'My Open Tasks')
        self.assertEqual(body['jql'], VALID_JQL)
        self.assertEqual(body['user_id'], self.user.id)
        self.assertEqual(body['project_key'], 'SFIL')
        filter_id = body['id']

        # Reopen (list / retrieve)
        get_resp = self.client.get(f'/api/saved-filters/{filter_id}/')
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()['name'], 'My Open Tasks')

        # Execute returns the saved filter payload
        exec_resp = self.client.get(f'/api/saved-filters/{filter_id}/execute/')
        self.assertEqual(exec_resp.status_code, 200, exec_resp.content)
        self.assertEqual(exec_resp.json()['filter_name'], 'My Open Tasks')
        self.assertIn('items', exec_resp.json())

    def test_duplicate_name_rejected_case_insensitive(self):
        """AC-3: duplicate names (same owner + project) are rejected."""
        self.assertEqual(self._create().status_code, 201)
        dup = self._create({'name': 'my open tasks'})  # case-insensitive dup
        self.assertEqual(dup.status_code, 400, dup.content)
        detail = dup.json()
        self.assertIn('already exists', str(detail))

    def test_same_name_different_project_allowed(self):
        """Same name in a different project scope is not a duplicate."""
        other = Project.objects.create(
            name='Other Proj', key='SFIL2', org_id=1, access_level='open',
        )
        self.assertEqual(self._create().status_code, 201)
        resp = self._create({'name': 'My Open Tasks', 'project': other.id})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_malformed_jql_rejected(self):
        """AC-3: malformed JQL is rejected with an explicit error."""
        resp = self._create({'jql': MALFORMED_JQL})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('Malformed JQL', str(resp.json()))

    def test_empty_jql_rejected(self):
        resp = self._create({'jql': '   '})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_empty_name_rejected(self):
        resp = self._create({'name': '   '})
        self.assertEqual(resp.status_code, 400, resp.content)


class SavedFilterUpdateDeleteTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Saved Filter Proj', key='SFIL', org_id=1,
            access_level='open',
        )
        self.saved = SavedFilter.objects.create(
            name='Initial', jql='status = "pending"',
            user_id=self.user.id, user_email=self.user.email,
            project=self.project,
        )

    def test_edit_round_trip(self):
        """Edit name + JQL, then reopen shows the new values."""
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'name': 'Renamed', 'jql': 'priority = "high"'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.saved.refresh_from_db()
        self.assertEqual(self.saved.name, 'Renamed')
        self.assertEqual(self.saved.jql, 'priority = "high"')

    def test_edit_to_duplicate_name_rejected(self):
        """Editing to a name already used by another filter is rejected."""
        SavedFilter.objects.create(
            name='Existing', jql='status = "pending"',
            user_id=self.user.id, user_email=self.user.email,
            project=self.project,
        )
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'name': 'Existing'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('already exists', str(resp.json()))

    def test_edit_keeps_own_name(self):
        """Editing another field (not name) must not trip its own dup check."""
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'jql': 'priority = "urgent"'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_edit_malformed_jql_rejected(self):
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'jql': MALFORMED_JQL},
            format='json',
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('Malformed JQL', str(resp.json()))

    def test_delete_round_trip(self):
        resp = self.client.delete(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(SavedFilter.objects.filter(pk=self.saved.id).exists())
        # Reopen after delete -> 404
        get_resp = self.client.get(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(get_resp.status_code, 404)


class SavedFilterOwnershipTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.user
        self.other = User.objects.create_user(
            username='other', email='other@example.com',
            password='testpass123', is_staff=False,
        )
        self.saved = SavedFilter.objects.create(
            name='Owner Filter', jql='status = "pending"',
            user_id=self.owner.id, user_email=self.owner.email,
        )

    def test_non_owner_cannot_edit(self):
        # Unshared filter: invisible to non-owner -> 404 (no existence leak)
        self.client.force_authenticate(user=self.other)
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'name': 'Hijacked'},
            format='json',
        )
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_non_owner_cannot_edit_shared_filter(self):
        # Shared project-scoped filter is visible to non-owner but not editable -> 403
        project = Project.objects.create(
            name='Shared Proj', key='SHAR', org_id=1, access_level='open',
        )
        self.saved.project = project
        self.saved.is_shared = True
        self.saved.save(update_fields=['project', 'is_shared'])
        with mock.patch(
            'tasks.views.PermissionService.get_user_projects',
            return_value=Project.objects.filter(pk=project.pk),
        ):
            self.client.force_authenticate(user=self.other)
            resp = self.client.patch(
                f'/api/saved-filters/{self.saved.id}/',
                {'name': 'Hijacked'},
                format='json',
            )
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_non_owner_cannot_delete(self):
        self.client.force_authenticate(user=self.other)
        resp = self.client.delete(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertTrue(SavedFilter.objects.filter(pk=self.saved.id).exists())

    def test_owner_can_delete(self):
        resp = self.client.delete(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(resp.status_code, 204, resp.content)


class SavedFilterJwtStringUserIdTests(AuthenticatedAPITestCase):
    """PLS-985: JWT user_id is a string claim; owner checks must compare as ints.

    The auth layer (ShizuhaJWTAuthentication -> FederatedUser) sets
    ``request.user.id`` from the raw JWT ``user_id`` claim, which is a string
    (``"user_id": "123"``). SavedFilter.user_id is an IntegerField, so a direct
    ``saved_filter.user_id != request.user.id`` comparison evaluates
    ``123 != '123'`` -> True and wrongly 403s the owner. Regression tests below
    assert the owner can delete/update/toggle-share with a string user id.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = 123
        self.saved = SavedFilter.objects.create(
            name='JWT Owner Filter', jql='status = "pending"',
            user_id=self.owner_id, user_email='san@shizuha.com',
        )
        # FederatedUser with a string user_id (mimics the JWT claim).
        from tasks.authentication import FederatedUser
        self.jwt_user = FederatedUser(
            user_id=str(self.owner_id), email='san@shizuha.com',
        )
        self.client.force_authenticate(user=self.jwt_user)

    def test_owner_with_string_user_id_can_delete(self):
        resp = self.client.delete(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(SavedFilter.objects.filter(pk=self.saved.id).exists())

    def test_owner_with_string_user_id_can_edit(self):
        resp = self.client.patch(
            f'/api/saved-filters/{self.saved.id}/',
            {'name': 'Renamed By JWT Owner'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.saved.refresh_from_db()
        self.assertEqual(self.saved.name, 'Renamed By JWT Owner')

    def test_owner_with_string_user_id_can_toggle_share(self):
        resp = self.client.post(
            f'/api/saved-filters/{self.saved.id}/toggle_share/',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.saved.refresh_from_db()
        self.assertTrue(self.saved.is_shared)

    def test_non_owner_with_string_user_id_cannot_delete(self):
        from tasks.authentication import FederatedUser
        other = FederatedUser(user_id='999', email='other@shizuha.com')
        self.client.force_authenticate(user=other)
        resp = self.client.delete(f'/api/saved-filters/{self.saved.id}/')
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertTrue(SavedFilter.objects.filter(pk=self.saved.id).exists())

class PLS831MalformedJQLItemsTests(AuthenticatedAPITestCase):
    """PLS-831: malformed JQL on the items API must fail explicitly (400) and
    never silently degrade to an unfiltered query (the MCP list path)."""

    def setUp(self):
        super().setUp()
        Item.objects.create(
            mode='task', title='T1', status='in_progress', priority='high',
            created_by_id=self.user.id,
        )

    def _items(self, jql):
        return self.client.get('/api/items/', {'jql': jql, 'mode': 'task'})

    def test_unbalanced_parens_rejected(self):
        """`(((` must be a 400 with a jql error, not the unfiltered queue."""
        resp = self._items('(((')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('jql', resp.json())

    def test_incomplete_operator_rejected(self):
        """`status =` must be a 400 (missing value), not the unfiltered queue."""
        resp = self._items('status =')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('jql', resp.json())

    def test_triple_equals_rejected(self):
        """`status === 'in_progress'` must be a 400 (unsupported operator /
        leftover tokens), not silently treated as status='='."""
        resp = self._items("status === 'in_progress'")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('jql', resp.json())

    def test_valid_control_query_returns_rows(self):
        """A valid JQL control still filters normally (no regression)."""
        resp = self._items("status = 'in_progress'")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['count'], 1)

    def test_no_task_rows_on_validation_failure(self):
        """Validation failure must return zero task data (no unfiltered leak)."""
        resp = self._items('(((')
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        # DRF error payload carries the jql field error, not a task list.
        self.assertNotIn('results', body)
        self.assertNotIn('count', body)
