"""PLAT-708: the health sweep durably deactivates provider-exhausted (codex
usage-limited) agents in AgentAvailability so routing + the UI reflect reality,
and auto-restores them on recovery — without clobbering operator force-OFF rows
or falsely deactivating busy claude agents.
"""

from django.test import TestCase

from tasks.models import AgentAvailability
from tasks.tasks import _reconcile_health_managed_availability, HEALTH_SWEEP_ACTOR

NOW = '2026-06-22T12:00:00Z'
THRESHOLD = 3


class AutoDeactivateProviderExhaustedTests(TestCase):
    def test_sustained_exhaustion_creates_inactive_row_when_none(self):
        deact, react = _reconcile_health_managed_availability(
            {'kai@shizuha.com': 4}, THRESHOLD, NOW)
        self.assertEqual(deact, ['kai@shizuha.com'])
        self.assertEqual(react, [])
        row = AgentAvailability.objects.get(email='kai@shizuha.com')
        self.assertFalse(row.active)
        self.assertEqual(row.status, 'inactive')
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)

    def test_overrides_stale_operator_active_true(self):
        # The exact reported bug: operator force-ON shadowing provider-down.
        AgentAvailability.objects.create(
            email='haru@shizuha.com', active=True, status='active',
            reason='operator 2026-06-18: codex usage back', updated_by='operator')
        deact, _ = _reconcile_health_managed_availability(
            {'haru@shizuha.com': 5}, THRESHOLD, NOW)
        self.assertEqual(deact, ['haru@shizuha.com'])
        row = AgentAvailability.objects.get(email='haru@shizuha.com')
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)
        self.assertIn('was active by operator', row.reason)

    def test_below_threshold_no_change(self):
        AgentAvailability.objects.create(
            email='nao@shizuha.com', active=True, status='active', updated_by='operator')
        deact, react = _reconcile_health_managed_availability(
            {'nao@shizuha.com': 2}, THRESHOLD, NOW)  # 2 < 3
        self.assertEqual((deact, react), ([], []))
        self.assertTrue(AgentAvailability.objects.get(email='nao@shizuha.com').active)

    def test_recovery_reactivates_only_rows_we_own(self):
        AgentAvailability.objects.create(
            email='ren@shizuha.com', active=False, status='inactive',
            reason='auto: provider exhausted', updated_by=HEALTH_SWEEP_ACTOR)
        # PLS-741/PLS-869: recovery requires EXPLICIT measured provenance. The
        # sweep passes the filtered (non-JOIN_PAD) sample set as
        # measured_streak_emails; here ren's 0 is certified measured, so it
        # reactivates.
        _, react = _reconcile_health_managed_availability(
            {'ren@shizuha.com': 0}, THRESHOLD, NOW,
            measured_streak_emails={'ren@shizuha.com'})
        self.assertEqual(react, ['ren@shizuha.com'])
        self.assertTrue(AgentAvailability.objects.get(email='ren@shizuha.com').active)

    def test_join_pad_zero_without_provenance_is_fail_closed(self):
        # The pre-fix call shape: a bare 0 in the streak map with NO measured
        # provenance is indistinguishable from the producer's JOIN_PAD padding.
        # It must NEVER grant a recovery — this is the concrete inversion the
        # contract forbids (rule 4). With the field still JOIN_PAD on :9888 the
        # sweep passes an empty measured set, so nothing reactivates.
        AgentAvailability.objects.create(
            email='yuki@shizuha.com', active=False, status='inactive',
            reason='auto: provider exhausted', updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {'yuki@shizuha.com': 0}, THRESHOLD, NOW)
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='yuki@shizuha.com').active)

    def test_recovery_requires_explicit_measured_provenance(self):
        # Borderline case: a 0 sample that IS present but NOT certified measured
        # (measured_streak_emails empty) must not reactivate; once the caller
        # certifies it measured, it does. Distinguishes absent vs JOIN_PAD-zero
        # vs measured-zero (contract rule 3).
        AgentAvailability.objects.create(
            email='nari@shizuha.com', active=False, status='inactive',
            reason='auto: provider exhausted', updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability(
            {'nari@shizuha.com': 0}, THRESHOLD, NOW,
            measured_streak_emails=set())
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='nari@shizuha.com').active)

        _, react = _reconcile_health_managed_availability(
            {'nari@shizuha.com': 0}, THRESHOLD, NOW,
            measured_streak_emails={'nari@shizuha.com'})
        self.assertEqual(react, ['nari@shizuha.com'])
        self.assertTrue(AgentAvailability.objects.get(email='nari@shizuha.com').active)

    def test_operator_force_off_never_auto_reactivated(self):
        # Operator deliberately disabled the agent; recovery must NOT flip it on.
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=False, status='inactive',
            reason='operator: on leave', updated_by='operator')
        _, react = _reconcile_health_managed_availability(
            {'sora@shizuha.com': 0}, THRESHOLD, NOW,
            measured_streak_emails={'sora@shizuha.com'})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='sora@shizuha.com').active)

    def test_down_but_not_sampled_not_reactivated(self):
        # A codex with no container is absent from the streak map (not confirmed
        # recovered) — leave our deactivation in place.
        AgentAvailability.objects.create(
            email='kei@shizuha.com', active=False, status='inactive',
            updated_by=HEALTH_SWEEP_ACTOR)
        _, react = _reconcile_health_managed_availability({}, THRESHOLD, NOW)
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='kei@shizuha.com').active)

    def test_stale_agent_made_deactivation_recovers_when_healthy(self):
        # The 2026-06-27 bug: an AGENT (not operator, not health-sweep) stamped a
        # stale incident deactivation that pinned a now-healthy agent unavailable
        # forever. Once sampled healthy (streak 0), it must auto-recover and pass to
        # health-sweep ownership.
        for by in ('ichi', 'shizuha-steward', 'self:ni@shizuha.com'):
            AgentAvailability.objects.create(
                email=f'{by}-agent@shizuha.com', active=False, status='inactive',
                reason='codex agent DOWN per old sweep', updated_by=by)
        streaks = {f'{by}-agent@shizuha.com': 0
                   for by in ('ichi', 'shizuha-steward', 'self:ni@shizuha.com')}
        _, react = _reconcile_health_managed_availability(
            streaks, THRESHOLD, NOW,
            measured_streak_emails=set(streaks))
        self.assertEqual(len(react), 3)
        for by in ('ichi', 'shizuha-steward', 'self:ni@shizuha.com'):
            row = AgentAvailability.objects.get(email=f'{by}-agent@shizuha.com')
            self.assertTrue(row.active)
            self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)

    def test_stale_agent_made_deactivation_NOT_recovered_while_exhausted(self):
        # A non-operator row whose agent is still exhausted (streak >= threshold) must
        # NOT be reactivated (ghost/exhausted protection via the streak gate).
        AgentAvailability.objects.create(
            email='busy@shizuha.com', active=False, status='inactive',
            reason='codex DOWN', updated_by='ichi')
        _, react = _reconcile_health_managed_availability(
            {'busy@shizuha.com': 9}, THRESHOLD, NOW,
            measured_streak_emails={'busy@shizuha.com'})
        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='busy@shizuha.com').active)

    def test_explicit_non_codex_provider_down_is_durable(self):
        AgentAvailability.objects.create(
            email='saki@shizuha.com', active=True, status='active',
            reason='old healthy sample', updated_by=HEALTH_SWEEP_ACTOR)

        deact, react = _reconcile_health_managed_availability(
            {'saki@shizuha.com': 0}, THRESHOLD, NOW,
            enabled_up=set(), provider_down={'saki@shizuha.com'})

        self.assertEqual(deact, ['saki@shizuha.com'])
        self.assertEqual(react, [])
        row = AgentAvailability.objects.get(email='saki@shizuha.com')
        self.assertFalse(row.active)
        self.assertIn('provider-health signal', row.reason)

    def test_non_codex_provider_row_does_not_recover_from_empty_streak(self):
        AgentAvailability.objects.create(
            email='nagi@shizuha.com', active=False, status='inactive',
            reason=(f'auto: provider unavailable — provider-health signal @ {NOW}'),
            updated_by=HEALTH_SWEEP_ACTOR)

        _, react = _reconcile_health_managed_availability(
            {'nagi@shizuha.com': 0}, THRESHOLD, NOW)

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='nagi@shizuha.com').active)

    def test_provider_down_blocks_recovery_of_an_existing_inactive_row(self):
        AgentAvailability.objects.create(
            email='goro@shizuha.com', active=False, status='inactive',
            reason='older automation deactivation', updated_by='legacy-sweep')

        _, react = _reconcile_health_managed_availability(
            {'goro@shizuha.com': 0}, THRESHOLD, NOW,
            provider_down={'goro@shizuha.com'})

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='goro@shizuha.com').active)

    def test_provider_row_does_NOT_recover_without_capability_signal(self):
        """PLS-748: provider-health 'available' + enablement alone must NOT clear a
        seat. The broker sidecar can be refusing model tokens (coordinator HTTP 404)
        while process-up/enabled/provider-health all look healthy — recovery must
        observe a productive turn (empty-turn streak == 0), not reachability."""
        AgentAvailability.objects.create(
            email='mika@shizuha.com', active=False, status='inactive',
            reason=(f'auto: provider unavailable — provider-health signal @ {NOW}'),
            updated_by=HEALTH_SWEEP_ACTOR)

        # Provider-health healthy + enabled, but NO capability signal (no streak-0
        # sample — the seat cannot mint a model token, so it produces no turn).
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'mika@shizuha.com'}, provider_down=set())

        self.assertEqual(react, [])
        row = AgentAvailability.objects.get(email='mika@shizuha.com')
        self.assertFalse(row.active)

    def test_provider_row_recovers_when_capability_signal_observed(self):
        """PLS-748 positive control: once the seat is observed producing output
        (empty-turn streak == 0), a provider-health row may recover."""
        AgentAvailability.objects.create(
            email='mika@shizuha.com', active=False, status='inactive',
            reason=(f'auto: provider unavailable — provider-health signal @ {NOW}'),
            updated_by=HEALTH_SWEEP_ACTOR)

        _, react = _reconcile_health_managed_availability(
            {'mika@shizuha.com': 0}, THRESHOLD, NOW,
            enabled_up={'mika@shizuha.com'}, provider_down=set())

        self.assertEqual(react, ['mika@shizuha.com'])
        row = AgentAvailability.objects.get(email='mika@shizuha.com')
        self.assertTrue(row.active)
        self.assertIn('provider-health signal available', row.reason)


