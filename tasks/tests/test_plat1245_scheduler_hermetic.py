"""PLAT-1245 regression: the Item post_save timer signal must NOT open a redis
connection under the hermetic test suite (settings.TESTING). Before the fix, every
Item.save() hit the :9 discard-sentinel broker with retry-backoff, SIGKILLing the
CI test Job at 23m and blocking all pulse deploys."""
from types import SimpleNamespace
import os
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from tasks import signals
from tasks.services.admin_service import AdminService


class SchedulerHermeticTest(SimpleTestCase):
    def _fire(self):
        # Directly invoke the post_save receiver with a dummy Item-like instance;
        # no DB needed. If the guard is missing, it would call redis.Redis.from_url.
        instance = SimpleNamespace(pk=1, mode='task', is_recurring=False,
                                   frequency=None, assignee_id=None,
                                   due_date=None, reminder_time=None)
        signals.schedule_item_timer(sender=None, instance=instance, created=True)

    @override_settings(TESTING=True)
    def test_no_redis_connection_when_testing(self):
        with patch('redis.Redis.from_url') as from_url:
            self._fire()
        from_url.assert_not_called()

    @override_settings(TESTING=False)
    def test_redis_attempted_when_not_testing(self):
        # With TESTING off, the signal proceeds (and, with no broker, fails
        # non-fatally) — proving the guard is the ONLY thing gating it.
        # get_timer_manager() caches a module-global TimerManager singleton, so
        # reset it: otherwise a manager built by an earlier test is reused and
        # redis.Redis.from_url is never called on this run (order-dependent).
        from tasks import scheduler
        scheduler._manager = None
        with patch('redis.Redis.from_url') as from_url:
            self._fire()
        from_url.assert_called()


class CrossServiceHermeticSettingsTest(SimpleTestCase):
    def test_admin_clients_fast_fail_on_test_owned_loopback(self):
        self.assertEqual(settings.SHIZUHA_ADMIN_API_URL, 'http://127.0.0.1:9/api')
        self.assertEqual(AdminService().admin_api_url, settings.SHIZUHA_ADMIN_API_URL)
        self.assertEqual(os.environ['SHIZUHA_ADMIN_API_URL'], settings.SHIZUHA_ADMIN_API_URL)
        self.assertEqual(os.environ['ADMIN_INTERNAL_URL'], 'http://127.0.0.1:9')
