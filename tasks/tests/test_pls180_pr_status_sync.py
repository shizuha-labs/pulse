"""PLS-180: linked PR status sync updates stale Pulse metadata."""
import os
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import (
    DeploymentEvent, Item, ItemActivity, LinkedPullRequestMergeAttestation,
)
from tasks.serializers import ItemListSerializer
from tasks.tasks import _linked_pr_api_request, sync_linked_pr_statuses


class _GitHubResponse:
    status_code = 200
    ok = True

    def __init__(
        self, *, merged=True, state='closed',
        merged_at='2026-07-01T01:19:49Z', head_sha=None,
    ):
        self._payload = {
            'merged': merged,
            'state': state,
            'merged_at': merged_at,
            'merged_by': {'login': 'kai2574'},
        }
        if head_sha:
            self._payload['head'] = {'sha': head_sha}
        if merged:
            self._payload.update({
                'head': {'sha': head_sha or ('1' * 40)},
                'merge_commit_sha': '2' * 40,
            })
    def json(self):
        return self._payload


class _ForgejoResponse:
    status_code = 200
    ok = True

    def __init__(
        self, *, merged=True, state='closed',
        merged_at='2026-07-02T10:00:00Z', head_sha=None,
    ):
        self._payload = {
            'merged': merged,
            'state': state,
            'merged_at': merged_at,
            'merged_by': {'login': 'aoi'},
        }
        if head_sha:
            self._payload['head'] = {'sha': head_sha}
        if merged:
            self._payload.update({
                'head': {'sha': head_sha or ('3' * 40)},
                'merge_commit_sha': '4' * 40,
            })
    def json(self):
        return self._payload


class _ErrorResponse:
    ok = False

    def __init__(self, status_code=401):
        self.status_code = status_code

    def json(self):
        return {}


def _item(key, *, status='in_progress', pr_status='open', number=1, url=None, repo='shizuha-labs/shizuha-tasks'):
    pr = {
        'number': number,
        'url': url or f'https://github.com/{repo}/pull/{number}',
        'repo': repo,
    }
    if pr_status is not None:
        pr['status'] = pr_status
    return Item.objects.create(
        item_key=key,
        title=key,
        status=status,
        workflow_name='autonomous-bug',
        workflow_status=status,
        metadata={'pull_requests': [pr]},
    )


