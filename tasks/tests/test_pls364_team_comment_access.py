"""
PLS-364 regression tests — members of an item's assignment_group roster must be
able to comment on the item even in a private project they are not a member of
(team-routed work on e.g. Ventures is picked up by roster members who are
neither assignee nor project members; comments are the primary handoff channel).

Complements PLAT-257 (assignee/reporter participant carve-out) — that fix
covers the exact VEN-84 symptom (assignee 403); this adds the acceptance's
assignment_group clause. Team-membership runs BEFORE any org/project lookup so
these tests make no admin-service network calls; negative controls confirm a
true outsider still cannot comment on a private-project item.
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, TeamRoster
from tasks.permissions import is_item_team_member

User = get_user_model()


class PLS364TeamCommentAccessTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Roster member of 'engineering' — NOT assignee, NOT project member.
        self.member = User.objects.create_user(
            username='jun', email='jun@shizuha.com', password='x', is_staff=False,
        )
        # Non-member, non-participant outsider (negative control).
        self.outsider = User.objects.create_user(
            username='intruder', email='intruder@shizuha.com', password='x', is_staff=False,
        )
        self.project = Project.objects.create(
            name='Ventures', key='VEN', org_id=1, access_level='private', is_active=True,
        )
        # The roster row may be auto-seeded — upsert deterministically.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['JUN@Shizuha.COM', 'kai@shizuha.com'], ['failover@shizuha.com']],
                'is_active': True,
            },
        )
        self.item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignment_group='engineering',
        )

    def _post_comment(self, user, item):
        self.client.force_authenticate(user)
        return self.client.post('/api/comments/', {'item': item.id, 'content': 'hi'}, format='json')

    def test_roster_member_can_comment_on_private_project_item(self):
        # Email match is case-insensitive against the roster entry.
        self.assertEqual(self._post_comment(self.member, self.item).status_code, 201)

    def test_failover_tier_members_also_count(self):
        failover = User.objects.create_user(
            username='fo', email='failover@shizuha.com', password='x', is_staff=False,
        )
        self.assertEqual(self._post_comment(failover, self.item).status_code, 201)

    def test_outsider_still_denied_on_private_project_item(self):
        resp = self._post_comment(self.outsider, self.item)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('access to comment', str(resp.content))

    def test_inactive_roster_row_does_not_grant_access(self):
        TeamRoster.objects.filter(team_name='engineering').update(is_active=False)
        resp = self._post_comment(self.member, self.item)
        self.assertEqual(resp.status_code, 400)

    def test_item_without_assignment_group_not_granted(self):
        item = Item.objects.create(mode='task', title='T2', project=self.project)
        resp = self._post_comment(self.member, item)
        self.assertEqual(resp.status_code, 400)

    def test_helper_fails_closed_on_malformed_tiers(self):
        TeamRoster.objects.filter(team_name='engineering').update(tiers={'bad': 'shape'})
        self.assertFalse(is_item_team_member(self.member, self.item))
