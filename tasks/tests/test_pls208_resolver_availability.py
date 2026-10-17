"""PLS-208 regressions: resolver fail-open vs stopped agents + alias-blind membership.

Defect 1: a stopped/disabled agent's unavailability lived ONLY in the ephemeral
Redis keys (pulse:agent_availability / pulse:agent_disabled, TTL ~600s). When
the sweep dies / the key expires / Redis restarts, _health_availability_map()
reads {} and _is_agent_available_for_assignment fails OPEN — so
_resolve_team_assignee handed NEW work to an agent that had been
operator-stopped for days (the sara repro). The fix persists the exporter's
shizuha_agent_enabled=0 signal into the durable AgentAvailability table
(health-sweep-owned rows), which _configured_agent_availability consults first,
so the stopped state survives every ephemeral-signal outage. PLAT-319 fail-open
remains for genuinely-unknown agents (exporter down, no durable row).

Defect 2: membership checks compared raw email strings, but rosters carry
@shizuha.com addresses while assignees are often stored under the
@agents.shizuha.io alias of the SAME agent — the #9532 PATCH-path re-resolve
misread members as non-members and churned. The PATCH path now uses the shared
alias-folding helper _email_in_current_team_active_tier.
"""
import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from tasks.models import AgentAvailability, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reconcile_health_managed_availability, sweep_agent_availability
from tasks.tests.base import AuthenticatedAPITestCase


