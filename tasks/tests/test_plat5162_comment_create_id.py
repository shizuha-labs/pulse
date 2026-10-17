"""
PLAT-5162 live-contract regression — POST /comments/ 201 must include id.

The approved live tenant-isolation harness (PLAT-4196) and MCP clients address
a created comment by its stable ``id`` for cross-tenant detail/reaction probes.
CommentCreateSerializer.Meta.fields omitted ``id``, so a successful create
returned 201 with only item/content/parent and the caller had no handle on the
new row. This test asserts 201 + ``id`` through the real CommentViewSet create
boundary, and that write validation/redaction is unchanged.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, ProjectMembership, TeamRoster
from tasks.serializers import CommentCreateSerializer

User = get_user_model()


class PLAT5162CommentCreateIdTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.member = User.objects.create_user(
            username='qa', email='qa@shizuha.com', password='x', is_staff=False,
        )
        self.origin = Project.objects.create(
            name='Shizuha Origin', key='ORIG', org_id=1, access_level='private',
            is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['qa@shizuha.com']], 'is_active': True},
        )
        self.item = Item.objects.create(
            mode='task', title='Origin handoff', project=self.origin,
            assignment_group='engineering',
        )

    def _post_comment(self, content='live contract probe'):
        self.client.force_authenticate(self.member)
        return self.client.post(
            '/api/comments/', {'item': self.item.id, 'content': content},
            format='json',
        )

    def test_create_response_includes_comment_id(self):
        response = self._post_comment()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIn('id', body)
        self.assertIsInstance(body['id'], int)
        # The returned id must be the actual persisted row, not a placeholder.
        from tasks.models import Comment
        self.assertTrue(Comment.objects.filter(id=body['id'], item=self.item).exists())

    def test_create_response_id_is_stable_across_contract(self):
        """The create response exposes the same shape consumers read back."""
        response = self._post_comment('second probe')
        body = response.json()
        # Camel/snake consistency: id is surfaced, and the persisted row's
        # full serializer (the detail boundary) resolves the same id.
        from tasks.models import Comment
        comment = Comment.objects.get(id=body['id'])
        self.assertEqual(comment.content, 'second probe')
        self.assertEqual(comment.item_id, self.item.id)
        self.assertIsNotNone(comment.author_email)

    def test_create_serializer_contract_exposes_id(self):
        """The serializer field contract must keep id in the create response."""
        meta_fields = CommentCreateSerializer.Meta.fields
        self.assertIn('id', meta_fields)

    def test_write_validation_unchanged_rejects_empty_content(self):
        response = self._post_comment(content='   ')
        self.assertEqual(response.status_code, 400, response.content)

    def test_create_redacts_secrets_in_persisted_content(self):
        # A realistic high-entropy hex secret the scanner redacts (PLAT-39).
        secret = 'abcdef1234567890abcdef1234567890abcdef12'
        response = self._post_comment(
            content=f'token={secret} please rotate'
        )
        self.assertEqual(response.status_code, 201, response.content)
        from tasks.models import Comment
        comment = Comment.objects.get(id=response.json()['id'])
        self.assertNotIn(secret, comment.content)
        self.assertIn('[REDACTED:', comment.content)
