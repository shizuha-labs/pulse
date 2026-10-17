"""PLAT-1210: successful rollouts must produce a QA regression watchlist.

PLAT-287 (Books MCP org-context, fixed and done) was re-broken by the
books-42a2fe1 rollout and found by an internal user (BKS-44), not QA, because
nothing re-checked previously-fixed bugs when their service rolled out. These
tests pin the deployment-event → watchlist-task loop.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from tasks.models import (
    DeploymentEvent,
    Item,
    Project,
    QaVerificationAudit,
    QaVerificationDemand,
    QaVerificationOutbox,
)
from tasks.services.deployment_events import upsert_deployment_event as _upsert_deployment_event
from tasks.services.qa_verification_demand import materialize_qa_verification_outbox
from tasks.services.regression_watchlist import (
    ensure_regression_watchlist_task,
    recently_fixed_bugs,
)


class RegressionWatchlistTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='BKS', org_id=1, name='Books')

    def _fixed_bug(self, title, days_ago=3, **overrides):
        kwargs = dict(
            project=self.project,
            mode='task',
            title=title,
            status='done',
            workflow_name='autonomous-bug',
            completed_at=timezone.now() - timedelta(days=days_ago),
        )
        kwargs.update(overrides)
        return Item.objects.create(**kwargs)

    def _payload(self, **overrides):
        payload = {
            'project_key': 'BKS',
            'repo': 'shizuha-labs/books',
            'service': 'books',
            'environment': 'live',
            'commit_sha': '42a2fe1234567890',
            'image_digest': 'sha256:books42a2fe1',
            'pipeline_run_id': 'run-9',
            'state': 'rollout_started',
            'smoke_result': 'unknown',
        }
        payload.update(overrides)
        return payload

    def _upsert(self, payload):
        """Exercise the real ingest path, then the real delayed materializer."""
        result = _upsert_deployment_event(payload)
        event = result[0]
        outbox = (
            QaVerificationOutbox.objects.filter(
                demand__generations__deployment_event=event,
                delivered_at__isnull=True,
            )
            .order_by('-created_at')
            .first()
        )
        if outbox:
            materialize_qa_verification_outbox(outbox.id, force=True)
        return result

    def test_rollout_creates_watchlist_of_recently_fixed_bugs(self):
        """The BKS-44 escape: a rollout must surface PLAT-287-class fixes."""
        bug = self._fixed_bug('Books MCP missing X-Organization-ID')
        old_bug = self._fixed_bug('Ancient fix', days_ago=90)

        event, created, _ = self._upsert(self._payload())

        event.refresh_from_db()
        self.assertTrue(created)
        self.assertIsNotNone(event.qa_task)
        task = event.qa_task
        self.assertEqual(task.assignment_group, 'qa')
        self.assertEqual(task.item_type, 'task.regression_watchlist')
        self.assertIn(bug.item_key or str(bug.pk), task.description)
        self.assertNotIn('Ancient fix', task.description)
        self.assertIn('requires-live-qa', task.tags or '')

    def test_no_recent_fixes_creates_no_task(self):
        event, _, _ = self._upsert(self._payload())
        event.refresh_from_db()
        self.assertIsNone(event.qa_task)
        self.assertFalse(
            Item.objects.filter(item_type='task.regression_watchlist').exists())

    def test_failure_events_do_not_create_watchlist(self):
        self._fixed_bug('Recently fixed')
        event, _, failure_task = self._upsert(
            self._payload(state='smoke_failed', smoke_result='failed'))
        event.refresh_from_db()
        self.assertIsNotNone(failure_task)  # existing failure path unchanged
        self.assertIsNone(event.qa_task)

    def test_idempotent_across_retries_and_state_progression(self):
        self._fixed_bug('Recently fixed')
        self._upsert(self._payload())
        self._upsert(self._payload(state='smoke_passed', smoke_result='passed'))

        self.assertEqual(
            Item.objects.filter(item_type='task.regression_watchlist').count(), 1)
        event = DeploymentEvent.objects.get()
        self.assertEqual(event.state, 'smoke_passed')
        self.assertIsNotNone(event.qa_task)

    def test_retry_keeps_focused_qa_and_watchlist_as_distinct_artifacts(self):
        self._fixed_bug('Recently fixed')
        payload = self._payload(
            state='smoke_passed',
            smoke_result='passed',
            metadata={
                'risk_labels': ['requires-live-qa', 'user-facing'],
                'qa_surfaces': ['/hive/agents', '/hive/subscriptions'],
            },
        )

        event, _, _ = self._upsert(payload)
        event.refresh_from_db()
        focused = event.qa_task
        self.assertEqual(focused.source, 'focused-qa-automation')
        self.assertIn('/hive/agents', focused.description)

        self._upsert(payload)
        event.refresh_from_db()
        focused.refresh_from_db()

        self.assertEqual(event.qa_task_id, focused.id)
        self.assertEqual(focused.source, 'focused-qa-automation')
        self.assertIn('/hive/agents', focused.description)
        self.assertEqual(
            Item.objects.filter(source='focused-qa-automation').count(), 1)
        self.assertEqual(
            Item.objects.filter(item_type='task.regression_watchlist').count(), 1)

    def test_failure_progression_retires_open_watchlist(self):
        """Review P1: rollout_started creates the watchlist, but the SAME
        event progressing to smoke_failed must not leave QA a live-regression
        checklist for a rollout that did not pass."""
        self._fixed_bug('Recently fixed')
        event, _, _ = self._upsert(self._payload(state='rollout_started'))
        event.refresh_from_db()
        watchlist = event.qa_task
        self.assertIsNotNone(watchlist)
        self.assertNotIn(watchlist.status, ('cancelled',))

        event2, created2, failure_task = self._upsert(
            self._payload(state='smoke_failed', smoke_result='failed'))

        self.assertFalse(created2)               # same idempotency key
        self.assertEqual(event2.pk, event.pk)
        self.assertIsNotNone(failure_task)       # DevOps failure task created
        watchlist.refresh_from_db()
        self.assertEqual(watchlist.status, 'cancelled')
        self.assertFalse(
            Item.objects.filter(item_type='task.regression_watchlist')
            .exclude(status='cancelled').exists())

    def test_recovery_after_failure_creates_fresh_watchlist(self):
        """A later successful rollout (new commit/event) gets a new checklist."""
        self._fixed_bug('Recently fixed')
        self._upsert(self._payload(state='rollout_started'))
        self._upsert(self._payload(state='smoke_failed', smoke_result='failed'))
        event, _, _ = self._upsert(
            self._payload(commit_sha='51b2fe0000000000', state='smoke_passed',
                          smoke_result='passed', pipeline_run_id='run-10'))

        event.refresh_from_db()
        self.assertIsNotNone(event.qa_task)
        self.assertNotEqual(event.qa_task.status, 'cancelled')

    def test_other_services_bugs_are_excluded(self):
        other_project = Project.objects.create(key='NTS', org_id=1, name='Notes')
        self._fixed_bug('Books bug')
        Item.objects.create(
            project=other_project, mode='task', title='Notes bug', status='done',
            workflow_name='autonomous-bug',
            completed_at=timezone.now() - timedelta(days=1))

        event, _, _ = self._upsert(self._payload())

        event.refresh_from_db()
        self.assertIn('Books bug', event.qa_task.description)
        self.assertNotIn('Notes bug', event.qa_task.description)

    def test_service_label_match_without_project(self):
        bug = self._fixed_bug('Labeled bug', project=None, labels={'service': 'books'})
        event = DeploymentEvent.objects.create(
            repo='shizuha-labs/books', service='books', environment='live',
            cluster='shizuha-k3s', commit_sha='deadbeef1234', state='rollout_started')

        self.assertIn(bug, list(recently_fixed_bugs(event)))
        task = ensure_regression_watchlist_task(event)
        self.assertIsNotNone(task)
        self.assertIn('Labeled bug', task.description)

    def test_array_service_label_match_without_project(self):
        """PLS-712: the dominant flat-label shape reaches the watchlist."""
        bug = self._fixed_bug(
            'Array-labeled fixed bug',
            project=None,
            labels=['service:books'],
        )
        event = DeploymentEvent.objects.create(
            repo='shizuha-labs/books', service='books', environment='live',
            cluster='shizuha-k3s', commit_sha='feedface1234', state='rollout_started')

        self.assertIn(bug, list(recently_fixed_bugs(event)))

    def test_rapid_deploys_settle_latest_wins_with_reverse_delivery_and_retry(self):
        """Real production order: ingest A/B, deliver B/A/A, never roll back."""
        self._fixed_bug('Recently fixed')
        event_a, created_a, _ = _upsert_deployment_event(
            self._payload(
                commit_sha='aaaaaaaaaaaaaaaa',
                image_digest='sha256:a',
                pipeline_run_id='run-a',
            )
        )
        event_b, created_b, _ = _upsert_deployment_event(
            self._payload(
                commit_sha='bbbbbbbbbbbbbbbb',
                image_digest='sha256:b',
                pipeline_run_id='run-b',
            )
        )
        self.assertTrue(created_a)
        self.assertTrue(created_b)
        demand = QaVerificationDemand.objects.get()
        rows = list(demand.outbox.order_by('version'))
        self.assertEqual(len(rows), 2)

        newest = materialize_qa_verification_outbox(rows[1].id, force=True)
        stale = materialize_qa_verification_outbox(rows[0].id, force=True)
        stale_retry = materialize_qa_verification_outbox(rows[0].id, force=True)

        self.assertEqual(newest['status'], 'materialized')
        self.assertEqual(stale['status'], 'superseded')
        self.assertEqual(stale_retry['status'], 'delivered')
        demand.refresh_from_db()
        task = demand.active_item
        self.assertIsNotNone(task)
        self.assertEqual(task.metadata['deployment_event_id'], str(event_b.id))
        self.assertEqual(task.metadata['commit_sha'], 'bbbbbbbbbbbbbbbb')
        self.assertEqual(
            Item.objects.filter(item_type='task.regression_watchlist')
            .exclude(status__in=('cancelled', 'done', 'closed')).count(),
            1,
        )
        audit = QaVerificationAudit.objects.get(effect_kind='generation_superseded')
        self.assertEqual(audit.from_generation, 1)
        self.assertEqual(audit.to_generation, 2)
        self.assertEqual(audit.payload['superseded_commit_sha'], 'aaaaaaaaaaaaaaaa')
        self.assertEqual(audit.payload['current_commit_sha'], 'bbbbbbbbbbbbbbbb')

    def test_superseded_failure_cannot_cancel_newest_generation(self):
        self._fixed_bug('Recently fixed')
        event_a, _, _ = self._upsert(self._payload(
            commit_sha='aaaaaaaaaaaaaaaa', pipeline_run_id='run-a'))
        event_b, _, _ = self._upsert(self._payload(
            commit_sha='bbbbbbbbbbbbbbbb', pipeline_run_id='run-b'))
        demand = QaVerificationDemand.objects.get()
        task = demand.active_item

        _upsert_deployment_event(self._payload(
            commit_sha='aaaaaaaaaaaaaaaa',
            pipeline_run_id='run-a-retry',
            state='smoke_failed',
            smoke_result='failed',
        ))

        event_a.refresh_from_db()
        event_b.refresh_from_db()
        task.refresh_from_db()
        demand.refresh_from_db()
        self.assertTrue(event_a.is_failure)
        self.assertEqual(demand.active_item_id, task.id)
        self.assertNotEqual(task.status, 'cancelled')
        self.assertEqual(task.metadata['deployment_event_id'], str(event_b.id))

    def test_exact_org_surfaces_do_not_share_a_demand(self):
        self._fixed_bug('Org one fix')
        project_two = Project.objects.create(key='BKS', org_id=2, name='Books Two')
        Item.objects.create(
            project=project_two,
            mode='task',
            title='Org two fix',
            status='done',
            workflow_name='autonomous-bug',
            completed_at=timezone.now(),
        )
        _upsert_deployment_event(self._payload(
            commit_sha='orgone1111111111', pipeline_run_id='org-1'))
        _upsert_deployment_event(self._payload(
            project=project_two,
            project_key=None,
            commit_sha='orgtwo2222222222',
            pipeline_run_id='org-2',
        ))

        self.assertEqual(QaVerificationDemand.objects.count(), 2)
        self.assertEqual(
            set(QaVerificationDemand.objects.values_list('organization_id', flat=True)),
            {1, 2},
        )