class LinkedPrStatusSyncTests(TestCase):
    def test_task_has_worker_starvation_kill_switches(self):
        self.assertEqual(sync_linked_pr_statuses.soft_time_limit, 90)
        self.assertEqual(sync_linked_pr_statuses.time_limit, 120)

    def test_no_token_records_failure_event_and_urgent_devops_task(self):
        _item('NO-TOKEN', status='done', pr_status='open', number=250)

        with mock.patch.dict(
                os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'off'}, clear=True), \
             mock.patch('tasks.tasks.send_alert_notification.delay'):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'no_token')
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.service, 'pulse-linked-pr-sync')
        self.assertEqual(event.state, 'smoke_failed')
        self.assertEqual(event.smoke_result, 'failed')
        self.assertEqual(event.metadata['reason'], 'no_token')
        task = Item.objects.get(source='deployment-event', metadata__deployment_event_id=str(event.id))
        self.assertEqual(task.assignment_group, 'devops')
        self.assertEqual(task.priority, 'urgent')

    def test_github_auth_failure_warns_counts_and_records_failure_artifact(self):
        item = _item('BAD-TOKEN', status='done', pr_status='open', number=251)

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'bad-token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=True), \
             mock.patch('tasks.tasks.requests.get', return_value=_ErrorResponse(401)), \
             mock.patch('tasks.tasks.send_alert_notification.delay'), \
             self.assertLogs('tasks.tasks', level='WARNING') as logs:
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['api_errors'], 1)
        self.assertEqual(result['github_auth_errors'], 1)
        self.assertIn('HTTP 401', '\n'.join(logs.output))
        item.refresh_from_db()
        self.assertEqual(item.metadata['pull_requests'][0]['status'], 'open')
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.state, 'smoke_failed')
        self.assertEqual(event.metadata['reason'], 'github_token_invalid')
        self.assertTrue(Item.objects.filter(source='deployment-event', metadata__deployment_event_id=str(event.id)).exists())

    def test_terminal_item_stale_open_pr_is_backfilled_and_serialized_as_merged(self):
        item = _item('PLS-49', status='done', pr_status=None, number=109)
        self.assertEqual(ItemListSerializer(item).data['pull_requests'][0]['status'], 'open')

        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'token'}), \
             mock.patch('tasks.tasks.requests.get', return_value=_GitHubResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 1)
        get.assert_called_once()

        item.refresh_from_db()
        pr = item.metadata['pull_requests'][0]
        self.assertEqual(pr['status'], 'merged')
        self.assertEqual(pr['merged_at'], '2026-07-01T01:19:49Z')
        self.assertEqual(pr['merged_by'], 'kai2574')
        self.assertEqual(ItemListSerializer(item).data['pull_requests'][0]['status'], 'merged')
        self.assertTrue(ItemActivity.objects.filter(item=item, action='pr_status_changed').exists())
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.state, 'smoke_passed')
        self.assertEqual(event.smoke_result, 'passed')
        self.assertEqual(event.metadata['checked'], 1)
        self.assertEqual(event.metadata['updated'], 1)

    def test_limit_is_api_check_budget_not_candidate_row_slice(self):
        _item('DONE', status='done', pr_status='merged', number=108)
        second = _item('SECOND', status='done', pr_status='open', number=109)
        third = _item('THIRD', status='in_progress', pr_status='open', number=110)
        now = timezone.now()
        Item.objects.filter(pk=second.pk).update(updated_at=now)
        Item.objects.filter(pk=third.pk).update(updated_at=now - timedelta(days=1))

        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'token'}), \
             mock.patch('tasks.tasks.requests.get', return_value=_GitHubResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 1)
        get.assert_called_once()

        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(second.metadata['pull_requests'][0]['status'], 'merged')
        self.assertEqual(third.metadata['pull_requests'][0]['status'], 'open')

    def test_recent_stale_terminal_pr_is_checked_before_historical_backlog(self):
        historical = _item('OLD-DONE', status='done', pr_status='open', number=109)
        recent = _item('PLS-180', status='done', pr_status='open', number=224)
        now = timezone.now()
        Item.objects.filter(pk=historical.pk).update(updated_at=now - timedelta(days=14))
        Item.objects.filter(pk=recent.pk).update(updated_at=now)

        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'token'}), \
             mock.patch('tasks.tasks.requests.get', return_value=_GitHubResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 1)
        self.assertIn('/pulls/224', get.call_args.args[0])

        historical.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(historical.metadata['pull_requests'][0]['status'], 'open')
        self.assertEqual(recent.metadata['pull_requests'][0]['status'], 'merged')

    def test_historical_backlog_gets_reserved_budget_after_recent_pass(self):
        historical = _item('OLD-STUCK', status='done', pr_status='open', number=101)
        recent_a = _item('RECENT-A', status='in_progress', pr_status='open', number=203)
        recent_b = _item('RECENT-B', status='in_progress', pr_status='open', number=204)
        now = timezone.now()
        Item.objects.filter(pk=historical.pk).update(updated_at=now - timedelta(days=90))
        Item.objects.filter(pk=recent_a.pk).update(updated_at=now)
        Item.objects.filter(pk=recent_b.pk).update(updated_at=now - timedelta(minutes=1))

        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'token'}), \
             mock.patch('tasks.tasks.requests.get', return_value=_GitHubResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=2)

        self.assertEqual(result['checked'], 2)
        self.assertEqual(result['updated'], 2)
        checked_urls = [call.args[0] for call in get.call_args_list]
        self.assertTrue(any('/pulls/203' in url for url in checked_urls))
        self.assertTrue(any('/pulls/101' in url for url in checked_urls))
        self.assertFalse(any('/pulls/204' in url for url in checked_urls))

        historical.refresh_from_db()
        recent_a.refresh_from_db()
        recent_b.refresh_from_db()
        self.assertEqual(historical.metadata['pull_requests'][0]['status'], 'merged')
        self.assertEqual(recent_a.metadata['pull_requests'][0]['status'], 'merged')
        self.assertEqual(recent_b.metadata['pull_requests'][0]['status'], 'open')

    def test_stale_open_rotation_sweeps_old_mid_id_item_to_merged(self):
        """PLS-784: an old mid/high-id item with a stale open-stored PR that is
        actually merged upstream is swept by the stale-open rotation instead of
        starving forever behind the recent/low-id fast paths (PLS-600/#118)."""
        # Recent, low-id items that occupy the recent + low-id fast paths.
        now = timezone.now()
        for n in range(3):
            item = _item('PLS-784-RECENT-%d' % n, number=300 + n)
            meta = dict(item.metadata)
            meta['pull_requests'][0]['linked_at'] = now.isoformat()
            Item.objects.filter(pk=item.pk).update(metadata=meta)
        # The stale target: old linked_at + old updated_at, created AFTER the
        # recent items so it is NOT low-id and NOT recently updated.
        target = _item('PLS-784-TARGET', number=118)
        old = timezone.now() - timedelta(days=30)
        meta = dict(target.metadata)
        meta['pull_requests'][0]['linked_at'] = old.isoformat()
        Item.objects.filter(pk=target.pk).update(metadata=meta, updated_at=old)

        def _side_effect(url, **kwargs):
            if '/pulls/118' in url:
                return _GitHubResponse(
                    merged=True, state='closed',
                    merged_at='2026-07-26T00:51:45Z',
                )
            return _GitHubResponse(merged=False, state='open')

        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'token'}), \
             mock.patch('tasks.tasks.requests.get', side_effect=_side_effect) as get:
            result = sync_linked_pr_statuses.run(limit=4)

        target.refresh_from_db()
        pr = target.metadata['pull_requests'][0]
        self.assertEqual(pr['status'], 'merged')
        self.assertEqual(pr['merged_at'], '2026-07-26T00:51:45Z')
        # The stale-open rotation made a provider call for the target.
        self.assertTrue(
            any('/pulls/118' in call.args[0] for call in get.call_args_list),
            'stale target #118 was never provider-checked',
        )
        self.assertEqual(result['updated'], 1)

    def test_wall_clock_budget_returns_partial_progress_before_worker_starvation(self):
        first = _item('BUDGET-FIRST', status='done', pr_status='open', number=301)
        second = _item('BUDGET-SECOND', status='done', pr_status='open', number=302)
        now = timezone.now()
        Item.objects.filter(pk=first.pk).update(updated_at=now)
        Item.objects.filter(pk=second.pk).update(updated_at=now - timedelta(minutes=1))

        # start deadline=75; first item/entry fit, the second-item check expires.
        monotonic = mock.Mock(side_effect=[0, 0, 0, 100])
        # PLS-909: _attempt_merge_event_retry now clears stale per-item retry
        # failures via Redis for out-of-band-advanced items. The Redis client
        # calls time.monotonic() internally for socket timeouts, which would
        # consume the budget mock below — stub the liveness store so the
        # wall-clock budget test measures the sync loop only.
        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=True), \
             mock.patch('tasks.tasks.time.monotonic', monotonic), \
             mock.patch('tasks.liveness.clear_merge_event_retry_failure'), \
             mock.patch('tasks.liveness.record_merge_event_retry_failure'), \
             mock.patch('tasks.tasks.requests.get', return_value=_GitHubResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertTrue(result['budget_exhausted'])
        self.assertEqual(result['checked'], 1)
        get.assert_called_once()
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.metadata['reason'], 'partial_budget')
        self.assertTrue(event.metadata['budget_exhausted'])

    def test_origin_forgejo_pr_is_backfilled_with_origin_token(self):
        item = _item(
            'ORIG-15',
            status='in_progress',
            pr_status='open',
            number=67,
            # Linked metadata keeps the canonical public identity; ORIGIN_URL
            # below selects the trusted in-cluster transport at request time.
            url='https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/67',
            repo='shizuha-labs/shizuha-stack',
        )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'http://10.43.15.105',
        }, clear=True), mock.patch('tasks.tasks.requests.get', return_value=_ForgejoResponse()) as get:
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 1)
        get.assert_called_once()
        api_url = get.call_args.args[0]
        headers = get.call_args.kwargs['headers']
        self.assertEqual(api_url, 'http://10.43.15.105/api/v1/repos/shizuha-labs/shizuha-stack/pulls/67')
        self.assertEqual(headers['Authorization'], 'token forgejo-token')

        item.refresh_from_db()
        pr = item.metadata['pull_requests'][0]
        self.assertEqual(pr['status'], 'merged')
        self.assertEqual(pr['merged_at'], '2026-07-02T10:00:00Z')
        self.assertEqual(pr['merged_by'], 'aoi')
        self.assertTrue(ItemActivity.objects.filter(item=item, action='pr_status_changed').exists())
        attestation = LinkedPullRequestMergeAttestation.objects.get(item=item)
        self.assertEqual(attestation.repo, 'shizuha-labs/shizuha-stack')
        self.assertEqual(attestation.number, 67)
        self.assertEqual(attestation.merge_commit, '4' * 40)

    def test_caller_claimed_merged_metadata_is_not_attested_when_provider_is_open(self):
        item = _item(
            'PLS-521-FORGE',
            status='in_progress',
            pr_status='merged',
            number=68,
            url='https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/68',
            repo='shizuha-labs/shizuha-stack',
        )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'http://10.43.15.105',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get',
            return_value=_ForgejoResponse(merged=False, state='open'),
        ):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        item.refresh_from_db()
        self.assertEqual(item.metadata['pull_requests'][0]['status'], 'open')
        self.assertFalse(
            LinkedPullRequestMergeAttestation.objects.filter(item=item).exists()
        )

    def test_partial_bounce_map_backfills_provider_confirmed_null_entry(self):
        first_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/6'
        second_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/7'
        first_head = '1111111111111111111111111111111111111111'
        second_head = '2222222222222222222222222222222222222222'
        item = Item.objects.create(
            item_key='PLS-684-PARTIAL',
            title='partial bounce provenance',
            status='in_progress',
            workflow_name='autonomous-bug',
            workflow_status='in_progress',
            metadata={
                'pull_requests': [
                    {
                        'number': 7,
                        'url': second_url,
                        'repo': 'shizuha-labs/deploy',
                        'status': 'open',
                        'head_sha': second_head,
                        'linked_at': '2026-07-14T20:48:54Z',
                    },
                    {
                        'number': 6,
                        'url': first_url,
                        'repo': 'shizuha-labs/deploy',
                        'status': 'open',
                        'head_sha': first_head,
                        'linked_at': '2026-07-14T20:47:54Z',
                    },
                ],
                'review_bounce_at': '2026-07-14T20:49:50Z',
                'review_bounce_pr_heads': {
                    first_url: first_head,
                    second_url: None,
                },
            },
        )

        def provider_response(url, **_kwargs):
            head = first_head if url.endswith('/pulls/6') else second_head
            return _ForgejoResponse(
                merged=False, state='open', merged_at=None, head_sha=head,
            )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get', side_effect=provider_response,
        ):
            first = sync_linked_pr_statuses.run(limit=1)
            second = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(first['checked'], 1)
        self.assertEqual(first['updated'], 1)
        self.assertEqual(second['checked'], 1)
        self.assertEqual(second['updated'], 0)
        item.refresh_from_db()
        self.assertEqual(
            item.metadata['review_bounce_pr_heads'],
            {first_url: first_head, second_url: second_head},
        )
        self.assertEqual(
            item.metadata['review_bounce_backfill_source'],
            'sync_linked_pr_statuses',
        )

    def test_partial_bounce_map_does_not_use_unconfirmed_sibling_head(self):
        first_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/6'
        second_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/7'
        first_head = '1111111111111111111111111111111111111111'
        second_head = '2222222222222222222222222222222222222222'
        item = Item.objects.create(
            item_key='PLS-684-BOUNDED',
            title='bounded partial bounce provenance',
            status='in_progress',
            workflow_name='autonomous-bug',
            workflow_status='in_progress',
            metadata={
                'pull_requests': [
                    {
                        'number': 6,
                        'url': first_url,
                        'repo': 'shizuha-labs/deploy',
                        'status': 'open',
                        'head_sha': first_head,
                        'linked_at': '2026-07-14T20:47:54Z',
                    },
                    {
                        'number': 7,
                        'url': second_url,
                        'repo': 'shizuha-labs/deploy',
                        'status': 'open',
                        'head_sha': second_head,
                        'linked_at': '2026-07-14T20:48:54Z',
                    },
                ],
                'review_bounce_at': '2026-07-14T20:49:50Z',
                'review_bounce_pr_heads': {
                    first_url: first_head,
                    second_url: None,
                },
            },
        )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get',
            return_value=_ForgejoResponse(
                merged=False, state='open', merged_at=None, head_sha=first_head,
            ),
        ):
            result = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(result['checked'], 1)
        item.refresh_from_db()
        self.assertIsNone(
            item.metadata['review_bounce_pr_heads'][second_url],
            'a stored sibling head is not trusted until this pass confirms it',
        )

    def test_partial_bounce_map_does_not_backfill_provider_confirmed_closed_pr(self):
        origin_url = (
            'https://origin.shizuha.com/shizuha-labs/deploy/pulls/7'
        )
        current_head = '2222222222222222222222222222222222222222'
        item = Item.objects.create(
            item_key='PLS-684-CLOSED',
            title='closed PR bounce provenance',
            status='in_progress',
            workflow_name='autonomous-bug',
            workflow_status='in_progress',
            metadata={
                'pull_requests': [{
                    'number': 7,
                    'url': origin_url,
                    'repo': 'shizuha-labs/deploy',
                    'status': 'open',
                    'head_sha': 'stale-head',
                    'linked_at': '2026-07-14T20:48:54Z',
                }],
                'review_bounce_at': '2026-07-14T20:49:50Z',
                'review_bounce_pr_heads': {origin_url: None},
            },
        )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get',
            return_value=_ForgejoResponse(
                merged=False, state='closed', merged_at=None,
                head_sha=current_head,
            ),
        ):
            result = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(result['checked'], 1)
        item.refresh_from_db()
        self.assertEqual(item.metadata['pull_requests'][0]['status'], 'closed')
        self.assertIsNone(item.metadata['review_bounce_pr_heads'][origin_url])
        self.assertNotIn('review_bounce_backfilled_at', item.metadata)

    def test_partial_bounce_map_heals_mirror_key_without_duplicate(self):
        mirror_url = 'https://github.com/shizuha-labs/deploy/pull/7'
        origin_url = (
            'https://origin.shizuha.com/shizuha-labs/deploy/pulls/7'
        )
        current_head = '2222222222222222222222222222222222222222'
        item = Item.objects.create(
            item_key='PLS-684-MIRROR-KEY',
            title='mirror-keyed partial bounce provenance',
            status='in_progress',
            workflow_name='autonomous-bug',
            workflow_status='in_progress',
            metadata={
                'pull_requests': [{
                    'number': 7,
                    'url': origin_url,
                    'repo': 'shizuha-labs/deploy',
                    'status': 'open',
                    'head_sha': current_head,
                    'linked_at': '2026-07-14T20:48:54Z',
                }],
                'review_bounce_at': '2026-07-14T20:49:50Z',
                'review_bounce_pr_heads': {mirror_url: None},
            },
        )

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get',
            return_value=_ForgejoResponse(
                merged=False, state='open', merged_at=None,
                head_sha=current_head,
            ),
        ):
            result = sync_linked_pr_statuses.run(limit=1)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['updated'], 1)
        item.refresh_from_db()
        self.assertEqual(
            item.metadata['review_bounce_pr_heads'],
            {origin_url: current_head},
        )
        self.assertNotIn(mirror_url, item.metadata['review_bounce_pr_heads'])

    def test_superseded_close_then_sync_does_not_close_open_successor(self):
        """PLS-607 production order: predecessor closes, successor stays open.

        Both rows live on the same task.  The reconciler must fetch the stored
        terminal-looking successor too; otherwise the false ``closed`` value is
        treated as immutable and the open PR remains mislabelled forever.
        """
        predecessor_url = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/45'
        successor_url = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/47'
        item = Item.objects.create(
            item_key='PLS-607-SEQUENCE',
            title='PLS-607 sequence',
            status='in_progress',
            workflow_name='autonomous-bug',
            workflow_status='in_progress',
            metadata={'pull_requests': [
                {
                    'number': 45,
                    'url': predecessor_url,
                    'repo': 'shizuha-labs/hive',
                    'status': 'open',
                    'head_sha': 'predecessor-head',
                },
                {
                    'number': 47,
                    'url': successor_url,
                    'repo': 'shizuha-labs/hive',
                    'status': 'closed',
                    'head_sha': 'successor-head',
                },
            ]},
        )

        def provider_response(url, **_kwargs):
            if url.endswith('/pulls/45'):
                return _ForgejoResponse(merged=False, state='closed', merged_at=None)
            if url.endswith('/pulls/47'):
                return _ForgejoResponse(merged=False, state='open', merged_at=None)
            raise AssertionError(url)

        with mock.patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'forgejo-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get', side_effect=provider_response,
        ) as get:
            result = sync_linked_pr_statuses.run(limit=2)

        self.assertEqual(result['checked'], 2)
        self.assertEqual(get.call_count, 2)
        item.refresh_from_db()
        by_number = {pr['number']: pr for pr in item.metadata['pull_requests']}
        self.assertEqual(by_number[45]['status'], 'closed')
        self.assertEqual(by_number[47]['status'], 'open')
        self.assertEqual(by_number[47]['head_sha'], 'successor-head')


