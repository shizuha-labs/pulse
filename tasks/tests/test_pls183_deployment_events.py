"""PLS-183 deployment-event ledger and failed-smoke automation."""
import os
from unittest import mock

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import DeploymentEvent, Item, Project
from tasks.services import deployment_events as deployment_events_module
from tasks.services.deployment_events import upsert_deployment_event
from tasks.views import DeploymentEventViewSet


class _U:
    id = 1
    pk = 1
    email = 'ci@example.com'
    is_authenticated = True
    is_staff = True
    is_superuser = True


class DeploymentEventIdentityTests(SimpleTestCase):
    def test_idempotency_key_is_stable_for_same_origin_manifest_retry(self):
        a = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/notes', service='notes', environment='live', cluster='shizuha-k3s',
            commit_sha='abcdef123', image_digest='sha256:1', pipeline_run_id='42',
            manifest_ref='origin/shizuha-labs/deploy@abc123', pipeline_system='origin-forgejo',
        )
        b = DeploymentEvent.build_idempotency_key(
            repo=' SHIZUHA-LABS/NOTES ', service=' Notes ', environment='LIVE', cluster='SHIZUHA-K3S',
            commit_sha='ABCDEF123', image_digest='sha256:2', image_tag='retry', pipeline_run_id='43',
            manifest_ref=' ORIGIN/SHIZUHA-LABS/DEPLOY@ABC123 ', pipeline_system=' Origin-Forgejo ',
        )
        self.assertEqual(a, b)

    def test_idempotency_key_changes_for_new_origin_manifest_ref(self):
        app_commit = 'abcdef123'
        a = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/notes', service='notes', environment='live', cluster='shizuha-k3s',
            commit_sha=app_commit, manifest_ref='origin/deploy@manifest-1',
            pipeline_system='origin-forgejo',
        )
        b = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/notes', service='notes', environment='live', cluster='shizuha-k3s',
            commit_sha=app_commit, manifest_ref='origin/deploy@manifest-2',
            pipeline_system='origin-forgejo',
        )
        self.assertNotEqual(a, b)

    def test_idempotency_key_changes_for_pipeline_system(self):
        a = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/notes', service='notes', environment='live', cluster='shizuha-k3s',
            commit_sha='abcdef123', manifest_ref='origin/deploy@manifest-1',
            pipeline_system='origin-forgejo',
        )
        b = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/notes', service='notes', environment='live', cluster='shizuha-k3s',
            commit_sha='abcdef123', manifest_ref='origin/deploy@manifest-1',
            pipeline_system='github-actions',
        )
        self.assertNotEqual(a, b)


class DeploymentEventServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='PLS', org_id=1, name='Pulse')

    def _payload(self, **overrides):
        payload = {
            'project_key': 'PLS',
            'repo': 'shizuha-labs/notes',
            'service': 'notes',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': 'abcdef1234567890',
            'image_digest': 'sha256:deadbeef',
            'manifest_ref': 'origin/shizuha-labs/deploy@manifest-1',
            'pipeline_system': 'origin-forgejo',
            'pipeline_run_id': 'run-1',
            'pipeline_url': 'https://origin.shizuha/shizuha-labs/notes/actions/runs/1',
            'logs_url': 'https://origin.shizuha/shizuha-labs/notes/actions/runs/1/logs',
            'state': 'smoke_failed',
            'smoke_result': 'failed',
        }
        payload.update(overrides)
        return payload

    def test_failed_smoke_creates_one_urgent_devops_task_and_retries_update_it(self):
        event, created, task = upsert_deployment_event(self._payload())
        self.assertTrue(created)
        self.assertIsNotNone(task)
        self.assertEqual(task.assignment_group, 'devops')
        self.assertEqual(task.priority, 'urgent')
        self.assertEqual(task.metadata['deployment_event_id'], str(event.id))
        observation = task.metadata['origin_observations'][0]
        self.assertEqual(observation['repo'], 'shizuha-labs/notes')
        self.assertEqual(observation['commit_sha'], 'abcdef1234567890')
        self.assertEqual(observation['run_id'], 'run-1')
        self.assertEqual(observation['check'], 'deployment-smoke')
        self.assertEqual(Item.objects.filter(source='deployment-event').count(), 1)

        event2, created2, task2 = upsert_deployment_event(self._payload(
            image_digest='sha256:retry',
            pipeline_run_id='run-2',
            logs_url='https://origin.shizuha/new-logs',
            links={'github_mirror_ref': 'github/shizuha-labs/deploy@mirror-retry'},
        ))
        self.assertFalse(created2)
        self.assertEqual(event2.pk, event.pk)
        self.assertEqual(event2.pipeline_run_id, 'run-2')
        self.assertEqual(event2.image_digest, 'sha256:retry')
        self.assertEqual(task2.pk, task.pk)
        self.assertEqual(task2.metadata['origin_observations'][0]['run_id'], 'run-2')
        self.assertEqual(len(task2.metadata['origin_observations']), 2)
        self.assertEqual(Item.objects.filter(source='deployment-event').count(), 1)


    def test_new_origin_manifest_ref_creates_distinct_event_for_same_app_commit(self):
        event, created, task = upsert_deployment_event(self._payload())
        self.assertTrue(created)

        event2, created2, task2 = upsert_deployment_event(self._payload(
            manifest_ref='origin/shizuha-labs/deploy@manifest-2',
            pipeline_run_id='run-2',
            logs_url='https://origin.shizuha/manifest-2-logs',
        ))

        self.assertTrue(created2)
        self.assertNotEqual(event2.pk, event.pk)
        self.assertNotEqual(event2.idempotency_key, event.idempotency_key)
        self.assertEqual(DeploymentEvent.objects.count(), 2)
        self.assertEqual(Item.objects.filter(source='deployment-event').count(), 2)
        self.assertNotEqual(task2.pk, task.pk)

    def test_github_mirror_ref_is_secondary_metadata_not_identity(self):
        event, created, task = upsert_deployment_event(self._payload(
            links={'github_mirror_ref': 'github/shizuha-labs/deploy@mirror-1'},
        ))
        self.assertTrue(created)

        event2, created2, task2 = upsert_deployment_event(self._payload(
            pipeline_run_id='run-2',
            links={'github_mirror_ref': 'github/shizuha-labs/deploy@mirror-2'},
        ))

        self.assertFalse(created2)
        self.assertEqual(event2.pk, event.pk)
        self.assertEqual(task2.pk, task.pk)
        self.assertEqual(event2.links['github_mirror_ref'], 'github/shizuha-labs/deploy@mirror-2')
        self.assertEqual(DeploymentEvent.objects.count(), 1)
        self.assertEqual(Item.objects.filter(source='deployment-event').count(), 1)

    def test_successful_deployment_stays_ledger_only(self):
        event, created, task = upsert_deployment_event(self._payload(state='smoke_passed', smoke_result='passed'))
        self.assertTrue(created)
        self.assertIsNone(task)
        self.assertEqual(event.state, 'smoke_passed')
        self.assertFalse(Item.objects.filter(source='deployment-event').exists())

    def test_ingest_derives_all_linked_tasks_from_exact_merge_commit_and_pr(self):
        head_sha = 'a' * 40
        merge_sha = 'b' * 40

        def linked_item(key, *, repo='shizuha-labs/notes', number=44):
            return Item.objects.create(
                project=self.project,
                item_key=key,
                title=f'Acceptance for {key}',
                description='Live acceptance follows the linked deployment.',
                item_type='task',
                status='awaiting_deploy',
                workflow_name='autonomous-bug',
                created_by_id=1,
                created_by_email='author@shizuha.com',
                metadata={'pull_requests': [{
                    'url': f'https://origin.shizuha.com/{repo}/pulls/{number}',
                    'repo': repo,
                    'number': number,
                    'status': 'merged',
                    'merged_at': '2026-08-09T00:00:00Z',
                    'head_sha': head_sha,
                    'merge_commit_sha': merge_sha,
                }]},
            )

        linked_item('PLS-900')
        linked_item('PLS-901')  # Follow-up task need not be named in PR prose.
        linked_item('PLS-902', repo='other-org/notes')
        linked_item('PLS-903', number=45)

        event, created, task = upsert_deployment_event(self._payload(
            state='smoke_passed',
            smoke_result='passed',
            commit_sha=merge_sha,
            pr_numbers=[44],
            source_task_keys=['PLS-MANUAL'],
        ))

        self.assertTrue(created)
        self.assertIsNone(task)
        self.assertEqual(
            event.source_task_keys,
            ['PLS-900', 'PLS-901', 'PLS-MANUAL'],
        )
        self.assertEqual(event.pr_numbers, [44])

    def test_ingest_does_not_correlate_unrelated_commit(self):
        Item.objects.create(
            project=self.project,
            item_key='PLS-904',
            title='Unrelated deploy',
            description='Negative control.',
            item_type='task',
            status='awaiting_deploy',
            workflow_name='autonomous-bug',
            created_by_id=1,
            created_by_email='author@shizuha.com',
            metadata={'pull_requests': [{
                'url': 'https://origin.shizuha.com/shizuha-labs/notes/pulls/44',
                'repo': 'shizuha-labs/notes',
                'number': 44,
                'status': 'merged',
                'merged_at': '2026-08-09T00:00:00Z',
                'head_sha': 'a' * 40,
                'merge_commit_sha': 'b' * 40,
            }]},
        )
        event, _, _ = upsert_deployment_event(self._payload(
            state='smoke_passed',
            smoke_result='passed',
            commit_sha='c' * 40,
            pr_numbers=[44],
        ))
        self.assertEqual(event.source_task_keys, [])

    def test_api_ingest_upserts_and_recent_query_lists_event(self):
        factory = APIRequestFactory()
        view = DeploymentEventViewSet.as_view({'post': 'create', 'get': 'list'})
        req = factory.post('/deployment-events/', self._payload(), format='json')
        force_authenticate(req, user=_U())
        resp = view(req)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['devops_task_key'], Item.objects.get(source='deployment-event').item_key)

        req = factory.get('/deployment-events/', {'service': 'notes', 'failure_only': 'true'})
        force_authenticate(req, user=_U())
        resp = view(req)
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get('results', resp.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['service'], 'notes')

    # ── PLS-961 provider-bound merge-identity correlation (PR-sync ordering race) ──

    def _provider_env(self):
        return mock.patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'ci-test-token', 'ORIGIN_URL': 'https://origin.shizuha'},
        )

    @staticmethod
    def _fake_response(payload):
        return mock.Mock(
            status_code=200,
            json=mock.Mock(return_value=payload),
        )

    def _open_pr_item(self, key, *, number=44, repo='shizuha-labs/notes', url=None):
        """Item whose linked PR still reads ``open`` (sweep has not run yet)."""
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=f'Acceptance for {key}',
            description='Live acceptance follows the linked deployment.',
            item_type='task',
            status='awaiting_deploy',
            workflow_name='autonomous-bug',
            created_by_id=1,
            created_by_email='author@shizuha.com',
            metadata={'pull_requests': [{
                'url': url or f'https://origin.shizuha.com/{repo}/pulls/{number}',
                'repo': repo,
                'number': number,
                'status': 'open',
            }]},
        )

    def _forgejo_pulls_payload(self, merge_sha, *, number=44, head_sha=None):
        return [{
            'id': 1000 + number,
            'number': number,
            'state': 'merged',
            'merged': True,
            'merged_at': '2026-08-12T00:00:00Z',
            'merge_commit_sha': merge_sha,
            'head': {'sha': head_sha or ('d' * 40)},
        }]

    def test_ingest_correlates_merged_pr_before_pr_sync_sweep(self):
        """Production-order regression: linked PR open → provider merge occurs →
        DeploymentEvent ingest arrives before the periodic PR-sync sweep → the event
        still correlates the correct task by exact provider-bound repo/PR/merge SHA."""
        merge_sha = 'b' * 40
        self._open_pr_item('PLS-961A')

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                return_value=self._fake_response(self._forgejo_pulls_payload(merge_sha)),
            ) as mocked_get:
                event, created, task = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha=merge_sha,
                    pr_numbers=[0],  # deploy-lane recorder has no Forgejo token
                ))

        self.assertTrue(created)
        self.assertIsNone(task)
        self.assertEqual(event.source_task_keys, ['PLS-961A'])
        called_url = mocked_get.call_args[0][0]
        self.assertIn('shizuha-labs/notes/commits/', called_url)
        self.assertIn(merge_sha, called_url)
        # The linked-PR tuple was backfilled with the authoritative merge identity
        # so the next ingest correlates through the metadata pass (no round-trip).
        item = Item.objects.get(item_key='PLS-961A')
        pr = (item.metadata or {})['pull_requests'][0]
        self.assertEqual(pr['status'], 'merged')
        self.assertEqual(pr['merge_commit_sha'], merge_sha)

    def test_ingest_correlates_followup_task_linking_same_pr(self):
        """Both the source task and a follow-up verification task linking the same
        merged PR correlate from one provider merge identity (Drive-style DRV-98 /
        DRV-107 pairing)."""
        merge_sha = 'b' * 40
        self._open_pr_item('PLS-961A')
        self._open_pr_item('PLS-961B')  # carrier task links the same PR

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                return_value=self._fake_response(self._forgejo_pulls_payload(merge_sha)),
            ):
                event, _, _ = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha=merge_sha,
                    pr_numbers=[0],
                ))

        self.assertEqual(
            event.source_task_keys,
            ['PLS-961A', 'PLS-961B'],
        )

    def test_ingest_fail_closed_when_provider_has_no_merged_pr_for_commit(self):
        """Provider reports no associated PR for this commit → no correlation; the
        linked-PR tuple is left untouched (still open)."""
        self._open_pr_item('PLS-961C')

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                return_value=self._fake_response([]),
            ):
                event, _, _ = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha='c' * 40,
                    pr_numbers=[0],
                ))

        self.assertEqual(event.source_task_keys, [])
        item = Item.objects.get(item_key='PLS-961C')
        self.assertEqual((item.metadata or {})['pull_requests'][0]['status'], 'open')

    def test_ingest_fail_closed_when_provider_merge_sha_mismatch(self):
        """A merged PR whose provider merge_commit_sha differs from the event commit
        never correlates — an incidental/injected SHA is not proof of correlation."""
        self._open_pr_item('PLS-961D')

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                return_value=self._fake_response(
                    self._forgejo_pulls_payload('f' * 40),
                ),
            ):
                event, _, _ = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha='c' * 40,
                    pr_numbers=[0],
                ))

        self.assertEqual(event.source_task_keys, [])

    def test_ingest_provider_lookup_failure_never_fails_ingestion(self):
        """A provider transport/error response leaves ingestion alive and fail-closed."""
        self._open_pr_item('PLS-961E')

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                side_effect=deployment_events_module.requests.RequestException('boom'),
            ):
                event, created, _ = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha='c' * 40,
                ))

        self.assertTrue(created)
        self.assertEqual(event.source_task_keys, [])

    def test_ingest_provider_reconciliation_is_idempotent(self):
        """Re-ingesting the same event (same idempotency) does not duplicate work:
        one event row, one correlation, and the second ingest correlates through the
        backfilled metadata pass without another provider round-trip."""
        merge_sha = 'b' * 40
        self._open_pr_item('PLS-961F')
        payload = dict(self._payload(
            state='smoke_passed',
            smoke_result='passed',
            commit_sha=merge_sha,
            pr_numbers=[0],
        ))

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                return_value=self._fake_response(self._forgejo_pulls_payload(merge_sha)),
            ) as mocked_get:
                event_1, created_1, _ = upsert_deployment_event(payload)
                event_2, created_2, _ = upsert_deployment_event(payload)

        self.assertTrue(created_1)
        self.assertFalse(created_2)
        self.assertEqual(event_2.pk, event_1.pk)
        self.assertEqual(event_1.source_task_keys, ['PLS-961F'])
        self.assertEqual(event_2.source_task_keys, ['PLS-961F'])
        self.assertEqual(DeploymentEvent.objects.count(), 1)
        # Only the FIRST ingest needed the provider; the backfilled metadata serves
        # the replay.
        self.assertEqual(mocked_get.call_count, 1)

    def test_ingest_correlates_from_metadata_without_provider_lookup(self):
        """When the linked PR already reads merged, correlation is served purely from
        Pulse state — the provider round-trip must not run on every deploy."""
        head_sha = 'a' * 40
        merge_sha = 'b' * 40
        Item.objects.create(
            project=self.project,
            item_key='PLS-961G',
            title='Already attested',
            description='Sweep already populated the merge identity.',
            item_type='task',
            status='awaiting_deploy',
            workflow_name='autonomous-bug',
            created_by_id=1,
            created_by_email='author@shizuha.com',
            metadata={'pull_requests': [{
                'url': 'https://origin.shizuha.com/shizuha-labs/notes/pulls/44',
                'repo': 'shizuha-labs/notes',
                'number': 44,
                'status': 'merged',
                'merged_at': '2026-08-12T00:00:00Z',
                'head_sha': head_sha,
                'merge_commit_sha': merge_sha,
            }]},
        )

        def _must_not_call(*args, **kwargs):
            raise AssertionError('provider lookup must not run on attested deploys')

        with self._provider_env():
            with mock.patch.object(
                deployment_events_module.requests, 'get',
                side_effect=_must_not_call,
            ):
                event, _, _ = upsert_deployment_event(self._payload(
                    state='smoke_passed',
                    smoke_result='passed',
                    commit_sha=merge_sha,
                    pr_numbers=[0],
                ))

        self.assertEqual(event.source_task_keys, ['PLS-961G'])


