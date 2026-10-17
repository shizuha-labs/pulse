"""
PLS-861 regression tests — CommentViewSet.list must discriminate the four
get_queryset branches at the RESPONSE layer.

Previously all four listed below returned a 200-empty success body, so a refused
list was byte-identical to a genuinely empty one (measured: 236 comment rows
rendered as "No comments." for a caller without access). The fix raises at the
response layer only:

    (a) no `item` filter            -> 400 Bad Request
    (b) item does not exist         -> 404 Not Found
    (c) permission refused          -> 403 Forbidden
    (d) accessible, possibly empty  -> 200 with the normal paginated body

Constraint (PLAT-4195, restated in the ticket): the set of rows a caller can read
must NOT change by one row. Refused callers still receive zero rows.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import Comment, Item, Project

User = get_user_model()


class PLS861CommentListDiscriminatorTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='staff', email='staff@shizuha.com', password='x', is_staff=True,
        )
        self.outsider = User.objects.create_user(
            username='outsider', email='outsider@shizuha.com', password='x', is_staff=False,
        )
        self.private_project = Project.objects.create(
            name='Shizuha Origin', key='ORIG', org_id=1, access_level='private', is_active=True,
        )
        # Private item: only staff (or participants/members) may touch it.
        self.private_item = Item.objects.create(
            mode='task', title='Private origin item', project=self.private_project,
            item_key='ORIG-861', created_by_id=self.staff.id,
        )

    def _comment(self, item, content):
        return Comment.objects.create(
            item=item, content=content, author_id=self.staff.id, author_email=self.staff.email,
        )

    # ── (a) no filter ───────────────────────────────────────────────────────
    def test_no_item_filter_returns_400(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get('/api/comments/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        # Legible — not a 200-empty success body.
        self.assertIn(b'item', response.content)

    # ── (b) item does not exist ─────────────────────────────────────────────
    def test_item_not_found_returns_404_distinct_from_empty(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get('/api/comments/', {'item': 'PLS-999999'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.content)
        # Byte-distinct from the accessible-and-empty 200 (no results payload).
        self.assertNotIn(b'results', response.content)
        self.assertNotIn(b'count', response.content)

    # ── (c) permission-refused vs (d) genuinely-empty ──────────────────────
    def test_refused_list_is_distinct_from_accessible_empty(self):
        # Rows exist on the private item — the refusal is about scope, not absence.
        self._comment(self.private_item, 'refused-secret-handoff')
        self.assertEqual(Comment.objects.filter(item=self.private_item).count(), 1)

        # Non-staff outsider, no participation/membership: refused.
        self.client.force_authenticate(self.outsider)
        refused = self.client.get('/api/comments/', {'item': self.private_item.pk})
        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN, refused.content)
        # Zero rows leak: the 403 body carries no comment data.
        self.assertNotIn(b'refused-secret-handoff', refused.content)
        self.assertNotIn(b'results', refused.content)

        # A genuinely-empty accessible item renders as 200 empty — with rows in
        # the DB elsewhere, proving the discriminator is not "empty database".
        empty_item = Item.objects.create(
            mode='task', title='Accessible empty item', project=self.private_project,
            item_key='ORIG-862', created_by_id=self.staff.id,
        )
        self.client.force_authenticate(self.staff)
        empty = self.client.get('/api/comments/', {'item': empty_item.pk})
        self.assertEqual(empty.status_code, status.HTTP_200_OK, empty.content)
        data = empty.json()
        self.assertEqual(data.get('count'), 0)
        self.assertEqual(data.get('results'), [])

        # The refused response is no longer byte-identical to the empty one.
        self.assertNotEqual(refused.status_code, empty.status_code)
        self.assertNotEqual(refused.content, empty.content)

    # ── (c) no widening — a refused caller still receives zero rows ────────
    def test_refused_caller_receives_zero_rows(self):
        self._comment(self.private_item, 'must-not-leak')
        self.client.force_authenticate(self.outsider)
        response = self.client.get('/api/comments/', {'item': self.private_item.pk})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
        # 403 body is an error payload, not a paginated list; nothing to page through.
        body = response.content.decode('utf-8', 'replace')
        self.assertNotIn('must-not-leak', body)
        self.assertNotIn('"results"', body)
        self.assertNotIn('"count"', body)

    # ── (d) accessible paths unchanged ─────────────────────────────────────
    def test_accessible_with_comments_returns_200_and_rows(self):
        self._comment(self.private_item, 'handoff-1')
        self._comment(self.private_item, 'handoff-2')
        self.client.force_authenticate(self.staff)
        response = self.client.get('/api/comments/', {'item': self.private_item.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        data = response.json()
        self.assertEqual(data.get('count'), 2)
        contents = [c['content'] for c in data.get('results', [])]
        self.assertIn('handoff-1', contents)
        self.assertIn('handoff-2', contents)

    def test_accessible_empty_returns_200_empty(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get('/api/comments/', {'item': self.private_item.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        data = response.json()
        self.assertEqual(data.get('count'), 0)
        self.assertEqual(data.get('results'), [])

    # ── list must keep resolving item keys ─────────────────────────────────
    def test_accessible_list_accepts_item_key(self):
        self._comment(self.private_item, 'by-key')
        self.client.force_authenticate(self.staff)
        response = self.client.get('/api/comments/', {'item': self.private_item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json().get('count'), 1)
