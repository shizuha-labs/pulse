"""PLAT-12617: the pulse-internal mode=alert Items DM path must class-dedup.

The Alertmanager webhook path has had (source, kind, alertname, severity,
team) class-key dedup since the 1h-TTL reservation landed; `notify_alert`
(the path every `send_alert_notification.delay()` caller uses) had NONE —
every re-fired task re-paged every recipient on every channel (12k+
duplicate notifications/day, 3,607 copies of one estate item to a single
user). These tests pin the reservation discipline on the Items path:

- second send for the same alert inside the TTL is deduped (one delivery);
- a severity escalation (different key) re-pages;
- a total delivery failure releases the reservation so the next attempt
  is not eaten by the TTL;
- a notify_alert exception releases the reservation (retryable).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from tasks.tests.test_alertmanager_dm_only import (
    _DistinctRedisClient,
    _SharedRedisState,
)


def _alert_item(item_id=35273, severity='warning', status='in_review'):
    return SimpleNamespace(
        id=item_id,
        mode='alert',
        title='Alert: AgentRuntimeEstateSingleDown',
        severity=severity,
        status=status,
        source='k3s-alertmanager',
        labels={'alertname': 'AgentRuntimeEstateSingleDown'},
        item_key='PLAT-0000',
    )


class AlertItemDmClassDedupTests(SimpleTestCase):
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

        self.alert = _alert_item()
        # Patch the whole manager, not `Item.objects.get`: the task calls
        # `Item.objects.select_related('project').get(...)`, and the QuerySet
        # returned by select_related bypasses a Manager-instance `get` patch —
        # CI (run 11085) correctly flagged the resulting DB touch as
        # DatabaseOperationForbidden in SimpleTestCase. Patching the manager
        # object intercepts every access path.
        item_patcher = patch('tasks.models.Item.objects')
        item_manager = item_patcher.start()
        item_manager.select_related.return_value.get.return_value = self.alert
        self.addCleanup(item_patcher.stop)

        recipient_patcher = patch(
            'tasks.tasks._unassigned_alert_recipient_ids',
            return_value=[101],
        )
        recipient_patcher.start()
        self.addCleanup(recipient_patcher.stop)

    def _task(self):
        from tasks.tasks import send_alert_notification

        return send_alert_notification

    @override_settings(PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_second_send_within_ttl_is_deduped(self):
        notify = self._task()
        with patch(
            'tasks.services.notifications.notification_service.notify_alert',
            return_value={'success': 1, 'failed': 0, 'channels': ['app'],
                          'connect_success': 1, 'connect_deferred': 0},
        ) as notify_alert:
            first = notify.run(self.alert.id)
            second = notify.run(self.alert.id)

        self.assertIn('Sent notification', str(first))
        self.assertIn('deduped', str(second))
        self.assertEqual(notify_alert.call_count, 1)

    @override_settings(PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_severity_escalation_re_pages(self):
        notify = self._task()
        with patch(
            'tasks.services.notifications.notification_service.notify_alert',
            return_value={'success': 1, 'failed': 0, 'channels': ['app'],
                          'connect_success': 1, 'connect_deferred': 0},
        ) as notify_alert:
            notify.run(self.alert.id)
            self.alert.severity = 'critical'  # escalation — different class key
            second = notify.run(self.alert.id)

        self.assertIn('Sent notification', str(second))
        self.assertEqual(notify_alert.call_count, 2)

    @override_settings(PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_total_delivery_failure_releases_reservation(self):
        notify = self._task()
        with patch(
            'tasks.services.notifications.notification_service.notify_alert',
            return_value={'success': 0, 'failed': 2, 'channels': [],
                          'connect_success': 0, 'connect_deferred': 0},
        ) as notify_alert:
            first = notify.run(self.alert.id)
            second = notify.run(self.alert.id)

        self.assertIn('Sent notification', str(first))
        self.assertIn('Sent notification', str(second))
        self.assertEqual(notify_alert.call_count, 2)

    @override_settings(PULSE_ALERT_DM_DEDUP_SECONDS=3600)
    def test_notify_alert_exception_releases_reservation(self):
        notify = self._task()
        with patch(
            'tasks.services.notifications.notification_service.notify_alert',
            side_effect=RuntimeError('connect transport down'),
        ):
            with self.assertRaises(RuntimeError):
                notify.run(self.alert.id)
        # Reservation must be released — a retry pages again (delivers).
        with patch(
            'tasks.services.notifications.notification_service.notify_alert',
            return_value={'success': 1, 'failed': 0, 'channels': ['app'],
                          'connect_success': 1, 'connect_deferred': 0},
        ) as notify_alert:
            result = notify.run(self.alert.id)
        self.assertIn('Sent notification', str(result))
        self.assertEqual(notify_alert.call_count, 1)