class DeploymentEventIdentityMutationTests(TestCase):
    """PLS-906: commit/image identity is immutable after creation.

    Production-order regression: ingest a deploy at SHA A / image A, then a
    QA/state update carrying SHA B must NOT rewrite commit_sha on the same row
    (which would leave commit B paired with image A). The update must be
    rejected; a genuinely different commit/image belongs to a new event via the
    idempotent ingest path.
    """

    def setUp(self):
        self.project = Project.objects.create(key='PLS', org_id=1, name='Pulse')

    def _payload(self, **overrides):
        payload = {
            'project_key': 'PLS',
            'repo': 'shizuha-labs/notes',
            'service': 'notes',
            'environment': 'live',
            'cluster': 'shizuha-k3s',
            'commit_sha': 'a' * 40,
            'image': 'localhost:30500/notes:notes-aaaaaaaaaaa',
            'image_tag': 'notes-aaaaaaaaaaa',
            'image_digest': 'sha256:' + '1' * 64,
            'manifest_ref': 'origin/shizuha-labs/deploy@manifest-1',
            'pipeline_system': 'origin-forgejo',
            'pipeline_run_id': 'run-1',
            'state': 'rollout_started',
        }
        payload.update(overrides)
        return payload

    def test_qa_state_update_cannot_rewrite_commit_sha_on_existing_row(self):
        # Ingest deploy at SHA A / image A.
        event, created, _ = upsert_deployment_event(self._payload())
        self.assertTrue(created)
        self.assertEqual(event.commit_sha, 'a' * 40)

        # QA/state update carries SHA B but no image change -> must be rejected,
        # never one row with commit B / image A.
        factory = APIRequestFactory()
        view = DeploymentEventViewSet.as_view({'patch': 'partial_update'})
        req = factory.patch(
            f'/deployment-events/{event.id}/',
            {'commit_sha': 'b' * 40, 'state': 'verified_by_qa'},
            format='json',
        )
        force_authenticate(req, user=_U())
        resp = view(req, pk=event.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('commit_sha', resp.data)

        event.refresh_from_db()
        self.assertEqual(event.commit_sha, 'a' * 40)
        self.assertEqual(event.state, 'rollout_started')

    def test_qa_state_update_cannot_rewrite_image_identity(self):
        event, created, _ = upsert_deployment_event(self._payload())
        self.assertTrue(created)

        factory = APIRequestFactory()
        view = DeploymentEventViewSet.as_view({'patch': 'partial_update'})
        req = factory.patch(
            f'/deployment-events/{event.id}/',
            {'image_digest': 'sha256:' + '2' * 64, 'state': 'verified_by_qa'},
            format='json',
        )
        force_authenticate(req, user=_U())
        resp = view(req, pk=event.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('image_digest', resp.data)

        event.refresh_from_db()
        self.assertEqual(event.image_digest, 'sha256:' + '1' * 64)

    def test_qa_state_update_may_advance_state_without_touching_identity(self):
        event, created, _ = upsert_deployment_event(self._payload())
        self.assertTrue(created)

        factory = APIRequestFactory()
        view = DeploymentEventViewSet.as_view({'patch': 'partial_update'})
        req = factory.patch(
            f'/deployment-events/{event.id}/',
            {'state': 'verified_by_qa', 'smoke_result': 'passed',
             'links': {'evidence': 'https://origin.shizuha/evidence/1'}},
            format='json',
        )
        force_authenticate(req, user=_U())
        resp = view(req, pk=event.id)
        self.assertEqual(resp.status_code, 200)

        event.refresh_from_db()
        self.assertEqual(event.state, 'verified_by_qa')
        self.assertEqual(event.smoke_result, 'passed')
        self.assertEqual(event.commit_sha, 'a' * 40)
        self.assertEqual(event.image_digest, 'sha256:' + '1' * 64)

    def test_put_with_mismatched_commit_sha_is_rejected(self):
        event, created, _ = upsert_deployment_event(self._payload())
        self.assertTrue(created)

        factory = APIRequestFactory()
        view = DeploymentEventViewSet.as_view({'put': 'update'})
        req = factory.put(
            f'/deployment-events/{event.id}/',
            self._payload(commit_sha='c' * 40),
            format='json',
        )
        force_authenticate(req, user=_U())
        resp = view(req, pk=event.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('commit_sha', resp.data)

        event.refresh_from_db()
        self.assertEqual(event.commit_sha, 'a' * 40)