class LinkedPrApiRequestHostBindingTests(TestCase):
    """PLAT-4675 P1 (kai review): _linked_pr_api_request must send FORGEJO_TOKEN only
    to the TRUSTED, operator-configured ``origin_url`` base — NEVER a host derived
    from the (possibly attacker-supplied) linked PR URL — and must fail closed when
    origin_url is unset instead of falling back to the URL's own host."""

    def _call(self, url, origin_url=''):
        with mock.patch('tasks.tasks.requests.get') as get:
            get.return_value = mock.Mock()
            result = _linked_pr_api_request(
                url, github_token='', forgejo_token='secret-fj', origin_url=origin_url)
            return get, result

    def test_no_origin_url_fails_closed_no_token(self):
        # No configured base -> fail closed; the token is NOT sent to the URL's host.
        get, result = self._call('https://evil.example.com/owner/repo/pulls/1', origin_url='')
        self.assertIsNone(result)
        self.assertFalse(get.called, 'FORGEJO_TOKEN must not be derived from / sent to the URL host')

    def test_hostile_url_is_rejected_before_token_request(self):
        # Canonical coordinates and token destination are independent gates:
        # a trusted origin_url must not make hostile linked metadata acceptable.
        get, result = self._call(
            'https://evil.example.com/owner/repo/pulls/1',
            origin_url='https://origin.shizuha.com')
        self.assertIsNone(result)
        self.assertFalse(get.called)

    def test_query_fragment_and_lookalike_make_zero_requests(self):
        for url in (
            'https://origin.shizuha.com/o/r/pulls/1?x=1',
            'https://origin.shizuha.com/o/r/pulls/1#x',
            'https://origin.shizuha.com.evil.example/o/r/pulls/1',
        ):
            with self.subTest(url=url):
                get, result = self._call(url, origin_url='https://origin.shizuha.com')
                self.assertIsNone(result)
                self.assertFalse(get.called)

    def test_internal_origin_base_used(self):
        # The in-cluster reconciler path (origin_url is the internal Forgejo host).
        get, result = self._call(
            'https://origin.shizuha.com/o/r/pulls/1',
            origin_url='http://forgejo-http.origin.svc.cluster.local')
        self.assertIsNotNone(result)
        call = get.call_args
        api_url = call.args[0] if call.args else call.kwargs.get('url', '')
        self.assertTrue(api_url.startswith('http://forgejo-http.origin.svc.cluster.local/api/v1/'), api_url)

    def test_strict_short_origin_reference_uses_trusted_base(self):
        get, result = self._call(
            'shizuha-labs/shizuha-beta#21',
            origin_url='http://forgejo-http.origin.svc.cluster.local',
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            get.call_args.args[0],
            'http://forgejo-http.origin.svc.cluster.local/api/v1/repos/'
            'shizuha-labs/shizuha-beta/pulls/21',
        )


