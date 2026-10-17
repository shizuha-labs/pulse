"""HIVE-603: GET /api/items/activity-feed/ — org-visible merged live feed.

The shizuha.com home theater needs one newest-first stream of what the org's
agents are doing: item activity (created/status_changed/assigned/completed…)
merged with comments (with excerpt). Scope piggybacks the ItemViewSet
queryset, so it can never show more than the item list itself.
"""
from datetime import timedelta

from django.utils import timezone

from tasks.models import Comment, Item, ItemActivity, Project
from tasks.tests.base import AuthenticatedAPITestCase

FEED = '/api/items/activity-feed/'


class ActivityFeedTests(AuthenticatedAPITestCase):

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        self.item = Item.objects.create(
            project=self.project, item_key='PLAT-9501', title='Feed source task',
            mode='task', status='open',
            created_by_email='zen@shizuha.com', created_by_id=0,
        )

    def _mk_activity(self, action, **kw):
        return ItemActivity.objects.create(
            item=self.item, action=action, user_id=kw.get('user_id', 5),
            user_email=kw.get('user_email', 'rei@shizuha.com'),
            field_name=kw.get('field_name', ''),
            old_value=kw.get('old_value', ''),
            new_value=kw.get('new_value', ''),
        )

    def test_feed_merges_activity_and_comments_newest_first(self):
        self._mk_activity('status_changed', field_name='status',
                          old_value='open', new_value='in_progress')
        Comment.objects.create(
            item=self.item, author_id=5, author_email='rei@shizuha.com',
            content='Started on the fix — repro confirmed.')
        resp = self.client.get(FEED)
        self.assertEqual(resp.status_code, 200)
        events = resp.data['results']
        self.assertGreaterEqual(len(events), 2)
        # Newest first
        ats = [e['at'] for e in events]
        self.assertEqual(ats, sorted(ats, reverse=True))
        types = {e['type'] for e in events}
        self.assertIn('comment', types)
        self.assertIn('status_changed', types)
        comment_ev = next(e for e in events if e['type'] == 'comment')
        self.assertEqual(comment_ev['item_key'], 'PLAT-9501')
        self.assertEqual(comment_ev['project'], 'PLAT')
        self.assertIn('repro confirmed', comment_ev['excerpt'])

    def test_commented_and_updated_activity_rows_are_excluded(self):
        # 'commented' duplicates the Comment stream; 'updated' is field noise.
        self._mk_activity('commented')
        self._mk_activity('updated')
        resp = self.client.get(FEED)
        types = [e['type'] for e in resp.data['results']]
        self.assertNotIn('commented', types)
        self.assertNotIn('updated', types)

    def test_since_returns_only_newer_events(self):
        old = self._mk_activity('created')
        ItemActivity.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(hours=2))
        cutoff = (timezone.now() - timedelta(minutes=30)).isoformat()
        self._mk_activity('status_changed', field_name='status',
                          old_value='open', new_value='blocked')
        resp = self.client.get(FEED, {'since': cutoff})
        types = [e['type'] for e in resp.data['results']]
        self.assertIn('status_changed', types)
        self.assertNotIn('created', types)

    def test_limit_is_bounded(self):
        for i in range(5):
            self._mk_activity('status_changed', new_value=f'v{i}')
        resp = self.client.get(FEED, {'limit': '2'})
        self.assertEqual(len(resp.data['results']), 2)
        resp = self.client.get(FEED, {'limit': 'garbage'})
        self.assertEqual(resp.status_code, 200)


class ActorAndKeyFilterTests(AuthenticatedAPITestCase):

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Platform2', key='PLAT2', org_id=1, access_level='open')
        self.item = Item.objects.create(
            project=self.project, item_key='PLAT2-1', title='Cockpit peek task',
            mode='task', status='open',
            created_by_email='zen@shizuha.com', created_by_id=0,
        )
        ItemActivity.objects.create(
            item=self.item, action='status_changed', user_id=5,
            user_email='rei@shizuha.com', field_name='status',
            old_value='open', new_value='in_progress')
        ItemActivity.objects.create(
            item=self.item, action='assigned', user_id=6,
            user_email='zen@shizuha.com')
        Comment.objects.create(
            item=self.item, author_id=5, author_email='rei@shizuha.com',
            content='Working on it.')

    def test_actor_param_filters_the_feed(self):
        resp = self.client.get(FEED, {'actor': 'rei@shizuha.com'})
        self.assertEqual(resp.status_code, 200)
        actors = {e['actor_email'].lower() for e in resp.data['results']}
        self.assertEqual(actors, {'rei@shizuha.com'})
        types = {e['type'] for e in resp.data['results']}
        self.assertIn('comment', types)

    def test_item_key_filter_on_list(self):
        resp = self.client.get('/api/items/', {'item_key': 'plat2-1'})
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get('results', resp.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['item_key'], 'PLAT2-1')
