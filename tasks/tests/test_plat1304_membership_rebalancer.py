"""EVOL-20 Ph3 (PLAT-1304): membership rebalancer — move memberships, not tasks.

Signed HLD ab8fc292 §5. Proves: flag off = no-op (existing behavior unchanged);
flag on = capacity skew grants a time-boxed SECONDARY membership (not a task
move), cap ≤4 + ≥24h cooldown enforced, stale secondaries expire, cross-org /
disabled / already-member agents are ineligible, and every change is audited with
the triggering metric.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import (
    AgentProjectMembership,
    AgentProjectMembershipActivity,
    Item,
    Project,
    TeamRoster,
)
from tasks.services import membership_rebalancer as mr
from tasks.services.workflow_service import WorkflowService

ON = dict(PULSE_MEMBERSHIP_REBALANCER_ENABLED=True,
          PULSE_MEMBERSHIP_REBALANCE_SKEW=1,
          PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS=168,
          PULSE_MEMBERSHIP_COOLDOWN_HOURS=24)


class MembershipRebalancerTests(TestCase):
    def setUp(self):
        self.plat = Project.objects.create(name='Platform', key='PLAT', org_id=1,
                                            access_level='private', is_active=True)
        self.hive = Project.objects.create(name='Hive', key='HIVE', org_id=1,
                                           access_level='private', is_active=True)
        self.other_org = Project.objects.create(name='OtherOrg', key='OO', org_id=2,
                                                access_level='private', is_active=True)
        # PLAT has one primary member (loaded); HIVE holds the eligible candidate.
        self._mk(self.plat, 'busy@shizuha.com', 101, tier='primary')
        self.cand = self._mk_direct(self.hive, 'cand@shizuha.com', 102, tier='primary')
        # ready-queue depth in PLAT: several open tasks.
        for i in range(6):
            self._task(self.plat, i)

    def _mk(self, project, email, uid, tier='primary'):
        """Via the service (writes an audit row = a membership change 'now')."""
        from tasks.services.agent_membership_service import create_membership
        return create_membership(project, agent_user_email=email, agent_user_id=uid,
                                 tier=tier, reason='seed', actor_email='system@shizuha.com')

    def _mk_direct(self, project, email, uid, tier='primary', expires_at=None):
        """Direct ORM create — NO activity row (so cooldown lookup sees None)."""
        return AgentProjectMembership.objects.create(
            project=project, agent_user_email=email, agent_user_id=uid,
            tier=tier, active=True, expires_at=expires_at)

    def _task(self, project, i):
        return Item.objects.create(
            project=project, title=f't{i}', description='', status='open',
            workflow_name='autonomous-dev', item_type='task',
            created_by_id=1, created_by_email='r@shizuha.com')

    def _patches(self, available=True, load=0, cap=3):
        return [
            patch.object(WorkflowService, '_is_agent_available_for_assignment',
                         side_effect=lambda e: available),
            patch.object(WorkflowService, '_agent_active_load',
                         side_effect=lambda uid, email=None: load),
            patch.object(WorkflowService, '_wip_cap', return_value=cap),
        ]

    def _run(self, **kw):
        ps = self._patches(**kw)
        for p in ps:
            p.start()
        try:
            return mr.rebalance_memberships(actor_email='system@shizuha.com')
        finally:
            for p in ps:
                p.stop()

    # ── flag gating ──
    def test_flag_off_is_noop(self):
        # busy@ loaded, cand@ idle — but flag off → nothing happens.
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            out = mr.rebalance_memberships()
        self.assertFalse(out['enabled'])
        self.assertEqual(AgentProjectMembership.objects.filter(tier='secondary').count(), 0)

    @override_settings(**ON)
    def test_skew_grants_secondary_to_underloaded_candidate(self):
        # busy@ (PLAT primary) is full; cand@ (HIVE primary) is idle.
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            out = mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertEqual(len(out['added']), 1)
        added = out['added'][0]
        self.assertEqual(added['project'], 'PLAT')
        self.assertEqual(added['agent'], 'cand@shizuha.com')
        m = AgentProjectMembership.objects.get(project=self.plat, agent_user_email='cand@shizuha.com')
        self.assertEqual(m.tier, 'secondary')
        self.assertIsNotNone(m.expires_at)          # time-boxed
        self.assertTrue(m.active)

    @override_settings(**ON)
    def test_membership_change_is_audited_with_metric(self):
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            mr.rebalance_memberships(actor_email='system@shizuha.com')
        m = AgentProjectMembership.objects.get(project=self.plat, agent_user_email='cand@shizuha.com')
        act = AgentProjectMembershipActivity.objects.filter(membership=m, action='created').first()
        self.assertIsNotNone(act)
        self.assertIn('skew=', act.reason)          # triggering metric recorded
        self.assertIn('PLAT', act.reason)

    @override_settings(**ON)
    def test_no_task_moves(self):
        before = {i.id: i.assignee_email for i in Item.objects.all()}
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            mr.rebalance_memberships(actor_email='system@shizuha.com')
        after = {i.id: i.assignee_email for i in Item.objects.all()}
        self.assertEqual(before, after)             # not a single task moved

    @override_settings(**ON)
    def test_cap_of_four_enforced(self):
        # cand@ already holds 4 active memberships → not granted a 5th.
        for k in ('A', 'B', 'C', 'D'):
            p = Project.objects.create(name=k, key=f'K{k}', org_id=1,
                                       access_level='private', is_active=True)
            self._mk_direct(p, 'cand@shizuha.com', 102)
        # cand now has HIVE + 4 = 5 already active; make sure >=4 short-circuits.
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            out = mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertFalse(AgentProjectMembership.objects.filter(
            project=self.plat, agent_user_email='cand@shizuha.com').exists())
        self.assertEqual(len(out['added']), 0)

    @override_settings(**ON)
    def test_cooldown_blocks_recent_change(self):
        # a membership activity for cand@ 'now' → within 24h cooldown → ineligible.
        AgentProjectMembershipActivity.objects.create(
            membership=self.cand, action='created', actor_email='system@shizuha.com', reason='recent')
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            out = mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertEqual(len(out['added']), 0)

    @override_settings(**ON)
    def test_unavailable_agent_excluded(self):
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: False), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            out = mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertEqual(len(out['added']), 0)

    @override_settings(**ON)
    def test_cross_org_agent_not_granted(self):
        # An agent only in org 2 must never be granted a secondary in org-1 PLAT.
        self._mk_direct(self.other_org, 'oo-agent@shizuha.com', 202)
        def load(uid, email=None):
            return 3 if uid == 101 else 0
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=load), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertFalse(AgentProjectMembership.objects.filter(
            project=self.plat, agent_user_email='oo-agent@shizuha.com').exists())

    @override_settings(**ON)
    def test_stale_time_boxed_secondary_expires(self):
        past = timezone.now() - timezone.timedelta(hours=1)
        stale = self._mk_direct(self.hive, 'stale@shizuha.com', 303,
                                tier='secondary', expires_at=past)
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=lambda uid, email=None: 0), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            out = mr.rebalance_memberships(actor_email='system@shizuha.com')
        stale.refresh_from_db()
        self.assertFalse(stale.active)
        self.assertGreaterEqual(out['expired'], 1)

    @override_settings(**ON)
    def test_project_without_members_gets_no_secondary(self):
        empty = Project.objects.create(name='Empty', key='EMP', org_id=1,
                                       access_level='private', is_active=True)
        for i in range(6):
            self._task(empty, 100 + i)
        with patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=lambda e: True), \
             patch.object(WorkflowService, '_agent_active_load', side_effect=lambda uid, email=None: 0), \
             patch.object(WorkflowService, '_wip_cap', return_value=3):
            mr.rebalance_memberships(actor_email='system@shizuha.com')
        self.assertFalse(AgentProjectMembership.objects.filter(project=empty).exists())


class SweepGuardTests(TestCase):
    def setUp(self):
        self.plat = Project.objects.create(name='Platform', key='PLAT', org_id=1,
                                           access_level='private', is_active=True)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['live@shizuha.com', 'down@shizuha.com']],
                'is_active': True,
            },
        )

    def _sweep(self, roster, avail):
        """Run the sweep with the roster/availability/health layers patched."""
        from tasks.tasks import _sweep_rebalance_pods_impl

        def _user(email):
            e = (email or '').lower()
            return {'id': 200 if 'live' in e else 201, 'email': email}

        import os
        with patch('tasks.tasks._rebalance_pulse_api_health_gate', return_value=(True, '')), \
             patch.dict(os.environ, {'PULSE_HEALTH_EXPORTER_URL': ''}, clear=False), \
             patch.object(WorkflowService, '_pod_roster', return_value=roster), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: bool(avail(e))), \
             patch.object(WorkflowService, '_member_can_execute_from_status', return_value=True), \
             patch.object(WorkflowService, '_is_human_account', return_value=False), \
             patch.object(WorkflowService, '_disabled_agent_emails', return_value=set()), \
             patch.object(WorkflowService, '_cap_applies', return_value=False), \
             patch('tasks.user_service.user_service.get_user_by_email', side_effect=_user):
            return _sweep_rebalance_pods_impl()

    @override_settings(**ON)
    def test_sweep_uses_membership_mode_when_flag_on(self):
        out = self._sweep({'engineering': ['live@shizuha.com']}, lambda e: True)
        self.assertEqual(out.get('mode'), 'membership_rebalance')

    @override_settings(**ON)
    def test_failover_drain_still_evacuates_down_agent_under_flag_on(self):
        """kei P1 / aoi ruling: with the flag ON, a down/stopped agent's already-
        assigned work must STILL evacuate to a live peer — the failover drain runs
        unconditionally; only the capacity task-mover is replaced by memberships."""
        stranded = Item.objects.create(
            project=self.plat, title='stranded', description='', status='open',
            workflow_name='autonomous-dev', item_type='task',
            assignment_group='engineering',
            assignee_id=201, assignee_email='down@shizuha.com',
            created_by_id=1, created_by_email='r@shizuha.com')
        out = self._sweep(
            {'engineering': ['live@shizuha.com', 'down@shizuha.com']},
            lambda e: 'live' in (e or '').lower())  # down@ is unavailable
        stranded.refresh_from_db()
        # evacuated to the live peer despite the membership flag being ON.
        self.assertEqual(stranded.assignee_email, 'live@shizuha.com')
        self.assertEqual(out.get('mode'), 'membership_rebalance')


class MembershipRebalancerEnvWiringTests(TestCase):
    """PLAT-1307: prove the deploy-env -> settings binding actually works, not just
    override_settings — a real env var must reach `settings.PULSE_MEMBERSHIP_*`."""

    def _reload_settings(self, env):
        import importlib
        import os
        from unittest.mock import patch
        from taskmanager import settings as settings_module
        try:
            with patch.dict(os.environ, env, clear=False):
                importlib.reload(settings_module)
                return {
                    'enabled': settings_module.PULSE_MEMBERSHIP_REBALANCER_ENABLED,
                    'skew': settings_module.PULSE_MEMBERSHIP_REBALANCE_SKEW,
                    'ttl': settings_module.PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS,
                    'cooldown': settings_module.PULSE_MEMBERSHIP_COOLDOWN_HOURS,
                    '_env_int': settings_module._membership_int_env,
                }
        finally:
            importlib.reload(settings_module)  # restore module to the process env

    def test_env_enables_and_sets_knobs(self):
        # Cooldown override must be >= the signed 24h floor to be honored (review P2);
        # 48 exercises the honored-custom-value path.
        r = self._reload_settings({
            'PULSE_MEMBERSHIP_REBALANCER_ENABLED': 'true',
            'PULSE_MEMBERSHIP_REBALANCE_SKEW': '5',
            'PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS': '72',
            'PULSE_MEMBERSHIP_COOLDOWN_HOURS': '48',
        })
        self.assertIs(r['enabled'], True)
        self.assertEqual((r['skew'], r['ttl'], r['cooldown']), (5, 72, 48))

    def test_cooldown_cannot_be_lowered_below_signed_floor(self):
        # Review P (kai2574 re-review of #292): the >=24h anti-thrash cooldown is
        # an EVOL-20/HLD-signed guardrail. A positive-but-too-small value (12), zero,
        # or a negative must all fall back to the documented default 24 — the env
        # var must NOT be able to weaken the invariant below 24. Only Architecture
        # changing the HLD/contract may lower it.
        for bad in ('12', '1', '23', '0', '-1', '-24'):
            r = self._reload_settings({'PULSE_MEMBERSHIP_COOLDOWN_HOURS': bad})
            self.assertEqual(r['cooldown'], 24,
                             f"cooldown={bad} must floor to the signed 24h default, got {r['cooldown']}")
        # >= 24 is honored (raising the anti-thrash window is always safe).
        self.assertEqual(self._reload_settings({'PULSE_MEMBERSHIP_COOLDOWN_HOURS': '24'})['cooldown'], 24)
        self.assertEqual(self._reload_settings({'PULSE_MEMBERSHIP_COOLDOWN_HOURS': '72'})['cooldown'], 72)

    def test_defaults_when_env_absent(self):
        r = self._reload_settings({
            'PULSE_MEMBERSHIP_REBALANCER_ENABLED': '',
            'PULSE_MEMBERSHIP_REBALANCE_SKEW': '',
            'PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS': '',
            'PULSE_MEMBERSHIP_COOLDOWN_HOURS': '',
        })
        self.assertIs(r['enabled'], False)  # off unless explicitly enabled
        self.assertEqual((r['skew'], r['ttl'], r['cooldown']), (2, 168, 24))

    def test_bad_numeric_env_falls_back_to_default(self):
        r = self._reload_settings({'PULSE_MEMBERSHIP_REBALANCE_SKEW': 'not-a-number'})
        self.assertEqual(r['skew'], 2)  # documented default, no crash
        env_int = r['_env_int']
        self.assertEqual(env_int('PULSE_MEMBERSHIP_COOLDOWN_HOURS', 24), 24)  # absent -> default

    def test_semantically_invalid_env_falls_back_to_default(self):
        # Review P (ren): zero/negative guardrail values are semantically invalid
        # — cooldown=0 disables the >=24h anti-thrash gate, a negative TTL/skew is
        # nonsensical — so they must fall back to the signed default, not pass through.
        r = self._reload_settings({
            'PULSE_MEMBERSHIP_REBALANCE_SKEW': '0',
            'PULSE_MEMBERSHIP_SECONDARY_TTL_HOURS': '-5',
            'PULSE_MEMBERSHIP_COOLDOWN_HOURS': '0',
        })
        self.assertEqual((r['skew'], r['ttl'], r['cooldown']), (2, 168, 24))
        env_int = r['_env_int']
        # Explicit min_value floor: below the floor -> default; at/above -> honored.
        self.assertEqual(env_int('PULSE_MEMBERSHIP_COOLDOWN_HOURS', 24), 24)  # absent -> default
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'X_MEMBERSHIP_TEST': '-1'}):
            self.assertEqual(env_int('X_MEMBERSHIP_TEST', 24), 24)   # negative -> default
        with patch.dict(os.environ, {'X_MEMBERSHIP_TEST': '48'}):
            self.assertEqual(env_int('X_MEMBERSHIP_TEST', 24), 48)   # valid positive honored

    def test_settings_reach_the_rebalancer_accessors(self):
        # The service reads these via getattr(settings, ...); with the binding in
        # place, override_settings now flows through the accessors end-to-end.
        with override_settings(PULSE_MEMBERSHIP_REBALANCER_ENABLED=True,
                               PULSE_MEMBERSHIP_REBALANCE_SKEW=3):
            self.assertTrue(mr.rebalancer_enabled())
            self.assertEqual(mr._skew_threshold(), 3)
