"""Alertmanager must not create Pulse tasks/items by default (DM-only)."""
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings


class _SharedRedisState:
    def __init__(self):
        self.values = {}
        self.lock = Lock()


class _DistinctRedisClient:
    """One client/process view backed by one shared atomic Redis state."""

    def __init__(self, state):
        self.state = state

    def set(self, key, value, *, nx=False, ex=None):
        del ex
        with self.state.lock:
            if nx and key in self.state.values:
                return False
            self.state.values[key] = value
            return True

    def get(self, key):
        with self.state.lock:
            return self.state.values.get(key)

    def delete(self, key):
        with self.state.lock:
            return int(self.state.values.pop(key, None) is not None)

    def expire(self, key, ttl):
        del ttl
        with self.state.lock:
            return int(key in self.state.values)

    def eval(self, script, numkeys, key, token):
        del script, numkeys
        with self.state.lock:
            if self.state.values.get(key) != token:
                return 0
            del self.state.values[key]
            return 1


class AlertmanagerDmOnlyTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.redis_state = _SharedRedisState()
        self.redis_clients = []

        def distinct_client():
            client = _DistinctRedisClient(self.redis_state)
            self.redis_clients.append(client)
            return client

        patcher = patch(
            'tasks.tasks._alert_dm_redis_client',
            side_effect=distinct_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_process_alertmanager_does_not_submit_alert_when_persist_off(self):
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1,
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
            record_usage=MagicMock(),
        )
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.services.deduplication.AlertDeduplicationService.submit_alert') as submit, \
             patch('tasks.tasks._dm_alertmanager_firing', return_value='notified') as dm:
            result = process_alertmanager_alerts.run(
                1,
                [{
                    'status': 'firing',
                    'labels': {'alertname': 'KubeJobFailed', 'severity': 'warning'},
                    'annotations': {'summary': 'job failed'},
                    'fingerprint': 'abc123',
                    'generatorURL': 'http://prom/graph',
                }],
                payload_status='firing',
            )
        submit.assert_not_called()
        dm.assert_called_once()
        self.assertEqual(result.get('created'), 0)
        self.assertEqual(result.get('dm'), 1)
        self.assertFalse(result.get('persist'))

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False)
    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False)
    def test_diagnostic_firing_and_resolved_batch_emit_no_action_dm(self):
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1, source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
            default_project_id=None,
            record_usage=MagicMock(),
        )
        labels = {
            'alertname': 'VllmGenerationStarvationFast',
            'severity': 'warning',
            'response_class': 'diagnostic',
            'mutation_authority': 'cortex_controller',
        }
        alerts = [
            {
                'status': 'firing', 'labels': labels,
                'annotations': {'summary': 'diagnostic'},
                'fingerprint': 'diagnostic-firing',
            },
            {
                'status': 'resolved', 'labels': labels,
                'annotations': {'summary': 'diagnostic resolved'},
                'fingerprint': 'diagnostic-resolved',
                'startsAt': '2026-08-09T00:00:00Z',
                'endsAt': '2026-08-09T00:05:00Z',
            },
        ]
        empty_qs = MagicMock()
        empty_qs.filter.return_value = empty_qs
        empty_qs.__iter__.return_value = iter(())
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.models.Item.objects.filter', return_value=empty_qs), \
             patch('tasks.services.deduplication.AlertDeduplicationService.submit_alert') as submit, \
             patch('tasks.tasks._dm_alertmanager_firing') as firing_dm, \
             patch('tasks.tasks._dm_alertmanager_resolved') as resolved_dm, \
             patch('tasks.tasks._reserve_alert_dm') as reserve, \
             patch('tasks.tasks._unassigned_alert_recipient_ids') as recipients, \
             patch('tasks.services.notifications.notification_service.notify_alert') as notify:
            result = process_alertmanager_alerts.run(
                1, alerts, payload_status='firing',
            )

        self.assertEqual(result['diagnostic_suppressed'], 2)
        self.assertEqual(result['dm'], 0)
        self.assertEqual(result['resolved_dm'], 0)
        submit.assert_not_called()
        firing_dm.assert_not_called()
        resolved_dm.assert_not_called()
        reserve.assert_not_called()
        recipients.assert_not_called()
        notify.assert_not_called()

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False)
    def test_only_raw_alertmanager_diagnostic_label_suppresses_delivery(self):
        from tasks.tasks import process_alertmanager_alerts

        base_alert = {
            'status': 'firing',
            'labels': {'alertname': 'ClassificationControl', 'severity': 'warning'},
            'annotations': {
                'summary': 'must remain actionable',
                'response_class': 'diagnostic',
            },
            'fingerprint': 'classification-control',
        }

        def run(source_name='k3s-alertmanager', auto_labels=None, labels=None):
            webhook_key = SimpleNamespace(
                id=1, source_name=source_name, default_severity='warning',
                auto_labels=auto_labels or {}, default_assignment_group='devops',
                record_usage=MagicMock(),
            )
            alert = {
                **base_alert,
                'labels': labels if labels is not None else base_alert['labels'],
            }
            with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
                 patch('tasks.tasks._dm_alertmanager_firing',
                       return_value='notified') as firing_dm:
                result = process_alertmanager_alerts.run(
                    1, [alert], payload_status='firing',
                )
            return result, firing_dm

        # Missing, unknown, and annotation-only values remain actionable.
        for labels in (
            {'alertname': 'ClassificationControl', 'severity': 'warning'},
            {
                'alertname': 'ClassificationControl', 'severity': 'warning',
                'response_class': 'unknown',
            },
        ):
            result, firing_dm = run(labels=labels)
            self.assertEqual(result['dm'], 1)
            self.assertEqual(result['diagnostic_suppressed'], 0)
            firing_dm.assert_called_once()

        # Mutable WebhookKey defaults cannot manufacture suppression.
        result, firing_dm = run(auto_labels={'response_class': 'diagnostic'})
        self.assertEqual(result['dm'], 1)
        self.assertEqual(result['diagnostic_suppressed'], 0)
        firing_dm.assert_called_once()

        # The helper is scoped to Alertmanager ingestion, not generic sources.
        result, firing_dm = run(
            source_name='origin-ci',
            labels={
                'alertname': 'ClassificationControl', 'severity': 'warning',
                'response_class': 'diagnostic',
            },
        )
        self.assertEqual(result['dm'], 1)
        self.assertEqual(result['diagnostic_suppressed'], 0)
        firing_dm.assert_called_once()

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=True)
    def test_persisted_diagnostic_firing_dedup_and_resolve_keep_rows_no_dm(self):
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1, source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
            default_project_id=None,
            record_usage=MagicMock(),
        )
        labels = {
            'alertname': 'VllmGenerationStarvationFast',
            'severity': 'warning',
            'response_class': 'diagnostic',
        }
        firing = {
            'status': 'firing', 'labels': labels,
            'annotations': {'summary': 'diagnostic'},
            'fingerprint': 'persisted-diagnostic',
        }
        resolved = {
            **firing,
            'status': 'resolved',
            'endsAt': '2026-08-09T00:05:00Z',
        }
        item = SimpleNamespace(id=77)
        resolved_item = MagicMock()
        resolve_q = MagicMock()
        resolve_q.filter.return_value = resolve_q
        resolve_q.__iter__.return_value = iter([resolved_item])

        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.models.Project.objects.filter') as projects, \
             patch('tasks.models.Item.objects.filter', return_value=resolve_q), \
             patch('tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                   side_effect=[(item, True), (item, False)]) as submit, \
             patch('tasks.tasks.send_alert_notification.delay') as notify, \
             patch('tasks.tasks._dm_alertmanager_resolved') as resolved_dm:
            projects.return_value.first.return_value = None
            first = process_alertmanager_alerts.run(
                1, [firing], payload_status='firing',
            )
            second = process_alertmanager_alerts.run(
                1, [firing], payload_status='firing',
            )
            final = process_alertmanager_alerts.run(
                1, [resolved], payload_status='resolved',
            )

        self.assertEqual(first['created'], 1)
        self.assertEqual(first['diagnostic_suppressed'], 1)
        self.assertEqual(second['deduplicated'], 1)
        self.assertEqual(final['resolved'], 1)
        self.assertEqual(submit.call_count, 2)
        resolved_item.resolve.assert_called_once_with(
            resolved_by='alertmanager:k3s-alertmanager',
        )
        notify.assert_not_called()
        resolved_dm.assert_not_called()

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_dm_dedup_suppresses_second_fire(self):
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch('tasks.services.notifications.notification_service.notify_alert') as notify:
            first = _dm_alertmanager_firing(
                webhook_key,
                title='KubeJobFailed',
                description='x',
                severity='warning',
                labels={
                    'alertname': 'KubeJobFailed',
                    'response_class': 'actionable',
                },
                payload={},
                source_url='',
                source_id='fp1',
            )
            second = _dm_alertmanager_firing(
                webhook_key,
                title='KubeJobFailed',
                description='x',
                severity='warning',
                labels={
                    'alertname': 'KubeJobFailed',
                    'response_class': 'actionable',
                },
                payload={},
                source_url='',
                source_id='fp1',
            )
        self.assertEqual(first, 'notified')
        self.assertEqual(second, 'deduped')
        self.assertEqual(notify.call_count, 1)
        packet = notify.call_args.args[0]
        self.assertEqual(packet.priority, 'normal')

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_class_dedup_collapses_distinct_fingerprints_of_same_alertname(self):
        """A crashlooping fleet is one page, not one page per pod."""
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        labels = {
            'alertname': 'KubePodCrashLooping',
            'severity': 'warning',
            'owner_group': 'devops',
        }
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[89]), \
             patch('tasks.services.notifications.notification_service.notify_alert') as notify:
            notify.return_value = {'connect_success': 1, 'success': 1, 'failed': 0, 'connect_deferred': 0}
            first = _dm_alertmanager_firing(
                webhook_key, title='KubePodCrashLooping', description='pod a',
                severity='warning', labels=labels, payload={}, source_url='',
                source_id='fp-pod-a',
            )
            second = _dm_alertmanager_firing(
                webhook_key, title='KubePodCrashLooping', description='pod b',
                severity='warning', labels=labels, payload={}, source_url='',
                source_id='fp-pod-b',
            )
            other = _dm_alertmanager_firing(
                webhook_key, title='KubeJobFailed', description='job x',
                severity='warning',
                labels={'alertname': 'KubeJobFailed', 'severity': 'warning', 'owner_group': 'devops'},
                payload={}, source_url='', source_id='fp-job',
            )
        self.assertEqual(first, 'notified')
        self.assertEqual(second, 'deduped')
        self.assertEqual(other, 'notified')
        self.assertEqual(notify.call_count, 2)

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_no_recipient_reservation_is_released_for_next_delivery(self):
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
        )
        kwargs = dict(
            title='NoSeat', description='x', severity='warning',
            labels={'alertname': 'NoSeat'}, payload={}, source_url='',
            source_id='release-no-recipient',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', side_effect=[[], [42]]), \
             patch('tasks.services.notifications.notification_service.notify_alert',
                   return_value={'connect_success': 1, 'success': 0, 'failed': 0}):
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'no_recipients')
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'notified')

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_deferred_and_failed_reservations_release_for_retry(self):
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
        )
        common = dict(
            title='RetryableAlert', description='x', severity='warning',
            labels={'alertname': 'RetryableAlert'}, payload={}, source_url='',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch('tasks.services.notifications.notification_service.notify_alert') as notify:
            notify.side_effect = [
                {'connect_success': 0, 'success': 0, 'failed': 0, 'connect_deferred': 1},
                {'connect_success': 1, 'success': 0, 'failed': 0, 'connect_deferred': 0},
            ]
            kwargs = {**common, 'source_id': 'release-deferred'}
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'deferred')
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'notified')
            # A later instance of the same class must not page again.
            kwargs = {**common, 'source_id': 'other-instance'}
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'deduped')

            failed_common = {
                **common,
                'title': 'RetryableFailed',
                'labels': {'alertname': 'RetryableFailed'},
            }
            notify.side_effect = [
                {'connect_success': 0, 'success': 0, 'failed': 1},
                {'connect_success': 1, 'success': 0, 'failed': 0},
            ]
            kwargs = {**failed_common, 'source_id': 'release-failed-result'}
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'failed')
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'notified')

            boom_common = {
                **common,
                'title': 'RetryableBoom',
                'labels': {'alertname': 'RetryableBoom'},
            }
            notify.side_effect = [RuntimeError('transport down'), {
                'connect_success': 1, 'success': 0, 'failed': 0,
            }]
            kwargs = {**boom_common, 'source_id': 'release-exception'}
            with self.assertRaisesRegex(RuntimeError, 'transport down'):
                _dm_alertmanager_firing(webhook_key, **kwargs)
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'notified')

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_routing_exception_releases_reservation_for_retry(self):
        """Every failure after cache.add(), not only transport, is retryable."""
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
        )
        kwargs = dict(
            title='RoutingRetry', description='x', severity='warning',
            labels={'alertname': 'RoutingRetry'}, payload={}, source_url='',
            source_id='release-routing-exception',
        )
        with patch(
            'tasks.views._routed_default_project_id',
            side_effect=[AttributeError('malformed webhook key'), None],
        ), patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch(
                 'tasks.services.notifications.notification_service.notify_alert',
                 return_value={'connect_success': 1, 'success': 0, 'failed': 0},
             ):
            with self.assertRaisesRegex(AttributeError, 'malformed webhook key'):
                _dm_alertmanager_firing(webhook_key, **kwargs)
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **kwargs), 'notified')

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False)
    def test_dm_packet_maps_critical_severity_to_urgent_priority(self):
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch('tasks.services.notifications.notification_service.notify_alert') as notify:
            outcome = _dm_alertmanager_firing(
                webhook_key,
                title='PlatformDatabaseDown',
                description='primary unavailable',
                severity='critical',
                labels={'alertname': 'PlatformDatabaseDown'},
                payload={},
                source_url='',
                source_id='critical-fp',
            )

        self.assertEqual(outcome, 'notified')
        self.assertEqual(notify.call_args.args[0].priority, 'urgent')

    def test_connect_formatter_defaults_missing_packet_priority_from_severity(self):
        from tasks.services.notifications import notification_service

        packet = SimpleNamespace(
            id=None,
            item_key=None,
            source_id='critical-fp',
            title='PlatformDatabaseDown',
            description='primary unavailable',
            severity='critical',
            status='firing',
            assignment_group='devops',
            labels={'alertname': 'PlatformDatabaseDown'},
            payload={},
            source_url='',
        )
        with patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            sent = notification_service._send_connect_alert_message(101, packet)

        self.assertTrue(sent)
        content = connect_post.call_args.kwargs['json']['content']
        self.assertIn('Severity: critical | Priority: urgent', content)

    def test_connect_formatter_renders_direct_annotation_runbook_url(self):
        from tasks.services.notifications import notification_service

        packet = SimpleNamespace(
            title='RunbookProbe', severity='warning', status='firing',
            assignment_group='devops', source_id='runbook-fp', source_url='',
            labels={'alertname': 'RunbookProbe'},
            payload={'annotations': {
                'summary': 'probe',
                'runbook_url': 'https://wiki.shizuha.com/runbooks/probe',
            }},
        )
        with patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            self.assertTrue(notification_service._send_connect_alert_message(101, packet))

        content = connect_post.call_args.kwargs['json']['content']
        self.assertIn('Runbook: https://wiki.shizuha.com/runbooks/probe', content)
        self.assertNotIn('Runbook: deploy/k3s/monitoring', content)

    @override_settings(
        PULSE_ALERTMANAGER_PERSIST_ITEMS=False,
        PULSE_ALERT_RESOLVED_DM_DEDUP_SECONDS=604800,
    )
    def test_resolved_webhook_sends_exactly_one_episode_dm(self):
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1, source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
            record_usage=MagicMock(),
        )
        alert = {
            'status': 'resolved',
            'labels': {'alertname': 'RecoveryProbe', 'severity': 'warning'},
            'annotations': {
                'summary': 'recovered',
                'runbook_url': 'https://wiki.shizuha.com/runbooks/recovery-probe',
            },
            'fingerprint': 'resolved-fp',
            'startsAt': '2026-08-02T08:00:00Z',
            'endsAt': '2026-08-02T08:05:00Z',
            'generatorURL': 'https://prometheus.example/graph',
        }
        empty_qs = MagicMock()
        empty_qs.filter.return_value = empty_qs
        empty_qs.__iter__.return_value = iter(())
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.models.Item.objects.filter', return_value=empty_qs), \
             patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch('tasks.services.notifications.notification_service.notify_user',
                   return_value={'success': 0, 'failed': 0, 'channels': []}), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            first = process_alertmanager_alerts.run(1, [alert], payload_status='resolved')
            second = process_alertmanager_alerts.run(1, [alert], payload_status='resolved')

        self.assertEqual(first['resolved_dm'], 1)
        self.assertEqual(first['resolved_dm_deduplicated'], 0)
        self.assertEqual(second['resolved_dm'], 0)
        self.assertEqual(second['resolved_dm_deduplicated'], 1)
        self.assertGreaterEqual(len(self.redis_clients), 4)
        self.assertEqual(
            len({id(client) for client in self.redis_clients}),
            len(self.redis_clients),
            'the regression must model distinct Pulse pod Redis clients',
        )
        self.assertEqual(connect_post.call_count, 1)
        content = connect_post.call_args.kwargs['json']['content']
        self.assertIn('[Alert Resolved]', content)
        self.assertIn('Status: resolved', content)
        self.assertIn('Runbook: https://wiki.shizuha.com/runbooks/recovery-probe', content)

    def test_non_owner_cannot_release_newer_shared_reservation(self):
        from tasks.tasks import (
            _release_alert_dm_reservation,
            _reserve_alert_dm,
        )

        first_client = _reserve_alert_dm('pulse:test:owner', 'owner-a', 600)
        self.assertIsNotNone(first_client)
        self.assertFalse(
            _release_alert_dm_reservation(
                'pulse:test:owner', 'owner-b', client=first_client,
            )
        )
        self.assertIsNone(_reserve_alert_dm('pulse:test:owner', 'owner-c', 600))
        self.assertTrue(
            _release_alert_dm_reservation(
                'pulse:test:owner', 'owner-a', client=first_client,
            )
        )
        self.assertIsNotNone(_reserve_alert_dm('pulse:test:owner', 'owner-c', 600))

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_dm_path_delivers_end_to_end_without_patching_notify_alert(self):
        """PLS-706: drive the REAL consumer, not a patched stand-in.

        The two pre-existing tests straddle the defect rather than cover it:
        one drives `_dm_alertmanager_firing` but patches `notify_alert`, so it
        stops at the packet; the other starts at `_send_connect_alert_message`
        with a hand-built object. The AttributeError lived in the seam between
        them — `notify_alert` reading a field the packet never set — so neither
        would have caught it, and the alert was lost on every channel while the
        Celery task reported `succeeded`.

        This patches only the outermost boundary (the Connect HTTP post) and a
        non-busy recipient, so the real notify_alert -> _send_connect_alert_message
        chain executes. `notify_user` is stubbed only because it is a
        DIFFERENT channel that needs the DB; the code under test —
        the packet/consumer seam — runs for real.
        """
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[42]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=False,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            outcome = _dm_alertmanager_firing(
                webhook_key,
                title='AlertmanagerFailedToSendAlerts',
                description='delivery failing',
                severity='critical',
                labels={'alertname': 'AlertmanagerFailedToSendAlerts'},
                payload={},
                source_url='',
                source_id='e2e-fp',
            )

        self.assertEqual(outcome, 'notified')
        self.assertTrue(
            connect_post.called,
            'no Connect DM was produced — the DM path raised before delivery',
        )
        content = connect_post.call_args.kwargs['json']['content']
        self.assertIn('Severity: critical | Priority: urgent', content)

    def test_connect_alert_tolerates_a_minimal_packet(self):
        """PLS-706 AC2: every optional field must be read defensively.

        The packet builder and this consumer are coupled only by convention,
        so guarding just `priority` would leave the class open — the next
        field added here would break the DM path again, equally silently.
        This passes the minimum the docstring promises; any newly-added
        unguarded `item.<attr>` read makes it raise.
        """
        from tasks.services.notifications import notification_service

        packet = SimpleNamespace(title='MinimalAlert', severity='error')

        with patch(
            'tasks.services.notifications._connect_alert_has_higher_priority_task',
            return_value=False,
        ), patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            sent = notification_service._send_connect_alert_message(101, packet)

        self.assertTrue(sent)
        content = connect_post.call_args.kwargs['json']['content']
        # Defaults stand in for every field the packet omitted.
        self.assertIn('Severity: error | Priority: high', content)
        self.assertIn('Status: firing', content)
        self.assertIn('Team: unassigned', content)

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_batch_distinguishes_contract_error_from_no_recipients(self):
        """PLS-706 AC4: `failed=N` must say WHAT failed.

        41 alerts were dropped across 5 pods while Celery reported `succeeded`,
        because a deterministic packet/consumer contract mismatch was counted
        as a bare `failed=1` — indistinguishable from a transient send failure
        or from a batch that simply had nobody to notify. A deterministic
        contract error recurs on every alert until code changes; the other two
        do not. Counting them alike is what made the outage invisible.
        """
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1, source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
            record_usage=MagicMock(),
        )
        alert = {
            'status': 'firing',
            'labels': {'alertname': 'ContractProbe'},
            'annotations': {'summary': 'x'},
            'fingerprint': 'contract-fp',
        }

        # A contract mismatch raises AttributeError out of the DM path.
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.tasks._dm_alertmanager_firing',
                   side_effect=AttributeError("'SimpleNamespace' object has no attribute 'priority'")):
            contract = process_alertmanager_alerts.run(1, [alert])

        # A batch with nobody to notify is a routing outcome, not a failure.
        cache.clear()
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.tasks._dm_alertmanager_firing', return_value='no_recipients'):
            empty = process_alertmanager_alerts.run(1, [alert])

        self.assertEqual(contract['failed'], 1)
        self.assertEqual(contract['contract_failed'], 1)
        self.assertEqual(contract['no_recipients'], 0)

        self.assertEqual(empty['failed'], 0, 'no-recipients is not a failure')
        self.assertEqual(empty['contract_failed'], 0)
        self.assertEqual(empty['no_recipients'], 1, 'no-recipients must not be silent')

        # The discriminating property: the two outcomes must not look alike.
        self.assertNotEqual(
            (contract['failed'], contract['contract_failed'], contract['no_recipients']),
            (empty['failed'], empty['contract_failed'], empty['no_recipients']),
        )

    def test_dm_path_reports_suppression_when_every_recipient_is_deferred(self):
        """PLS-706 AC5 (tightened): entering the DM path is not delivering on it.

        @mio's live sweep found the shape this covers: across 5 pods / 48
        batches, 61 DM notifies logged `connect=0 channels=0` on 49 of them —
        ~80% of alerts reached nobody — while every batch still reported
        `failed=0`. `notify_alert` already counts `connect_deferred`
        (notifications.py:1039) and the caller discarded it, so a delivered
        alert and a fully-suppressed one produced identical output.

        Drives the REAL notify_alert with the deferral predicate forced True,
        rather than stubbing the return value: the point is that the suppression
        propagates through the actual seam, not that a mocked dict round-trips.
        """
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[123]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=True,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            outcome = _dm_alertmanager_firing(
                webhook_key,
                title='KubeJobFailed',
                description='job failed',
                severity='warning',
                labels={'alertname': 'KubeJobFailed'},
                payload={},
                source_url='',
                source_id='deferred-fp',
            )

        self.assertEqual(
            outcome, 'deferred',
            'a batch that delivered on no channel must not report as notified',
        )
        self.assertFalse(
            connect_post.called,
            'deferral must short-circuit before the Connect transport',
        )

    def test_batch_distinguishes_full_suppression_from_delivery(self):
        """PLS-706 AC5: a delivered batch and a suppressed one must differ.

        This is the discriminator @mio asked for. Without it `dm=N` counts
        entries into the path rather than deliveries, so "every alert reached
        someone" and "no alert reached anyone" print the same line — which is
        how the loss stayed invisible even after the crash was fixed.

        The unbounded deferral itself is PLS-715; this only makes it legible.
        """
        from tasks.tasks import process_alertmanager_alerts

        webhook_key = SimpleNamespace(
            id=1, source_name='k3s-alertmanager', default_severity='warning',
            auto_labels={}, default_assignment_group='devops',
            record_usage=MagicMock(),
        )
        alert = {
            'status': 'firing',
            'labels': {'alertname': 'SuppressionProbe'},
            'annotations': {'summary': 'x'},
            'fingerprint': 'suppression-fp',
        }

        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.tasks._dm_alertmanager_firing', return_value='notified'):
            delivered = process_alertmanager_alerts.run(1, [alert])

        cache.clear()
        with patch('tasks.models.WebhookKey.objects.get', return_value=webhook_key), \
             patch('tasks.tasks._dm_alertmanager_firing', return_value='deferred'):
            suppressed = process_alertmanager_alerts.run(1, [alert])

        # Both entered the DM path, so dm= alone cannot tell them apart --
        # which is precisely why dm= was insufficient as a health signal.
        self.assertEqual(delivered['dm'], 1)
        self.assertEqual(suppressed['dm'], 1)

        self.assertEqual(delivered['deferred'], 0)
        self.assertEqual(suppressed['deferred'], 1, 'suppression must not be silent')

        # Neither is a failure, so failed= cannot tell them apart either.
        self.assertEqual(delivered['failed'], 0)
        self.assertEqual(suppressed['failed'], 0)

        # The discriminating property.
        self.assertNotEqual(
            (delivered['dm'], delivered['failed'], delivered['deferred']),
            (suppressed['dm'], suppressed['failed'], suppressed['deferred']),
        )


