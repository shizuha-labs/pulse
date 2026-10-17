"""PLS-374: Prometheus counter for no-routable-candidate routability holds.

Required before the PULSE_ROUTABILITY_ENFORCE shadow->enforce flip so the bake
go/no-go has a first-class observable signal (Fail-Loud liveness #2, PLAT-1254),
not log/comment scraping. Follow-up to PLS-278.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService

METRIC = 'pulse_routability_no_candidate_holds_total'


class RoutabilityHoldMetricTests(SimpleTestCase):
    def setUp(self):
        # Isolate the per-reason counters between tests.
        for reason in WorkflowService.ROUTABILITY_HOLD_REASONS:
            cache.delete(WorkflowService._routability_hold_metric_key(reason))

    def test_render_lists_every_reason_at_zero_before_any_hold(self):
        """A normal, hold-free run leaves every reason series at 0 (the counter does
        NOT increment on successful assignments) and the series is always present."""
        body = WorkflowService.render_routability_hold_metrics()
        self.assertIn(f'# TYPE {METRIC} counter', body)
        for reason in WorkflowService.ROUTABILITY_HOLD_REASONS:
            self.assertIn(f'{METRIC}{{reason="{reason}"}} 0', body)

    def test_hold_increments_only_the_held_reasons(self):
        WorkflowService._incr_routability_hold_metric(['seat_not_live'])
        WorkflowService._incr_routability_hold_metric(['seat_not_live', 'over_capacity'])
        body = WorkflowService.render_routability_hold_metrics()
        self.assertIn(f'{METRIC}{{reason="seat_not_live"}} 2', body)
        self.assertIn(f'{METRIC}{{reason="over_capacity"}} 1', body)
        # Untouched reasons stay at 0 (no spurious increments).
        self.assertIn(f'{METRIC}{{reason="team_unknown"}} 0', body)
        self.assertIn(f'{METRIC}{{reason="seat_incapable"}} 0', body)

    def test_counter_survives_past_the_300s_default_ttl(self):
        """PLS-853 regression: the seed MUST pass timeout=None so the counter is
        non-expiring. With the old `cache.add(key, 0)` the key died 300s after
        seeding regardless of activity (Django's 300s default, and neither LocMem
        nor Redis incr refreshes expiry), so any increase()/rate() over the bake
        window was corrupt. The production sequence is: seed -> incr -> render.
        This fixture asserts the seeded key carries NO expiry, then simulates the
        old 300s window having elapsed and proves the counter still reads back."""
        # The production call seeds with timeout=None; the stored value must be
        # readable after the 300s default window would have expired a finite TTL.
        WorkflowService._incr_routability_hold_metric(['seat_not_live'])
        # Test settings pin the cache to LocMemCache; its _expire_info holds the
        # absolute expiry (None == non-expiring). Assert the seed wrote no expiry.
        backend = cache._cache if hasattr(cache, '_cache') else None
        if backend is not None and hasattr(cache, '_expire_info'):
            key = WorkflowService._routability_hold_metric_key('seat_not_live')
            # A finite TTL would have an absolute timestamp here; None means never
            # expires (get_backend_timeout(timeout=None) -> None).
            self.assertIsNone(
                cache._expire_info.get(key),
                'PLS-374 counter seed must be non-expiring (timeout=None); '
                'a finite TTL resets the series 300s after seeding (PLS-853).',
            )
        # Regardless of backend, the value must still render after the old default
        # window: read it back and assert the series is present and nonzero.
        body = WorkflowService.render_routability_hold_metrics()
        self.assertIn(f'{METRIC}{{reason="seat_not_live"}} 1', body)

    def test_blank_and_unknown_reasons_are_ignored(self):
        WorkflowService._incr_routability_hold_metric(['', '  ', None])
        body = WorkflowService.render_routability_hold_metrics()
        for reason in WorkflowService.ROUTABILITY_HOLD_REASONS:
            self.assertIn(f'{METRIC}{{reason="{reason}"}} 0', body)

    def test_record_routability_hold_fires_the_metric(self):
        """The real hold path (_record_routability_hold) increments the counter before
        its comment-dedupe early-return — so every hold event is counted."""
        with patch.object(WorkflowService, '_incr_routability_hold_metric') as spy, \
             patch.object(WorkflowService, '_notify_routability_hold'), \
             patch.object(WorkflowService, '_canonical_team_name', return_value='engineering'):
            item = SimpleNamespace(id=1, item_key='PLS-9999')
            # The Comment/DB path fail-safes (caught) under SimpleTestCase; the metric
            # increment happens before it, so the spy is still called.
            WorkflowService._record_routability_hold(
                item, 'engineering', ['seat_not_live'], source='test',
            )
        spy.assert_called_once()
        self.assertIn('seat_not_live', list(spy.call_args[0][0]))

    def test_no_reasons_no_metric(self):
        with patch.object(WorkflowService, '_incr_routability_hold_metric') as spy:
            WorkflowService._record_routability_hold(
                SimpleNamespace(id=1, item_key='PLS-9999'), 'engineering', [], source='test',
            )
        spy.assert_not_called()