# PLAT-6372: a self-deactivated row whose agent is live (enabled=1, up=1, fresh
# heartbeat) is auto-reactivated by the sweep even when the bridge emits NO
# empty_turn_streak sample (codex-rpc-specific; claude bridges never emit it).
# jun was stuck inactive 6d because the streak==0 gate required a sample.
class SelfDeactivatedLiveRecoveryTests(TestCase):
    def test_self_deactivated_live_agent_reactivates_without_streak_sample(self):
        AgentAvailability.objects.create(
            email='jun@shizuha.com', active=False, status='inactive',
            reason='empty-turn exhausted on gpt-5.6-sol; no distinct fallback configured',
            updated_by='self:jun@shizuha.com')

        # No empty_turn_streak sample at all (None), but the agent is live.
        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'jun@shizuha.com'}, provider_down=set())

        self.assertEqual(react, ['jun@shizuha.com'])
        row = AgentAvailability.objects.get(email='jun@shizuha.com')
        self.assertTrue(row.active)
        self.assertEqual(row.status, 'active')
        self.assertEqual(row.updated_by, HEALTH_SWEEP_ACTOR)
        self.assertIn('self-deactivation recovered', row.reason)

    def test_self_deactivated_live_agent_not_reactivated_when_provider_down(self):
        AgentAvailability.objects.create(
            email='jun@shizuha.com', active=False, status='inactive',
            reason='empty-turn exhausted', updated_by='self:jun@shizuha.com')

        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'jun@shizuha.com'},
            provider_down={'jun@shizuha.com'})

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='jun@shizuha.com').active)

    def test_self_deactivated_agent_not_in_enabled_up_not_reactivated(self):
        # Retired/ghost agent: no container, no sample, not in enabled_up.
        AgentAvailability.objects.create(
            email='ghost@shizuha.com', active=False, status='inactive',
            reason='retired', updated_by='self:ghost@shizuha.com')

        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up=set(), provider_down=set())

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='ghost@shizuha.com').active)

    def test_self_deactivated_agent_with_positive_streak_not_reactivated(self):
        # Agent is emitting empty turns (streak > 0) — still in an empty-turn
        # state, so a self-deactivation must not be cleared.
        AgentAvailability.objects.create(
            email='jun@shizuha.com', active=False, status='inactive',
            reason='empty-turn exhausted', updated_by='self:jun@shizuha.com')

        _, react = _reconcile_health_managed_availability(
            {'jun@shizuha.com': 2}, THRESHOLD, NOW,
            enabled_up={'jun@shizuha.com'}, provider_down=set())

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='jun@shizuha.com').active)

    def test_operator_force_off_never_auto_reactivated_by_self_live_path(self):
        # Operator toggle stays sacred even when the agent is live.
        AgentAvailability.objects.create(
            email='jun@shizuha.com', active=False, status='inactive',
            reason='operator parked', updated_by='operator')

        _, react = _reconcile_health_managed_availability(
            {}, THRESHOLD, NOW, enabled_up={'jun@shizuha.com'}, provider_down=set())

        self.assertEqual(react, [])
        self.assertFalse(AgentAvailability.objects.get(email='jun@shizuha.com').active)
