"""PLS-8: per-project per-stage lead routing in _resolve_team_assignee.

Verifies the tiered-routing contract: a project that pins a stage lead routes to
that lead when it is available and under the lead load cap; otherwise the resolver
falls back to the existing least-loaded-member logic. Critical projects pin
Claude-tier leads, low-priority projects pin GLM-tier leads (operator policy
2026-06-10), so the same team routes to different leads per project.

Pure unit tests: all DB-touching dependencies (user lookup, availability, load)
are patched, so no database or running stack is required.
"""
from django.test import SimpleTestCase
from unittest.mock import patch, MagicMock

from tasks.services.workflow_service import WorkflowService

# uid map for the engineering roster + leads used below
_UIDS = {
    'ryo@shizuha.com': 1,
    'kai@shizuha.com': 2,
    'sara@shizuha.com': 3,
}


class ProjectStageLeadRoutingTests(SimpleTestCase):
    def setUp(self):
        # user_service is imported lazily inside _resolve_team_assignee via
        # `from ..user_service import user_service` → patch the source module.
        self.us_patcher = patch('tasks.user_service.user_service')
        self.mock_us = self.us_patcher.start()
        self.addCleanup(self.us_patcher.stop)
        self.mock_us.get_user_by_email.side_effect = lambda e: (
            {'id': _UIDS[e]} if e in _UIDS else None
        )

        # Availability + load are controllable per test.
        self.available = set(_UIDS)        # all available by default (by email)
        self.loads = {1: 5, 2: 5, 3: 5}    # uid -> workable load

        self.avail_patcher = patch.object(
            WorkflowService, '_is_agent_available_for_assignment',
            side_effect=lambda e: e in self.available,
        )
        self.avail_patcher.start()
        self.addCleanup(self.avail_patcher.stop)

        self.load_patcher = patch.object(
            WorkflowService, '_agent_workable_load',
            # PLS-23: _agent_workable_load now takes an optional second arg
            # (user_email) — accept it so the mock matches the new signature.
            side_effect=lambda uid, email=None: self.loads.get(int(uid), 0),
        )
        self.load_patcher.start()
        self.addCleanup(self.load_patcher.stop)

        # PLAT-390: the lead-preference + resolver now also read hands-on (active)
        # load. These tests model a single per-uid load value, so mirror it onto
        # _agent_active_load (active == inflight here) — preserves their semantics.
        self.active_load_patcher = patch.object(
            WorkflowService, '_agent_active_load',
            side_effect=lambda uid, email=None: self.loads.get(int(uid), 0),
        )
        self.active_load_patcher.start()
        self.addCleanup(self.active_load_patcher.stop)

        # PLS-8's static per-project stage-lead pins were emptied in code when the
        # config moved to the DB-backed ProjectTeamLead election (2026-06-11); the
        # routing mechanism still reads _PROJECT_STAGE_LEADS. These pure unit tests
        # own their fixture so they exercise the mechanism, not removed defaults.
        stage_leads = {
            'CON': {'engineering': 'ryo@shizuha.com'},  # critical → Claude-tier
            'CTX': {'engineering': 'ryo@shizuha.com'},
            'HKR': {'engineering': 'kai@shizuha.com'},  # low-pri → GLM-tier
            'SCS': {'engineering': 'kai@shizuha.com'},
        }
        self.stage_patcher = patch.object(
            WorkflowService, '_PROJECT_STAGE_LEADS', stage_leads)
        self.stage_patcher.start()
        self.addCleanup(self.stage_patcher.stop)

        # The engineering roster now lives only in the DB (TeamRoster, PLAT-431);
        # a SimpleTestCase has no DB, so patch _pod_roster to the members under test.
        self.roster_patcher = patch.object(
            WorkflowService, '_pod_roster',
            return_value={'engineering': list(_UIDS)},
        )
        self.roster_patcher.start()
        self.addCleanup(self.roster_patcher.stop)

    def _resolve(self, team='engineering', project_key=None, exclude=None):
        # These tests isolate the independent project-lead preference cap. The
        # production resolver now enables atomic roster WIP admission by
        # default; opt out here so that policy does not mask the lead-cap
        # fallback this unit test is specifically exercising.
        return WorkflowService._resolve_team_assignee(
            team, exclude_email=exclude, project_key=project_key,
            enforce_cap=False,
        )

    # ── Operator directive 2026-09-17: leads removed ──────────────────────────
    # Project/stage lead pins are inert: routing is uniformly least-loaded for
    # every project and priority. Equal loads tie-break by roster order (ryo).
    def test_critical_project_routes_to_claude_lead(self):
        # CTX is critical, but its lead pin is inert — equal loads tie-break
        # by roster order, NOT by lead preference.
        uid, email = self._resolve(team='engineering', project_key='CTX')
        self.assertEqual(email, 'ryo@shizuha.com')
        self.assertEqual(uid, 1)

    def test_lowpri_project_routes_to_least_loaded(self):
        # HKR's lead pin is inert: the least-loaded member wins regardless of
        # which member the (removed) lead policy pinned.
        self.loads = {1: 5, 2: 1, 3: 5}
        uid, email = self._resolve(team='engineering', project_key='HKR')
        self.assertEqual(email, 'kai@shizuha.com')
        self.assertEqual(uid, 2)

    def test_project_key_case_insensitive(self):
        _, email = self._resolve(team='engineering', project_key='ctx')
        self.assertEqual(email, 'ryo@shizuha.com')

    # ── AC: lead over cap → spill to least-loaded ─────────────────────────────
    def test_lead_over_cap_falls_back_to_least_loaded(self):
        # ryo (CTX engineering lead) at the lead cap (8) → not preferred; falls
        # back to the least-loaded available member (sara, load 1).
        self.loads = {1: 8, 2: 5, 3: 1}
        uid, email = self._resolve(team='engineering', project_key='CTX')
        self.assertEqual(email, 'sara@shizuha.com')
        self.assertEqual(uid, 3)

    def test_under_cap_lead_not_preferred_over_least_loaded(self):
        # Operator directive 2026-09-17: no lead preference at any load — the
        # least-loaded member wins even when the (inert) lead pin points at a
        # busier peer.
        self.loads = {1: 7, 2: 1, 3: 1}
        _, email = self._resolve(team='engineering', project_key='CTX')
        self.assertEqual(email, 'kai@shizuha.com')

    # ── AC: unavailable lead → fall back ──────────────────────────────────────
    def test_unavailable_lead_falls_back(self):
        self.available = {'kai@shizuha.com', 'sara@shizuha.com'}  # ryo down
        self.loads = {1: 1, 2: 5, 3: 2}
        uid, email = self._resolve(team='engineering', project_key='CTX')
        self.assertNotEqual(email, 'ryo@shizuha.com')
        self.assertEqual(email, 'sara@shizuha.com')  # least-loaded available

    # ── default-deny / fallback behaviors ─────────────────────────────────────
    def test_no_project_key_uses_least_loaded(self):
        self.loads = {1: 5, 2: 2, 3: 9}
        _, email = self._resolve(team='engineering', project_key=None)
        self.assertEqual(email, 'kai@shizuha.com')  # least-loaded, no lead pref

    def test_unlisted_project_uses_least_loaded(self):
        self.loads = {1: 5, 2: 2, 3: 9}
        _, email = self._resolve(team='engineering', project_key='ZZZ')
        self.assertEqual(email, 'kai@shizuha.com')

    def test_unlisted_team_for_project_uses_least_loaded(self):
        # Operator directive 2026-09-17: lead pins are inert — SCS and CON both
        # route by uniform least-loaded routing (equal loads → roster order).
        _, email_scs = self._resolve(team='engineering', project_key='SCS')
        _, email_con = self._resolve(team='engineering', project_key='CON')
        self.assertEqual(email_scs, 'ryo@shizuha.com')
        self.assertEqual(email_con, 'ryo@shizuha.com')

    def test_lead_that_is_author_is_excluded(self):
        # No self-review: if the pinned lead is the excluded author, fall back.
        self.loads = {1: 1, 2: 5, 3: 2}
        uid, email = self._resolve(
            team='engineering', project_key='CTX', exclude='ryo@shizuha.com',
        )
        self.assertNotEqual(email, 'ryo@shizuha.com')

    # ── pure helpers ──────────────────────────────────────────────────────────
    def test_project_stage_lead_helper(self):
        self.assertEqual(WorkflowService._project_stage_lead('CTX', 'engineering'), 'ryo@shizuha.com')
        self.assertEqual(WorkflowService._project_stage_lead('HKR', 'engineering'), 'kai@shizuha.com')
        self.assertIsNone(WorkflowService._project_stage_lead(None, 'engineering'))
        self.assertIsNone(WorkflowService._project_stage_lead('ZZZ', 'engineering'))
        # a team a project doesn't pin → None (default-deny → roster fallback)
        self.assertIsNone(WorkflowService._project_stage_lead('HKR', 'qa'))

    def test_pod_lead_load_cap_default(self):
        self.assertEqual(WorkflowService._pod_lead_load_cap(), 8)