def _urlopen_returning(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _reset_availability_caches():
    WorkflowService._health_avail_cache = None
    WorkflowService._disabled_emails_cache = None
    WorkflowService._wakeable_emails_cache = None
    WorkflowService._db_avail_cache = None


class StoppedAgentFailOpenTests(TestCase):
    """A durable stopped row must beat the PLAT-319 fail-open path."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def _stopped_row(self, email="sara@shizuha.com"):
        return AgentAvailability.objects.create(
            email=email, active=False, status="inactive",
            reason="auto: agent stopped/disabled — shizuha_agent_enabled=0",
            updated_by="health-sweep",
        )

    def test_stopped_agent_unavailable_when_health_map_empty(self):
        """Empty health map == sweep dead / key expired (the fail-open path).
        The durable row must still mark the stopped agent unavailable."""
        self._stopped_row()
        with patch.object(WorkflowService, "_health_availability_map", return_value={}), \
                patch.object(WorkflowService, "_redis_client") as rc:
            rc.return_value.get.return_value = None
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment("sara@shizuha.com"))
            # The @agents alias is the same identity and must read the same verdict.
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment("sara@agents.shizuha.io"))

    def test_unknown_agent_still_fails_open_when_health_map_empty(self):
        """PLAT-319 semantics preserved: exporter down + no durable knowledge
        about the agent -> fail open (do not freeze routing org-wide)."""
        with patch.object(WorkflowService, "_health_availability_map", return_value={}), \
                patch.object(WorkflowService, "_redis_client") as rc:
            rc.return_value.get.return_value = None
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment("kai@shizuha.com"))

    def test_resolver_skips_stopped_roster_member_under_failopen(self):
        """The PLS-208 repro: _resolve_team_assignee must never return a
        stopped roster member, even on the fail-open (empty health map) path."""
        self._stopped_row("sara@shizuha.com")

        def _user(email):
            return {"id": {"sara@shizuha.com": 11, "kai@shizuha.com": 12}[email]}

        with patch.object(WorkflowService, "_health_availability_map", return_value={}), \
                patch.object(WorkflowService, "_redis_client") as rc, \
                patch.object(WorkflowService, "assignable_teams",
                             return_value={"engineering": ["sara@shizuha.com", "kai@shizuha.com"]}), \
                patch.object(WorkflowService, "_pod_roster",
                             return_value={"engineering": ["sara@shizuha.com", "kai@shizuha.com"]}), \
                patch.object(WorkflowService, "_junior_emails", return_value=set()), \
                patch.object(WorkflowService, "_cap_applies", return_value=False), \
                patch.object(WorkflowService, "_agent_active_load", return_value=0), \
                patch.object(WorkflowService, "_agent_workable_load", return_value=0), \
                patch("tasks.user_service.user_service.get_user_by_email", side_effect=_user):
            rc.return_value.get.return_value = None
            uid, email = WorkflowService._resolve_team_assignee(
                "engineering", enforce_cap=True)
        self.assertEqual(email, "kai@shizuha.com")
        self.assertEqual(uid, 12)


class ReconcileStoppedPropagationTests(TestCase):
    """The sweep persists enabled=0 agents durably and reactivates on re-enable."""

    def test_stopped_agent_gets_durable_inactive_row(self):
        deactivated, _ = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:00:00Z", stopped={"sara@shizuha.com"})
        self.assertEqual(deactivated, ["sara@shizuha.com"])
        row = AgentAvailability.objects.get(email="sara@shizuha.com")
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, "health-sweep")
        self.assertIn("shizuha_agent_enabled=0", row.reason)
        # Idempotent: a second sweep does not re-deactivate / churn the row.
        deactivated2, _ = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:05:00Z", stopped={"sara@shizuha.com"})
        self.assertEqual(deactivated2, [])

    def test_stopped_overrides_stale_active_row(self):
        AgentAvailability.objects.create(
            email="sara@shizuha.com", active=True, status="active",
            reason="stale force-active", updated_by="operator")
        deactivated, _ = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:00:00Z", stopped={"sara@shizuha.com"})
        self.assertEqual(deactivated, ["sara@shizuha.com"])
        row = AgentAvailability.objects.get(email="sara@shizuha.com")
        self.assertFalse(row.active)

    def test_reenabled_agent_reactivates_health_sweep_row(self):
        AgentAvailability.objects.create(
            email="sara@shizuha.com", active=False, status="inactive",
            reason="auto: agent stopped/disabled — shizuha_agent_enabled=0",
            updated_by="health-sweep")
        _, reactivated = _reconcile_health_managed_availability(
            {"sara@shizuha.com": 0}, 3, "2026-07-02T00:00:00Z",
            enabled_up={"sara@shizuha.com"})
        self.assertEqual(reactivated, ["sara@shizuha.com"])
        row = AgentAvailability.objects.get(email="sara@shizuha.com")
        self.assertTrue(row.active)
        self.assertIn("re-enabled", row.reason)

    def test_reenabled_agent_does_NOT_reactivate_without_capability_signal(self):
        """PLS-748: an enablement flag alone must not clear a health-sweep row. The
        operator re-enabling the toggle does not prove the seat can mint a model
        token and serve a turn; recovery must observe a productive turn (empty-turn
        streak == 0)."""
        AgentAvailability.objects.create(
            email="sara@shizuha.com", active=False, status="inactive",
            reason="auto: agent stopped/disabled — shizuha_agent_enabled=0",
            updated_by="health-sweep")
        _, reactivated = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:00:00Z", enabled_up={"sara@shizuha.com"})
        self.assertEqual(reactivated, [])
        row = AgentAvailability.objects.get(email="sara@shizuha.com")
        self.assertFalse(row.active)

    def test_self_reported_outage_cleared_when_agent_live_without_streak_sample(self):
        # PLAT-6372: a self-deactivated row (updated_by='self:...') whose agent
        # is live — enabled=1, up=1/process_up=1, fresh heartbeat (enabled_up),
        # not provider-down, no empty-turn signal — IS auto-reactivated even
        # when the bridge emits no empty_turn_streak sample. The old streak==0
        # gate alone left such rows inactive forever (jun stuck 6d).
        AgentAvailability.objects.create(
            email="revi@shizuha.com", active=False, status="inactive",
            reason="empty-turn exhausted on gpt-5.6-sol; no distinct fallback configured",
            updated_by="self:revi@shizuha.com")
        _, reactivated = _reconcile_health_managed_availability(
            {}, 3, "2026-07-17T06:30:00Z", enabled_up={"revi@shizuha.com"})
        self.assertEqual(reactivated, ["revi@shizuha.com"])
        row = AgentAvailability.objects.get(email="revi@shizuha.com")
        self.assertTrue(row.active)
        self.assertIn("self-deactivation recovered", row.reason)

    def test_operator_row_never_auto_reactivated(self):
        AgentAvailability.objects.create(
            email="held@shizuha.com", active=False, status="on_leave",
            reason="operator hold", updated_by="operator")
        _, reactivated = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:00:00Z", enabled_up={"held@shizuha.com"})
        self.assertEqual(reactivated, [])
        self.assertFalse(AgentAvailability.objects.get(email="held@shizuha.com").active)

    def test_reenable_does_not_fight_same_sweep_stop(self):
        """An email in both sets (contradictory samples) stays deactivated."""
        deactivated, reactivated = _reconcile_health_managed_availability(
            {}, 3, "2026-07-02T00:00:00Z",
            stopped={"sara@shizuha.com"}, enabled_up={"sara@shizuha.com"})
        self.assertEqual(deactivated, ["sara@shizuha.com"])
        self.assertEqual(reactivated, [])
        self.assertFalse(AgentAvailability.objects.get(email="sara@shizuha.com").active)


class SweepEnabledMetricPropagationTests(TestCase):
    """End-to-end: an enabled=0 exporter sample produces the durable row."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def test_sweep_persists_enabled_zero_to_db(self):
        metrics = (
            'shizuha_agent_process_up{agent="kai",backend="claude"} 1\n'
            'shizuha_agent_enabled{agent="kai"} 1\n'
            'shizuha_agent_enabled{agent="sara"} 0\n'
        )
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        _rget = MagicMock()
        _rget.text = metrics
        _rget.status_code = 200
        _rget.raise_for_status.return_value = None
        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
                patch("requests.get", return_value=_rget), \
                patch("tasks.services.workflow_service.WorkflowService._redis_client",
                      return_value=redis_mock), \
                patch.dict("os.environ", {
                    "PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics",
                    "PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED": "0",
                }):
            result = sweep_agent_availability.run()
        self.assertTrue(result["ok"])
        row = AgentAvailability.objects.get(email="sara@shizuha.com")
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, "health-sweep")


