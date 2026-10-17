"""Duration-gated demotion/recovery for provider + runtime health signals.

Short flaps (seconds) must not thrash routing; sustained bad (minutes) must
demote so rebalance can drain work to healthy peers (Agy runtime_unhealthy class).
"""
from django.test import SimpleTestCase

from tasks.tasks import (
    _apply_health_signal_hysteresis,
    _classify_connect_health_signal,
    _HARD_HEALTH_UNAVAILABILITY_REASONS,
    _SOFT_UNAVAILABILITY_REASONS,
)


class HealthSignalHysteresisTests(SimpleTestCase):
    def test_short_blip_does_not_demote(self):
        now = 1_000_000.0
        sustained, state = _apply_health_signal_hysteresis(
            {'agy@shizuha.com': 'runtime_unhealthy'},
            {},
            now,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained, {})
        self.assertIn('agy@shizuha.com', state)
        self.assertFalse(state['agy@shizuha.com']['down'])
        self.assertAlmostEqual(state['agy@shizuha.com']['bad_since'], now)

    def test_sustained_bad_demotes_after_threshold(self):
        t0 = 1_000_000.0
        _, state1 = _apply_health_signal_hysteresis(
            {'agy@shizuha.com': 'runtime_unhealthy'},
            {},
            t0,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        sustained, state2 = _apply_health_signal_hysteresis(
            {'agy@shizuha.com': 'runtime_unhealthy'},
            state1,
            t0 + 181,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained, {'agy@shizuha.com': 'runtime_unhealthy'})
        self.assertTrue(state2['agy@shizuha.com']['down'])
        # bad_since preserved across scrapes (restart-safe clock).
        self.assertAlmostEqual(state2['agy@shizuha.com']['bad_since'], t0)

    def test_short_recovery_blip_does_not_restore(self):
        t0 = 1_000_000.0
        down_state = {
            'agy@shizuha.com': {
                'down': True,
                'bad_since': t0 - 600,
                'good_since': None,
                'reason': 'runtime_unhealthy',
            },
        }
        sustained, state = _apply_health_signal_hysteresis(
            {},  # instantaneous healthy
            down_state,
            t0,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained, {'agy@shizuha.com': 'runtime_unhealthy'})
        self.assertTrue(state['agy@shizuha.com']['down'])
        self.assertAlmostEqual(state['agy@shizuha.com']['good_since'], t0)

        # Still within recover window.
        sustained2, state2 = _apply_health_signal_hysteresis(
            {},
            state,
            t0 + 60,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained2, {'agy@shizuha.com': 'runtime_unhealthy'})
        self.assertTrue(state2['agy@shizuha.com']['down'])

    def test_sustained_healthy_recovers(self):
        t0 = 1_000_000.0
        down_state = {
            'agy@shizuha.com': {
                'down': True,
                'bad_since': t0 - 600,
                'good_since': t0,
                'reason': 'provider_down',
            },
        }
        sustained, state = _apply_health_signal_hysteresis(
            {},
            down_state,
            t0 + 181,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained, {})
        self.assertNotIn('agy@shizuha.com', state)

    def test_flip_back_to_bad_resets_recovery_clock(self):
        t0 = 1_000_000.0
        recovering = {
            'agy@shizuha.com': {
                'down': True,
                'bad_since': t0 - 600,
                'good_since': t0,
                'reason': 'provider_down',
            },
        }
        sustained, state = _apply_health_signal_hysteresis(
            {'agy@shizuha.com': 'provider_down'},
            recovering,
            t0 + 100,
            demote_after_sec=180,
            recover_after_sec=180,
        )
        self.assertEqual(sustained, {'agy@shizuha.com': 'provider_down'})
        self.assertIsNone(state['agy@shizuha.com']['good_since'])
        self.assertTrue(state['agy@shizuha.com']['down'])

    def test_instant_demote_when_threshold_zero(self):
        now = 1_000_000.0
        sustained, state = _apply_health_signal_hysteresis(
            {'rei@shizuha.com': 'provider_down'},
            {},
            now,
            demote_after_sec=0,
            recover_after_sec=0,
        )
        self.assertEqual(sustained, {'rei@shizuha.com': 'provider_down'})
        self.assertTrue(state['rei@shizuha.com']['down'])

    def test_runtime_unhealthy_is_hard_not_soft(self):
        self.assertIn('runtime_unhealthy', _HARD_HEALTH_UNAVAILABILITY_REASONS)
        self.assertNotIn('runtime_unhealthy', _SOFT_UNAVAILABILITY_REASONS)

    def test_classify_provider_flag(self):
        self.assertEqual(
            _classify_connect_health_signal({'provider_unavailable': True}, {}),
            'provider_down',
        )
        self.assertIsNone(
            _classify_connect_health_signal({'ok': True, 'recent_errors': []}, {}),
        )