class Pls715BoundedDeferralTests(SimpleTestCase):
    """PLS-715: a busy recipient cannot suppress an alert DM indefinitely.

    The unbounded deferral (PLS-706 AC5 made it legible; this bounds it):
    `_connect_alert_has_higher_priority_task` returns True for as long as the
    recipient holds higher-priority WIP, so a saturated seat suppressed ~80%
    of live alert DMs forever with `failed=0` success. The fix adds a
    per-(recipient, alert) deferral ceiling: after
    ALERT_CONNECT_DEFER_MAX_ATTEMPTS deferrals the alert is delivered anyway.

    Drives the REAL consumer boundary (`_dm_alertmanager_firing` — the actual
    DM delivery seam) with the deferral predicate forced True, across multiple
    firing episodes, asserting the bounded outcome: N deferrals then delivery.
    """

    def setUp(self):
        cache.clear()
        self.redis_state = _SharedRedisState()

        def distinct_client():
            return _DistinctRedisClient(self.redis_state)

        patcher = patch(
            'tasks.tasks._alert_dm_redis_client',
            side_effect=distinct_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_deferral_is_bounded_delivers_after_ceiling(self):
        from tasks.services import notifications as notif_mod
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        common = dict(
            title='KubeJobFailed',
            description='job failed',
            severity='warning',
            labels={'alertname': 'KubeJobFailed'},
            payload={},
            source_url='',
            source_id='bounded-defer-fp',
        )
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[123]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=True,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200

            # The first ALERT_CONNECT_DEFER_MAX_ATTEMPTS episodes are deferred
            # (busy seat holds higher-priority WIP) — bounded, not silent.
            ceiling = notif_mod.ALERT_CONNECT_DEFER_MAX_ATTEMPTS
            outcomes = [
                _dm_alertmanager_firing(webhook_key, **common)
                for _ in range(ceiling)
            ]
            self.assertEqual(
                outcomes, ['deferred'] * ceiling,
                'the first episodes must defer while the seat is busy',
            )
            self.assertFalse(
                connect_post.called,
                'deferral must short-circuit before the Connect transport',
            )

            # The next episode exceeds the ceiling → delivered anyway. No alert
            # is suppressible indefinitely by one busy seat.
            outcome = _dm_alertmanager_firing(webhook_key, **common)
            self.assertEqual(outcome, 'notified')
            self.assertTrue(
                connect_post.called,
                'after the ceiling the alert must be delivered, not dropped',
            )

    @override_settings(PULSE_ALERTMANAGER_PERSIST_ITEMS=False, PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_delivery_resets_the_deferral_budget(self):
        """A real delivery clears the counter so a fresh episode starts clean."""
        from tasks.services import notifications as notif_mod
        from tasks.tasks import _dm_alertmanager_firing

        webhook_key = SimpleNamespace(
            source_name='k3s-alertmanager',
            default_severity='warning',
            auto_labels={},
            default_assignment_group='devops',
        )
        common = dict(
            title='KubeDaemonSetMisScheduled',
            description='daemonset not scheduled',
            severity='warning',
            labels={'alertname': 'KubeDaemonSetMisScheduled'},
            payload={},
            source_url='',
            source_id='reset-budget-fp',
        )
        # First episode: busy → defer.
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[7]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=True,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **common), 'deferred')
            self.assertFalse(connect_post.called)

        # Second episode: seat free → delivered, budget reset.
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[7]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=False,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **common), 'notified')
            self.assertTrue(connect_post.called)

        # Third episode: busy again → the budget was reset, so it defers again
        # (not immediately exhausted from the pre-delivery deferrals). Clear the
        # shared Redis reservation first so the episode is not deduped (delivery
        # keeps the reservation for the dedupe window).
        self.redis_state.values.clear()
        with patch('tasks.views._routed_default_project_id', return_value=None), \
             patch('tasks.views._label_assignment_group', return_value='devops'), \
             patch('tasks.tasks._unassigned_alert_recipient_ids', return_value=[7]), \
             patch(
                 'tasks.services.notifications._connect_alert_has_higher_priority_task',
                 return_value=True,
             ), \
             patch(
                 'tasks.services.notifications.notification_service.notify_user',
                 return_value={'success': 0, 'failed': 0, 'channels': []},
             ), \
             patch('tasks.services.notifications.requests.post') as connect_post:
            connect_post.return_value.status_code = 200
            self.assertEqual(_dm_alertmanager_firing(webhook_key, **common), 'deferred')
            self.assertFalse(connect_post.called)
