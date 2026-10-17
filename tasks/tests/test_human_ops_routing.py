"""PLAT-724 / PLS-74 — human-ops escalation routing tests.

Verifies:
1. ceo-decisions/leads/triage aliases resolve to human-ops (not admin-ops).
2. _resolve_team_assignee returns (None, None) for human-ops by default
   (PLS-16 guard) and returns the operator when allow_operator=True (PLS-74).
3. The TeamRoster alias table and WorkflowService._canonical_team_name
   agree on the same canonical names.
4. pulse_server.py _canonical_team_slug maps aliases consistently.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import TeamRoster
from tasks.services.workflow_service import WorkflowService


OPERATOR_EMAIL = 'operator@example.test'

_HUMAN_OPS_ROSTER = {
    'human-ops': [[OPERATOR_EMAIL]],
}


class TeamAliasResolutionTests(TestCase):
    """leads/triage still resolve to human-ops; PLS-539: ceo-decisions must NOT
    (it is a real org-scoped decision team, never the physical-only lane)."""

    def test_workflow_service_aliases_to_human_ops(self):
        for alias in ('leads', 'triage'):
            self.assertEqual(
                WorkflowService._canonical_team_name(alias),
                'human-ops',
                f'{alias!r} must canonicalize to human-ops in WorkflowService',
            )

    def test_pls539_ceo_decisions_never_aliases_to_human_ops(self):
        """PLS-539 (I1): ceo-decisions must canonicalize to ITSELF in every alias
        table — never rewritten to the physical-only human-ops lane."""
        from tasks.models import TeamRoster
        self.assertEqual(WorkflowService._canonical_team_name('ceo-decisions'), 'ceo-decisions')
        self.assertEqual(
            TeamRoster._CANONICAL_ALIASES.get('ceo-decisions', 'ceo-decisions'),
            'ceo-decisions')

    def test_workflow_service_admin_ops_unchanged(self):
        self.assertEqual(WorkflowService._canonical_team_name('admin-ops'), 'admin-ops')
        self.assertEqual(WorkflowService._canonical_team_name('admin_ops'), 'admin-ops')

    def test_team_roster_aliases_to_human_ops(self):
        for alias in ('leads', 'triage'):
            canon = TeamRoster._CANONICAL_ALIASES.get(alias)
            self.assertEqual(
                canon,
                'human-ops',
                f'TeamRoster._CANONICAL_ALIASES[{alias!r}] must be human-ops',
            )
        # PLS-539 (I1): ceo-decisions is NOT in the alias table (resolves to self).
        self.assertNotIn('ceo-decisions', TeamRoster._CANONICAL_ALIASES)

    def test_security_triage_still_security(self):
        self.assertEqual(WorkflowService._canonical_team_name('security-triage'), 'security')


class HumanOpsResolverTests(TestCase):
    """_resolve_team_assignee gates the operator correctly (PLS-16 + PLS-74)."""

    def _mock_roster(self):
        return mock.patch.object(
            WorkflowService, '_pod_roster', return_value=_HUMAN_OPS_ROSTER
        )

    def _mock_availability(self, available=True):
        return mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=available
        )

    def _mock_load(self):
        return mock.patch.object(
            WorkflowService, '_route_load_key', return_value=(0, 0)
        )

    def _mock_user_service(self):
        # workflow_service imports user_service at call-time
        # (`from ..user_service import user_service`), so patch the canonical
        # module attribute rather than a name that no longer exists on
        # workflow_service at import time.
        return mock.patch(
            'tasks.user_service.user_service',
            **{'get_user_by_email.return_value': {'id': 1, 'email': OPERATOR_EMAIL}},
        )

    def _mock_empty_roster(self):
        # No active DB roster row for the team, so the operator can only be
        # reached via the last-resort lead fallback — the one path PLAT-666 still
        # CoS-gates (the PLS-16 guard). (A rostered operator IS assignable to his
        # own human-ops physical-work queue; see test_pls74/test_operator_rostered.)
        return mock.patch.object(WorkflowService, '_pod_roster', return_value={})

    def _mock_lead_operator(self):
        return mock.patch.object(
            WorkflowService, '_resolve_team_lead_email', return_value=OPERATOR_EMAIL
        )

    def test_pls16_guard_fires_by_default(self):
        """A NON-rostered operator reached as a last-resort lead fallback is never
        auto-assigned without allow_operator (PLS-16, narrowed by PLAT-666)."""
        with self._mock_empty_roster(), self._mock_availability(), self._mock_load(), \
             self._mock_user_service(), self._mock_lead_operator():
            uid, email = WorkflowService._resolve_team_assignee('human-ops')
        self.assertIsNone(uid)
        self.assertIsNone(email)

    def test_pls74_exception_returns_operator(self):
        """With allow_operator=True the rostered operator is elected (PLS-74)."""
        with self._mock_roster(), self._mock_availability(), self._mock_load(), self._mock_user_service():
            uid, email = WorkflowService._resolve_team_assignee('human-ops', allow_operator=True)
        self.assertEqual(email, OPERATOR_EMAIL)

    def test_pls747_empty_roster_operator_capable_lanes_share_fallback_policy(self):
        """Routing-path regression for ADR-0058.

        No TeamRoster row is seeded: both declared operator-capable lanes reach
        the operator only through the lane-policy fallback. The shared predicate
        must authorize both arms; a populated roster would mask the policy
        boundary via ``_has_active_db_row``.
        """
        for team_name in ('human-ops', 'ceo-decisions'):
            with self.subTest(team_name=team_name), self._mock_empty_roster(), \
                 self._mock_user_service():
                uid, email = WorkflowService._resolve_team_assignee(
                    team_name,
                    allow_operator=WorkflowService._is_operator_capable_team(
                        team_name,
                    ),
                )
                self.assertEqual(uid, 1)
                self.assertEqual(email, OPERATOR_EMAIL)

    def test_operator_rostered_in_human_ops_is_assignable(self):
        """PLAT-666: when the operator is a legitimately ROSTERED human-ops member,
        routing to him is correct (his physical-work queue), even without
        allow_operator — not work-dumping."""
        with self._mock_roster(), self._mock_availability(), self._mock_load(), self._mock_user_service():
            uid, email = WorkflowService._resolve_team_assignee('human-ops')
        self.assertEqual(email, OPERATOR_EMAIL)

    def test_ceo_decisions_is_not_the_human_ops_lane(self):
        """PLS-539 (I1): ceo-decisions is a real org-scoped decision team, NOT the
        physical-only human-ops lane — it must not canonicalize to human-ops, so
        the human-ops PLS-16 non-rostered guard does not govern it."""
        self.assertEqual(WorkflowService._canonical_team_name('ceo-decisions'), 'ceo-decisions')
        self.assertNotEqual(WorkflowService._canonical_team_name('ceo-decisions'), 'human-ops')

    def test_alias_leads_pls16_guard(self):
        with self._mock_empty_roster(), self._mock_availability(), self._mock_load(), \
             self._mock_user_service(), self._mock_lead_operator():
            uid, email = WorkflowService._resolve_team_assignee('leads')
        self.assertIsNone(uid)
        self.assertIsNone(email)


class HumanOpsBlockerSweepTests(TestCase):
    """sweep_stale_human_ops_blockers re-assigns unassigned human-ops tasks."""

    def test_sweep_assigns_operator_to_unassigned_human_ops_task(self):
        from django.contrib.auth import get_user_model
        from tasks.models import Item, Project
        from tasks.tasks import sweep_stale_human_ops_blockers
        from django.utils import timezone
        from datetime import timedelta

        User = get_user_model()
        project = Project.objects.create(name='P', key='SHP', org_id=1)

        item = Item.objects.create(
            title='Operator gated: approve billing',
            project=project,
            assignment_group='human-ops',
            status='open',
            mode='task',
            workflow_name='autonomous-dev',
        )
        # created_at is auto_now_add, so it can't be set via create(); and a task
        # Item with no workflow would coerce status 'open'→'todo'. Pin the workflow
        # (above) and backdate created_at via update() so the item matches the
        # sweep's (status in [open,pending], created_at <= cutoff) filter.
        Item.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timedelta(hours=5))

        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(1, OPERATOR_EMAIL),
        ) as mock_resolve, mock.patch.object(
            Item, 'assign',
        ) as mock_assign:
            result = sweep_stale_human_ops_blockers.apply(args=[], kwargs={'hours': 4})

        mock_resolve.assert_called_once_with(
            'human-ops', allow_operator=True, project_key='SHP', item=item,
        )
        mock_assign.assert_called_once()
        self.assertEqual(result.result['resolved'], 1)

    def test_sweep_skips_already_processed_items(self):
        from tasks.models import Item, Project
        from tasks.tasks import sweep_stale_human_ops_blockers
        from django.utils import timezone
        from datetime import timedelta

        project = Project.objects.create(name='P2', key='SHP2', org_id=1)
        item = Item.objects.create(
            title='Already swept',
            project=project,
            assignment_group='human-ops',
            status='open',
            mode='task',
            workflow_name='autonomous-dev',
            metadata={'human_ops_sweep_resolved': True},
        )
        # created_at is auto_now_add; backdate via update() so the item is inside
        # the sweep's cutoff and reaches the already-processed skip branch.
        Item.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timedelta(hours=10))

        with mock.patch.object(
            WorkflowService, '_resolve_team_assignee',
        ) as mock_resolve:
            result = sweep_stale_human_ops_blockers.apply(args=[], kwargs={'hours': 4})

        mock_resolve.assert_not_called()
        self.assertEqual(result.result['skipped'], 1)


class HumanAvailabilityRebalanceTests(TestCase):
    """Humans are not agents: the pod rebalancer must not drain their work as
    offline, disabled, or ghost-owned just because they have no runtime."""

    def test_explicit_human_assignment_is_not_failed_over_as_ghost_owner(self):
        from django.contrib.auth import get_user_model
        from tasks.models import Comment, Item, Project, ProjectMembership
        from tasks.tasks import sweep_rebalance_pods

        User = get_user_model()
        operator = User.objects.create_user(
            username='hritik', email=OPERATOR_EMAIL, password='x')
        nao = User.objects.create_user(
            username='nao', email='nao@shizuha.com', password='x')
        project = Project.objects.create(name='Books', key='BKSG', org_id=1)
        ProjectMembership.objects.create(
            project=project,
            user_id=operator.id,
            user_email=OPERATOR_EMAIL,
            role='member',
        )
        with mock.patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            item = Item.objects.create(
                title='Provide operator-only accounting source evidence',
                project=project,
                assignee_id=operator.id,
                assignee_email=OPERATOR_EMAIL,
                status='todo',
                mode='task',
            )

        def _user_by_email(email):
            normalized = (email or '').strip().lower()
            if normalized == 'nao@shizuha.com':
                return {'id': nao.id, 'email': nao.email}
            if normalized == OPERATOR_EMAIL:
                return {'id': operator.id, 'email': operator.email}
            return None

        with mock.patch.object(
            WorkflowService, '_pod_roster',
            return_value={'accounting': ['nao@shizuha.com']},
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_user_by_email,
        ), mock.patch.dict(
            'os.environ',
            {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'},
            clear=False,
        ):
            result = sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, OPERATOR_EMAIL)
        self.assertEqual(item.assignee_id, operator.id)
        self.assertEqual(result.result['moves'], 0)
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__icontains='Auto-failover',
            ).exists()
        )

    def test_group_only_legacy_assignment_drains_to_live_team_member(self):
        from django.contrib.auth import get_user_model
        from tasks.models import Item, Project, TeamRoster
        from tasks.tasks import sweep_rebalance_pods

        User = get_user_model()
        kai = User.objects.create_user(
            username='kai', email='kai@shizuha.com', password='x')
        project = Project.objects.create(name='SCLI', key='SCLI', org_id=1)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[kai.email]],
                'is_active': True,
            },
        )
        item = Item.objects.create(
            title='Legacy alias task should not strand',
            project=project,
            assignment_group='engineering',
            status='todo',
            mode='task',
        )

        def _user_by_email(email):
            normalized = (email or '').strip().lower()
            if normalized == 'kai@shizuha.com':
                return {'id': kai.id, 'email': kai.email}
            return None

        with mock.patch.object(
            WorkflowService, '_pod_roster',
            return_value={'engineering': ['kai@shizuha.com']},
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment',
            side_effect=lambda email: (email or '').strip().lower() == 'kai@shizuha.com',
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_user_by_email,
        ), mock.patch.dict(
            'os.environ',
            {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'},
            clear=False,
        ):
            result = sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, 'kai@shizuha.com')
        self.assertEqual(item.assignee_id, kai.id)
        self.assertEqual(result.result['moves'], 1)