class SweepJoinPadStreakFilterTests(TestCase):
    """PLS-741/PLS-869: the sweep must never feed producer JOIN_PAD padding
    (placeholder="1" label) into the empty-turn-streak availability gate — in
    either direction — while still honoring real MEASUREMENT samples (Phase C:
    values are derived, not presence-only)."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def _run_sweep(self, metrics):
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        _rget = MagicMock()
        _rget.text = metrics
        _rget.status_code = 200
        _rget.raise_for_status.return_value = None
        with patch("urllib.request.urlopen", _urlopen_returning(metrics)), \
                patch("requests.get", return_value=_rget), \
                patch("tasks.services.workflow_service.WorkflowService._redis_client",
                      return_value=redis_mock), \
                patch.dict("os.environ", {
                    "PULSE_HEALTH_EXPORTER_URL": "http://exporter.test/metrics",
                    "PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED": "0",
                }):
            return sweep_agent_availability.run()

    def test_join_pad_empty_turn_streak_never_deactivates(self):
        # All empty_turn_streak series are JOIN_PAD (placeholder="1"): the sweep
        # filters them out, so the streak gate sees zero real samples and NO
        # durable row is created — padding must never be read as exhaustion.
        metrics = (
            'shizuha_agent_process_up{agent="kai",backend="claude"} 1\n'
            'shizuha_agent_enabled{agent="kai"} 1\n'
            'shizuha_agent_empty_turn_streak{agent="kai",placeholder="1"} 0\n'
        )
        result = self._run_sweep(metrics)
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("auto_deactivated"), [])
        self.assertEqual(result.get("auto_reactivated"), [])
        self.assertFalse(
            AgentAvailability.objects.filter(email="kai@shizuha.com").exists())

    def test_join_pad_empty_turn_streak_never_reactivates(self):
        # A pre-existing inactive row must NOT be reactivated by a JOIN_PAD
        # (placeholder="1") zero sample — padding is not a measured recovery
        # grant (the exact inversion PLS-741 fixed).
        AgentAvailability.objects.create(
            email="rien@shizuha.com", active=False, status="inactive",
            reason="auto: provider exhausted", updated_by="health-sweep")
        metrics = (
            'shizuha_agent_process_up{agent="rien",backend="claude"} 1\n'
            'shizuha_agent_enabled{agent="rien"} 1\n'
            'shizuha_agent_empty_turn_streak{agent="rien",placeholder="1"} 0\n'
        )
        result = self._run_sweep(metrics)
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("auto_reactivated"), [])
        row = AgentAvailability.objects.get(email="rien@shizuha.com")
        self.assertFalse(row.active)

    def test_measured_empty_turn_streak_still_deactivates(self):
        # A real MEASUREMENT sample (no placeholder label) >= threshold STILL
        # deactivates — the fail-closed fix must not blind the gate to genuine
        # measurement. Non-zero/derived, not just "series present."
        metrics = (
            'shizuha_agent_process_up{agent="kai",backend="codex"} 1\n'
            'shizuha_agent_enabled{agent="kai"} 1\n'
            'shizuha_agent_empty_turn_streak{agent="kai"} 5\n'
        )
        result = self._run_sweep(metrics)
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("auto_deactivated"), ["kai@shizuha.com"])
        row = AgentAvailability.objects.get(email="kai@shizuha.com")
        self.assertFalse(row.active)
        self.assertEqual(row.updated_by, "health-sweep")
        self.assertIn("empty-turn streak 5", row.reason)




class HibernatedWakeableRoutingTests(TestCase):
    """PLAT-5861: typed hibernation is routable but never live capacity."""

    def setUp(self):
        super().setUp()
        _reset_availability_caches()

    def test_sweep_rearms_prior_stopped_row_and_publishes_wakeable(self):
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=False, status='inactive',
            reason='auto: agent stopped/disabled — shizuha_agent_enabled=0',
            updated_by='health-sweep',
        )
        metrics = (
            'shizuha_agent_process_up{agent="sora"} 0\n'
            'shizuha_agent_enabled{agent="sora"} 0\n'
            'shizuha_agent_lifecycle_state{agent="sora",state="hibernated"} 1\n'
            'shizuha_agent_provider_healthy{agent="sora"} 1\n'
        )
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        response = MagicMock(text=metrics, status_code=200)
        response.raise_for_status.return_value = None
        with patch('requests.get', return_value=response), \
                patch.object(WorkflowService, '_redis_client', return_value=redis_mock), \
                patch.dict('os.environ', {
                    'PULSE_HEALTH_EXPORTER_URL': 'http://exporter.test/metrics',
                    'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
                }):
            result = sweep_agent_availability.run()

        self.assertEqual(result['hibernated'], ['sora@shizuha.com'])
        self.assertEqual(result['wakeable'], ['sora@shizuha.com'])
        self.assertNotIn('sora@shizuha.com', result['disabled'])
        row = AgentAvailability.objects.get(email='sora@shizuha.com')
        self.assertTrue(row.active)
        self.assertIn('wakeable for new assignment', row.reason)
        writes = {call.args[0]: json.loads(call.args[2]) for call in redis_mock.setex.call_args_list}
        self.assertEqual(writes['pulse:agent_wakeable'], ['sora@shizuha.com'])
        self.assertEqual(writes['pulse:agent_availability']['sora@shizuha.com'], False)

    def test_hibernated_is_routable_while_operator_stopped_is_not(self):
        with patch.object(
            WorkflowService, '_health_availability_map',
            return_value={'sora@shizuha.com': False},
        ), patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'sora@shizuha.com'},
        ), patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_db_agent_availability', return_value=None,
        ), patch.object(
            WorkflowService, '_configured_email_set', return_value=set(),
        ):
            # Hibernated is not running capacity.  Only an explicit
            # new-assignment check may consume the wakeable signal.
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment(
                    'sora@shizuha.com', require_live_evidence=True,
                )
            )
            self.assertTrue(
                WorkflowService._is_agent_available_for_assignment(
                    'sora@shizuha.com',
                    require_live_evidence=True,
                    allow_wakeable=True,
                )
            )

        with patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'aoi@shizuha.com'},
        ), patch.object(
            WorkflowService, '_disabled_agent_emails',
            return_value={'aoi@shizuha.com'},
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment(
                    'aoi@shizuha.com',
                    require_live_evidence=True,
                    allow_wakeable=True,
                )
            )

        with patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'sora@shizuha.com'},
        ), patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_configured_agent_availability',
            return_value=False,
        ) as configured:
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment(
                    'sora@shizuha.com',
                    require_live_evidence=True,
                    allow_wakeable=True,
                )
            )
        configured.assert_called_once_with(
            'sora@shizuha.com', ignore_health=True,
        )

    def test_operator_stopped_and_provider_dead_hibernated_are_not_wakeable(self):
        metrics = (
            'shizuha_agent_process_up{agent="sora"} 0\n'
            'shizuha_agent_enabled{agent="sora"} 0\n'
            'shizuha_agent_lifecycle_state{agent="sora",state="hibernated"} 1\n'
            'shizuha_agent_provider_healthy{agent="sora"} 0\n'
            'shizuha_agent_process_up{agent="aoi"} 0\n'
            # Deliberately contradictory compatibility evidence: typed
            # operator_stopped must fail closed even if enabled=1 is stale.
            'shizuha_agent_enabled{agent="aoi"} 1\n'
            'shizuha_agent_lifecycle_state{agent="aoi",state="operator_stopped"} 1\n'
            'shizuha_agent_provider_healthy{agent="aoi"} 1\n'
        )
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        response = MagicMock(text=metrics, status_code=200)
        response.raise_for_status.return_value = None
        with patch('requests.get', return_value=response), \
                patch.object(WorkflowService, '_redis_client', return_value=redis_mock), \
                patch.dict('os.environ', {
                    'PULSE_HEALTH_EXPORTER_URL': 'http://exporter.test/metrics',
                    'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
                    'PULSE_HEALTH_DEMOTE_AFTER_SEC': '0',
                }):
            result = sweep_agent_availability.run()

        self.assertEqual(result['hibernated'], ['sora@shizuha.com'])
        self.assertEqual(result['provider_down'], ['sora@shizuha.com'])
        self.assertEqual(result['wakeable'], [])
        # ``disabled`` reports seats newly forced down during this scrape; the
        # complete operator-stop set is the durable Redis routing signal below.
        self.assertNotIn('aoi@shizuha.com', result['wakeable'])
        writes = {
            call.args[0]: json.loads(call.args[2])
            for call in redis_mock.setex.call_args_list
        }
        self.assertEqual(writes['pulse:agent_wakeable'], [])
        self.assertEqual(
            writes['pulse:agent_disabled'],
            ['aoi@shizuha.com'],
        )

    def test_fresh_wakeable_signal_supersedes_only_legacy_sweep_off_row(self):
        AgentAvailability.objects.create(
            email='sora@shizuha.com', active=False, status='inactive',
            reason='auto: agent stopped/disabled — shizuha_agent_enabled=0',
            updated_by='health-sweep',
        )
        AgentAvailability.objects.create(
            email='aoi@shizuha.com', active=False, status='inactive',
            reason='operator maintenance hold', updated_by='operator',
        )
        with patch.object(
            WorkflowService, '_health_availability_map',
            return_value={
                'sora@shizuha.com': False,
                'aoi@shizuha.com': False,
            },
        ), patch.object(
            WorkflowService, '_wakeable_agent_emails',
            return_value={'sora@shizuha.com'},
        ), patch.object(
            WorkflowService, '_service_denied_agent_emails', return_value=set(),
        ):
            self.assertIsNone(
                WorkflowService._db_agent_availability('sora@shizuha.com')
            )
            self.assertFalse(
                WorkflowService._db_agent_availability('aoi@shizuha.com')
            )
            disabled = WorkflowService._disabled_agent_emails()

        self.assertNotIn('sora@shizuha.com', disabled)
        self.assertIn('aoi@shizuha.com', disabled)



class AliasFoldedTeamChangeTests(AuthenticatedAPITestCase):
    """#9532 follow-up: the PATCH-path membership check must alias-fold."""

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    @patch('tasks.services.workflow_service.WorkflowService._active_team_candidate_emails',
           return_value=['banto@shizuha.com', 'aoi@shizuha.com'])
    def test_alias_assignee_recognized_as_member(self, _members, resolver):
        # Assignee stored under the @agents alias of a rostered member: the
        # membership check must fold aliases and KEEP them (no churny re-resolve).
        project = Project.objects.create(
            name='Alias membership test',
            key='P208',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [['banto@shizuha.com']],
            },
        )
        it = Item.objects.create(
            project=project,
            title='alias member target', mode='task', item_type='task.generic',
            status='open', assignment_group='engineering',
            assignee_email='banto@agents.shizuha.io')
        # Model the authoritative single-Team roster move before Pulse changes
        # the task's Team. The alias-folding assertion is about retaining the
        # same identity after that move, not dual-Team membership.
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='architecture',
            defaults={
                'organization_id': 1,
                'tiers': [['banto@shizuha.com', 'aoi@shizuha.com']],
            },
        )
        r = self.client.patch(f'/api/items/{it.id}/',
                              {'assignment_group': 'architecture'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        it.refresh_from_db()
        self.assertEqual(it.assignee_email, 'banto@agents.shizuha.io')
        resolver.assert_not_called()
