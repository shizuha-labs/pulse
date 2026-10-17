"""
PLS-398 regression tests — comment authorization must use the canonical
item-scoped predicate for alerts and private-project handoffs, not a narrower
CommentViewSet-only check.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, ProjectMembership, TeamRoster
from tasks.permissions import can_comment_on_item, is_item_team_member

User = get_user_model()


class PLS398CommentAuthorizationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.devops_member = User.objects.create_user(
            username='san', email='san@agents.shizuha.io', password='x', is_staff=False,
        )
        self.engineering_member = User.objects.create_user(
            username='kai', email='kai@agents.shizuha.io', password='x', is_staff=False,
        )
        self.project_member = User.objects.create_user(
            username='qa', email='qa@agents.shizuha.io', password='x', is_staff=False,
        )
        self.outsider = User.objects.create_user(
            username='outsider', email='outsider@shizuha.com', password='x', is_staff=False,
        )
        self.origin = Project.objects.create(
            name='Shizuha Origin', key='ORIG', org_id=1, access_level='private', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={'tiers': [['san@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['kai@shizuha.com']], 'is_active': True},
        )

    def _post_comment(self, user, item, content='handoff'):
        self.client.force_authenticate(user)
        return self.client.post('/api/comments/', {'item': item.id, 'content': content}, format='json')

    def test_projectless_alert_team_membership_fails_closed_without_org_context(self):
        alert = Item.objects.create(
            mode='alert', item_type='alert.error', title='Origin CI failed',
            assignment_group='devops', source='origin-ci', status='firing',
        )

        # A Team name alone cannot select a roster across organizations. Alerts
        # must carry a project/org (or a direct participant) before Team
        # membership can authorize access.
        self.assertFalse(is_item_team_member(self.devops_member, alert))
        self.assertFalse(can_comment_on_item(self.devops_member, alert))
        response = self._post_comment(self.devops_member, alert)
        self.assertEqual(response.status_code, 400, response.content)

    def test_private_origin_team_routed_handler_without_project_membership_can_comment(self):
        item = Item.objects.create(
            mode='task', title='Origin handoff', project=self.origin,
            assignment_group='engineering',
        )
        self.assertFalse(ProjectMembership.objects.filter(project=self.origin, user_id=self.engineering_member.id).exists())
        response = self._post_comment(self.engineering_member, item)
        self.assertEqual(response.status_code, 201, response.content)

    def test_project_membership_email_alias_can_comment(self):
        item = Item.objects.create(mode='task', title='Private member item', project=self.origin)
        ProjectMembership.objects.create(
            project=self.origin,
            user_id=None,
            user_email='qa@shizuha.com',
            role='member',
            is_active=True,
        )
        response = self._post_comment(self.project_member, item)
        self.assertEqual(response.status_code, 201, response.content)

    def test_outsider_cannot_comment_on_private_project_item(self):
        item = Item.objects.create(mode='task', title='Private item', project=self.origin)
        response = self._post_comment(self.outsider, item)
        self.assertEqual(response.status_code, 400)
        self.assertIn('access to comment', str(response.content))

    def test_outsider_cannot_comment_on_unrelated_projectless_non_alert_item(self):
        item = Item.objects.create(mode='task', title='Personal task')
        response = self._post_comment(self.outsider, item)
        self.assertEqual(response.status_code, 400)
        self.assertIn('access to comment', str(response.content))


class PLS716CommentListAccessDenialTests(APITestCase):
    """PLS-716: listing comments on an inaccessible item must be a 403, not
    a success-shaped empty thread.

    An agent reading a thread it lacks access to must not be able to conclude
    the thread is empty (a false-negative that reads as ground truth). The
    /comments/ list endpoint previously returned Comment.objects.none() (200,
    count=0) for an inaccessible item, which the MCP tool rendered as
    "No comments." — diverging from the item retrieve endpoint's 403.
    """

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='owner', email='owner@shizuha.com', password='x', is_staff=False,
        )
        self.outsider = User.objects.create_user(
            username='outsider', email='outsider@shizuha.com', password='x', is_staff=False,
        )
        self.private = Project.objects.create(
            name='Private', key='PRIV', org_id=99, access_level='private', is_active=True,
        )
        ProjectMembership.objects.create(
            project=self.private,
            user_id=self.owner.id,
            role='member',
            is_active=True,
        )
        self.item = Item.objects.create(mode='task', title='Private item', project=self.private)
        from tasks.models import Comment
        Comment.objects.create(
            item=self.item, author_id=self.owner.id, author_email='owner@shizuha.com',
            content='visible only to the owner',
        )

    def test_outsider_listing_comments_on_inaccessible_item_gets_403(self):
        # PLS-716: the MCP tool pulse_list_comments must surface a 403 (via
        # _fmt_error), never a success-shaped "No comments." — an empty result
        # reads as ground truth and an agent would act on a false conclusion.
        self.client.force_authenticate(self.outsider)
        response = self.client.get('/api/comments/', {'item': self.item.id})
        self.assertEqual(response.status_code, 403)
        self.assertIn('access', str(response.content).lower())

    def test_owner_listing_comments_on_accessible_item_gets_200_with_thread(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get('/api/comments/', {'item': self.item.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['content'], 'visible only to the owner')