class _UserResponse:
    """A 200 GET /user response for the auth probe."""
    status_code = 200
    ok = True
    headers = {'X-GitHub-Request-Id': 'probe-req-1'}

    def json(self):
        return {'login': 'sara2574', 'id': 1}


class _AuthProbe401Response(_ErrorResponse):
    headers = {'X-GitHub-Request-Id': 'pr-req-1'}


class LinkedPrSyncGithubAuthProbeTests(TestCase):
    """PLS-979: per-PR 401/403 must not be misreported as an invalid token.

    A dedicated GET /user probe is the only authoritative credential verdict.
    """

    def test_pr_401_with_probe_200_is_not_a_credential_failure(self):
        """Transient per-PR 401 with a valid token probe => smoke_passed, no
        DevOps failure task, api_errors recorded."""
        _item('TRANSIENT-401', status='done', pr_status='open', number=252)

        def _side_effect(url, **kwargs):
            if url == 'https://api.github.com/user':
                return _UserResponse()
            return _AuthProbe401Response(401)

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'valid-token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=True), \
             mock.patch('tasks.tasks.requests.get', side_effect=_side_effect), \
             mock.patch('tasks.tasks.send_alert_notification.delay'):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['api_errors'], 1)
        self.assertEqual(result['github_auth_errors'], 1)
        self.assertEqual(result['github_auth_probe']['status_code'], 200)
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.state, 'smoke_passed')
        self.assertEqual(event.metadata['reason'], 'success')
        self.assertEqual(event.metadata['github_auth_probe']['status_code'], 200)
        # No DevOps failure task is created for a valid token.
        self.assertFalse(
            Item.objects.filter(
                source='deployment-event',
                metadata__deployment_event_id=str(event.id),
            ).exists()
        )

    def test_pr_401_with_probe_401_is_a_credential_failure(self):
        """Per-PR 401 AND a 401 probe => github_token_invalid failure task."""
        _item('REALLY-BAD', status='done', pr_status='open', number=253)

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'bad-token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=True), \
             mock.patch('tasks.tasks.requests.get', return_value=_AuthProbe401Response(401)), \
             mock.patch('tasks.tasks.send_alert_notification.delay'):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['github_auth_errors'], 1)
        self.assertEqual(result['github_auth_probe']['status_code'], 401)
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.state, 'smoke_failed')
        self.assertEqual(event.metadata['reason'], 'github_token_invalid')
        self.assertTrue(
            Item.objects.filter(
                source='deployment-event',
                metadata__deployment_event_id=str(event.id),
            ).exists()
        )

    def test_probe_records_github_request_id(self):
        """Sanitized X-GitHub-Request-Id is captured for diagnosability."""
        _item('REQID', status='done', pr_status='open', number=254)

        with mock.patch.dict(os.environ, {
                'GITHUB_TOKEN': 'bad-token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
                clear=True), \
             mock.patch('tasks.tasks.requests.get', return_value=_AuthProbe401Response(401)), \
             mock.patch('tasks.tasks.send_alert_notification.delay'):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertIn('pr-req-1', result['github_auth_request_ids'])
        event = DeploymentEvent.objects.get(id=result['event_id'])
        self.assertEqual(event.metadata['github_auth_probe']['request_id'], 'pr-req-1')
        self.assertIn('pr-req-1', event.metadata['github_auth_request_ids'])
