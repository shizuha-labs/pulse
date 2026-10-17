"""PLS-621: routing source-health aggregates + pre-triage starvation signals.

Production-order regression: two consecutive false-disposition cycles on
detector artifacts must leave one durable source_unhealthy series (not silence
and not nine isolated tasks). Pre-triage enter→exit cancels the age arm.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from tasks.services import routing_source_health as rsh


def _artifact(**kwargs):
    base = dict(
        pk='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        item_key='PLAT-4985',
        title='[Team Outage] shizuha::qa — 0 live members',
        metadata={
            'org_health_signal': 'capacity-starve:1:qa',
            'auto_filed': '2026-07-19T00:00:00+00:00',
        },
        organization_id=1,
        assignment_group='qa',
        priority='normal',
        status='open',
        resolution=None,
        project=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'pls621-tests',
        }
    }
)
class RoutingSourceHealthTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_two_disposition_cycles_accumulate_one_series(self):
        """Replay PLAT-4985-style cancellations: count grows; series stays unhealthy."""
        with mock.patch.object(rsh, '_emit_dm_once', return_value=True) as dm:
            with override_settings():
                # threshold 3 → alert on third
                with mock.patch.object(rsh, 'false_disposition_alert_threshold', return_value=3):
                    s1 = rsh.record_false_disposition(
                        _artifact(item_key='PLAT-4985'),
                        resolution_slug='false_positive',
                        status_slug='done',
                        actor='shion@shizuha.com',
                    )
                    s2 = rsh.record_false_disposition(
                        _artifact(item_key='PLAT-4986'),
                        resolution_slug='not_a_bug',
                        status_slug='done',
                        actor='shion@shizuha.com',
                    )
                    # Second cycle (same detector/subject) after "fixing" children
                    s3 = rsh.record_false_disposition(
                        _artifact(item_key='PLAT-5009'),
                        resolution_slug='cancelled',
                        status_slug='cancelled',
                        actor='shion@shizuha.com',
                    )
                    s4 = rsh.record_false_disposition(
                        _artifact(item_key='PLAT-5030'),
                        resolution_slug='wont_fix',
                        status_slug='done',
                        actor='shion@shizuha.com',
                    )

        self.assertEqual(s1['count'], 1)
        self.assertEqual(s2['count'], 2)
        self.assertEqual(s3['count'], 3)
        self.assertEqual(s4['count'], 4)
        self.assertFalse(s4['healthy'])
        self.assertEqual(s4['class'], 'source_unhealthy')
        self.assertEqual(s4['detector'], 'capacity-starve')
        self.assertEqual(s4['subject'], 'qa')
        keys = {s['item_key'] for s in s4['samples']}
        self.assertIn('PLAT-4985', keys)
        self.assertIn('PLAT-5030', keys)
        # One durable series — not silence after first cancel
        unhealthy = rsh.list_unhealthy_series()
        self.assertEqual(len(unhealthy), 1)
        self.assertGreaterEqual(unhealthy[0]['count'], 4)
        # Alert armed once threshold crossed (deduped claim may allow 1+)
        self.assertGreaterEqual(dm.call_count, 1)
        kind_args = [c.args[0] if c.args else c.kwargs.get('signal_key') for c in dm.call_args_list]
        self.assertTrue(any(str(a).startswith('source-unhealthy:') for a in kind_args))

    def test_single_disposition_does_not_clear_aggregate(self):
        rsh.record_false_disposition(
            _artifact(item_key='PLAT-A'),
            resolution_slug='false_positive',
            status_slug='done',
        )
        rsh.record_false_disposition(
            _artifact(item_key='PLAT-B'),
            resolution_slug='false_positive',
            status_slug='done',
        )
        series = rsh.get_series('capacity-starve', '1', 'qa')
        self.assertEqual(series['count'], 2)
        self.assertFalse(series['healthy'])
        # Explicit healthy is the only clear path
        cleared = rsh.mark_source_healthy('capacity-starve', '1', 'qa')
        self.assertTrue(cleared['healthy'])

    def test_non_detector_item_ignored(self):
        item = _artifact(
            title='Normal feature work',
            metadata={},
            item_key='FEAT-1',
        )
        self.assertIsNone(
            rsh.record_false_disposition(
                item, resolution_slug='false_positive', status_slug='done',
            )
        )

    def test_fixed_resolution_not_counted_via_observe(self):
        item = _artifact(item_key='PLAT-FIX')
        rsh.observe_transition(
            item,
            original_status='open',
            resolved_status='done',
            resolution_slug='fixed',
            actor='eng@shizuha.com',
        )
        self.assertEqual(rsh.list_unhealthy_series(), [])

    def test_pretriage_enter_exit_and_urgent_signal(self):
        with mock.patch.object(rsh, '_emit_dm_once', return_value=True) as dm:
            urgent = _artifact(
                item_key='BUG-URG',
                title='Something urgent',
                metadata={},
                priority='urgent',
                assignment_group='engineering',
            )
            rsh.observe_transition(
                urgent,
                original_status='open',
                resolved_status='needs_triage',
                resolution_slug='',
            )
            self.assertGreaterEqual(dm.call_count, 1)
            self.assertTrue(
                any('queue-starving' in str(c) for c in dm.call_args_list)
            )

            # Exit clears arm state
            rsh.observe_transition(
                urgent,
                original_status='needs_triage',
                resolved_status='in_progress',
                resolution_slug='',
            )
            state = cache.get(rsh._PRETRIAGE_KEY.format(item_id=str(urgent.pk)))
            self.assertIsNone(state)

    def test_pretriage_deadline_only_fires_while_still_in_pretriage(self):
        item = _artifact(
            pk='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
            item_key='BUG-AGE',
            title='Aged triage',
            metadata={},
            priority='normal',
            assignment_group='engineering',
            status='needs_triage',
        )
        with mock.patch(
            'tasks.tasks.evaluate_pretriage_age_deadline'
        ) as celery_task:
            celery_task.apply_async = mock.Mock()
            rsh.on_pretriage_enter(item)
            celery_task.apply_async.assert_called_once()

        # Still in needs_triage → deadline evaluates true path when age forced
        with mock.patch.object(rsh, 'pretriage_age_threshold_hours', return_value=0.0):
            with mock.patch.object(rsh, '_emit_queue_starving_alert', return_value=True) as alert:
                with mock.patch('tasks.models.Item') as ItemMod:
                    ItemMod.objects.filter.return_value.first.return_value = item
                    item.status = 'needs_triage'
                    fired = rsh.evaluate_pretriage_deadline_for_item(str(item.pk))
        self.assertTrue(fired)
        alert.assert_called_once()

        # Re-enter state then leave pre-triage → no fire
        rsh.on_pretriage_enter(item)
        item.status = 'in_progress'
        with mock.patch('tasks.models.Item') as ItemMod:
            ItemMod.objects.filter.return_value.first.return_value = item
            with mock.patch.object(rsh, '_emit_queue_starving_alert') as alert2:
                fired2 = rsh.evaluate_pretriage_deadline_for_item(str(item.pk))
        self.assertFalse(fired2)
        alert2.assert_not_called()

    def test_prometheus_lines_for_unhealthy_series(self):
        rsh.record_false_disposition(
            _artifact(),
            resolution_slug='false_positive',
            status_slug='done',
        )
        lines = rsh.prometheus_lines()
        self.assertTrue(any('pulse_routing_source_unhealthy_dispositions' in ln for ln in lines))
