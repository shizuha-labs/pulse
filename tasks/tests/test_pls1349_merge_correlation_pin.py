"""PLS-1349 regression pin: merge-time DeploymentEvent correlation (PLS-1303 leg 4).

Pins the ``reingest_events_for_merge`` correlation scheduler contract at three
cases, per the PLS-1303 acceptance leg 4 pinned disposition (the evidence-
integrity surface must not ship mechanism-only):

1. Already-correlated skip — an event whose ``source_task_keys`` already names
   the task is skipped with no second event minted / no upsert replay
   (idempotency surface).
2. No-event no-op — no correlatable event for the merge commit means the
   scheduler no-ops cleanly and fail-closed; it never invents an event.
3. State preservation on refresh — the pure correlation refresh rebuilds the
   payload from the FULL stored row so state/smoke/producer identity survive;
   the only intentional delta is ``source_task_keys``.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import DeploymentEvent
from tasks.services.deployment_events import reingest_events_for_merge

REPO = 'shizuha-labs/notes'
SHA = 'a' * 40
TASK_KEY = 'PLS-1303'
OTHER_KEY = 'PLS-9XX'


class MergeCorrelationPinTests(TestCase):
    def _seed_event(self, *, source_task_keys, **overrides):
        fields = dict(
            idempotency_key=f'{REPO}:{SHA}:smoke_passed:notes:1',
            repo=REPO,
            commit_sha=SHA,
            service='notes',
            state='smoke_passed',
            smoke_result='passed',
            environment='live',
            cluster='shizuha-k3s',
            branch='main',
            pipeline_system='origin-forgejo',
            pipeline_run_id='42',
            source_task_keys=source_task_keys,
        )
        fields.update(overrides)
        return DeploymentEvent.objects.create(**fields)

    def test_already_correlated_event_is_skipped_without_replay(self):
        """Case 1: a live correlation naming the task is left untouched."""
        event = self._seed_event(source_task_keys=[TASK_KEY])

        with mock.patch(
            'tasks.services.deployment_events.upsert_deployment_event'
        ) as upsert:
            reingested = reingest_events_for_merge(
                repo=REPO, merge_commit_sha=SHA, item_key=TASK_KEY,
            )

        self.assertEqual(reingested, 0)
        upsert.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.source_task_keys, [TASK_KEY])
        # Idempotency surface: exactly one ledger row, no second event minted.
        self.assertEqual(
            DeploymentEvent.objects.filter(repo__iexact=REPO,
                                           commit_sha__iexact=SHA).count(), 1,
        )

    def test_no_event_noop_is_fail_closed(self):
        """Case 2: no correlatable event → clean no-op, nothing invented."""
        with mock.patch(
            'tasks.services.deployment_events.upsert_deployment_event'
        ) as upsert:
            reingested = reingest_events_for_merge(
                repo=REPO, merge_commit_sha=SHA, item_key=TASK_KEY,
            )

        self.assertEqual(reingested, 0)
        upsert.assert_not_called()
        self.assertEqual(DeploymentEvent.objects.count(), 0)

    def test_refresh_preserves_state_and_producer_identity(self):
        """Case 3: the refresh's only intentional delta is source_task_keys."""
        event = self._seed_event(source_task_keys=[OTHER_KEY])

        # No items carry pull_requests metadata in this test, so the linked-PR
        # sweep matches nothing; the provider fallback is mocked to its
        # fail-closed empty result (unreachable provider correlates nothing).
        with mock.patch(
            'tasks.services.deployment_events._provider_correlated_task_keys',
            return_value=set(),
        ):
            reingested = reingest_events_for_merge(
                repo=REPO, merge_commit_sha=SHA, item_key=TASK_KEY,
            )

        self.assertEqual(reingested, 1)
        event.refresh_from_db()
        self.assertEqual(
            sorted(event.source_task_keys), sorted([OTHER_KEY, TASK_KEY]),
        )
        # Truthful producer identity survived the full-row payload rebuild.
        self.assertEqual(event.state, 'smoke_passed')
        self.assertEqual(event.smoke_result, 'passed')
        self.assertEqual(event.service, 'notes')
        self.assertEqual(event.environment, 'live')
        self.assertEqual(event.pipeline_run_id, '42')
        self.assertEqual(event.pipeline_system, 'origin-forgejo')
        self.assertEqual(event.branch, 'main')
        # Still exactly one event: same idempotency key → update, not create.
        self.assertEqual(
            DeploymentEvent.objects.filter(repo__iexact=REPO,
                                           commit_sha__iexact=SHA).count(), 1,
        )
