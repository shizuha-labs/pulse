"""PLAT-13258: a PR head move re-queues an in-review item.

A stamp covers one SHA. When the webhook records a new head, the item
leaves in_review and the lapse is counted. A head that did not change,
or an item that is not in review, is left alone.
"""
import json
from unittest import mock
from django.db.models import QuerySet

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from tasks.models import Item, ItemActivity
from tasks.views import GitHubWebhookView

PR_URL = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/1938'


def _item(status):
    return Item.objects.create(
        item_key='PLAT-13258',
        title='review requeue',
        status=status,
        workflow_name='autonomous-dev',
        metadata={'pull_requests': [{
            'url': PR_URL,
            'repo': 'shizuha-labs/deploy',
            'number': 1938,
            'status': 'open',
            'head_sha': '1e0fe3616610',
        }]},
    )


def _post(sha):
    payload = {
        'action': 'synchronize',
        'pull_request': {
            'html_url': PR_URL,
            'state': 'open',
            'merged': False,
            'head': {'sha': sha},
            'user': {'login': 'kei'},
        },
    }
    request = APIRequestFactory().post(
        '/api/webhooks/github/',
        data=json.dumps(payload).encode('utf-8'),
        content_type='application/json',
        HTTP_X_GITHUB_EVENT='pull_request',
    )
    with mock.patch.dict('os.environ', {'PULSE_SUBSTRATE_CAP_MODE': 'off'}, clear=True):
        return GitHubWebhookView.as_view()(request)


class ReviewRequeueOnHeadChangeTests(TestCase):
    def test_head_move_requeues_in_review_and_counts_the_lapse(self):
        # deploy#1938 class: official approvals on 1e0fe361, then the head moved.
        item = _item('in_review')
        response = _post('aaaaaaaaaaaa')
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertEqual(item.metadata['pull_requests'][0]['head_sha'], 'aaaaaaaaaaaa')
        self.assertEqual(item.metadata['review_lapse_count'], 1)
        self.assertTrue(ItemActivity.objects.filter(
            item=item, action='status_changed',
            old_value='in_review', new_value='in_progress',
        ).exists())

    def test_second_head_move_increments_the_lapse_counter(self):
        item = _item('in_review')
        _post('bbbbbbbbbbbb')
        item.status = 'in_review'
        item.save(update_fields=['status'])
        _post('cccccccccccc')
        item.refresh_from_db()
        self.assertEqual(item.metadata['review_lapse_count'], 2)

    def test_same_head_does_not_requeue(self):
        item = _item('in_review')
        response = _post('1e0fe3616610')
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertNotIn('review_lapse_count', item.metadata)

    def test_head_move_on_an_item_not_in_review_is_not_a_lapse(self):
        item = _item('in_progress')
        _post('dddddddddddd')
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertNotIn('review_lapse_count', item.metadata)

    def test_zero_row_update_is_not_a_lapse(self):
        # The item is in_review in memory, but a concurrent writer already
        # moved the row. The conditional update matches nothing.
        item = _item('in_review')
        real = QuerySet.update

        def update_none(qs, **kwargs):
            if kwargs.get('status') == 'in_progress':
                return 0
            return real(qs, **kwargs)

        with mock.patch.object(QuerySet, 'update', update_none):
            response = _post('eeeeeeeeeeee')
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, 'in_review')
        self.assertNotIn('review_lapse_count', item.metadata)
        self.assertFalse(ItemActivity.objects.filter(
            item=item, action='status_changed').exists())
