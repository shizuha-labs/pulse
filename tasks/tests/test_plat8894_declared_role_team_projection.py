"""PLAT-8894 — DECLARED_ROLE_TEAM_PROJECTION (PLAT-7421 Defect-A subclass-2).

goro decision comment 772253: org-team ownership of a declared role authorizes
EXECUTION of a role-gated transition absent a project membership row, under an
explicit documented team→role mapping, fail-closed conjunction. The resolver's
candidate filter stays byte-identical (PLS-203/PLS-3): team-membership-only
seats are still skipped as assignment candidates.

Five fail-closed tests:
  (a) team-membership-only seat firing a declared-role transition succeeds;
  (b) seat holding no mapped role denied;
  (c) unmapped declared role denied;
  (d) team-lookup error denied;
  (e) resolver still skips team-membership-only seats as candidates.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from tasks.models import (
    Item,
    Project,
    ProjectMembership,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import (
    DECLARED_ROLE_TEAM_PROJECTION,
    WorkflowService,
    WorkflowValidationError,
)


def _principal(email, user_id, org_memberships=None):
    return SimpleNamespace(
        id=user_id,
        email=email,
        is_staff=False,
        is_superuser=False,
        is_authenticated=True,
        organization_memberships=org_memberships or {},
    )


@override_settings(PULSE_HEALTH_EXPORTER_URL='disabled', PULSE_API_HEALTH_URL='disabled')
class PLAT8894DeclaredRoleTeamProjectionTests(TestCase):
    def setUp(self):
        self.open_status, _ = Status.objects.get_or_create(
            slug='plat8894_open',
            defaults={'name': 'PLAT8894 Open', 'category': 'todo', 'color': '#ccc'},
        )
        self.doing_status, _ = Status.objects.get_or_create(
            slug='plat8894_in_progress',
            defaults={'name': 'PLAT8894 In Progress', 'category': 'in_progress', 'color': '#3b82f6'},
        )
        self.workflow = Workflow.objects.create(
            name='PLAT8894 Security Finding',
            slug='plat8894-security-finding',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.doing_status, position=1)
        self.accept = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Accept',
            from_status='plat8894_open',
            to_status='plat8894_in_progress',
            allowed_roles=['security'],
            position=1,
        )
        self.project = Project.objects.create(
            name='Pulse', key='PLAT8894', org_id=1, access_level='open',
        )
        # goro's fixture shape: the acceptance-test seat owns the role via the
        # org roster and holds NO project membership row.
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults={'tiers': [['goro@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='security finding',
            mode='task',
            item_type='task.bug',
            workflow_name='plat8894-security-finding',
            status='plat8894_open',
            workflow_status='plat8894_open',
            assignment_group='security',
            priority='urgent',
            created_by_id=999,
            created_by_email='reporter@shizuha.com',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    # ------------------------------------------------------------------ (a)
    def test_team_membership_only_seat_fires_declared_role_transition(self):
        """(a) goro's acceptance fixture: roster-owned role, NO project row —
        the declared-role transition executes."""
        item = self._item()
        goro = _principal('goro@shizuha.com', 201)
        self.assertFalse(
            ProjectMembership.objects.filter(project=self.project).exists(),
            'fixture precondition: no project membership rows at all',
        )

        # Execution-side check (default flag) authorizes via the projection.
        self.assertTrue(
            WorkflowService._check_transition_conditions(self.accept, item, goro),
        )

        # And the real execution path fires it end-to-end.
        WorkflowService.execute_transition(item, 'plat8894_in_progress', goro)
        item.refresh_from_db()
        self.assertEqual(item.status, 'plat8894_in_progress')

    # ------------------------------------------------------------------ (b)
    def test_seat_without_mapped_role_is_denied(self):
        """(b) a seat on NO owning team, with no project row, is denied even
        though the transition declares a mapped role."""
        item = self._item()
        outsider = _principal('outsider@shizuha.com', 202)

        self.assertFalse(
            WorkflowService._check_transition_conditions(self.accept, item, outsider),
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(item, 'plat8894_in_progress', outsider)

    # ------------------------------------------------------------------ (c)
    def test_unmapped_declared_role_is_denied(self):
        """(c) a declared role absent from DECLARED_ROLE_TEAM_PROJECTION never
        fires the projection path — even for a roster member of an unrelated
        mapped team. Absence of projection keys preserves today's behavior."""
        self.assertNotIn('owner', DECLARED_ROLE_TEAM_PROJECTION)
        owner_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Owner Accept',
            from_status='plat8894_open',
            to_status='plat8894_in_progress',
            allowed_roles=['owner'],
            position=2,
        )
        item = self._item()
        # This seat IS a member of a mapped owning team ('security') — the
        # denial must come from the role being unmapped, not the seat.
        self.assertTrue(
            WorkflowService._is_team_member('goro@shizuha.com', 'security', item=item),
        )
        goro = _principal('goro@shizuha.com', 201)

        self.assertFalse(
            WorkflowService._check_transition_conditions(owner_transition, item, goro),
        )

    # ------------------------------------------------------------------ (d)
    def test_team_lookup_error_is_denied(self):
        """(d) a team-lookup error fails closed — denied, no partial grant.

        The item carries no assignment_group so the pre-existing
        exact-team-member lookup short-circuits and the ONLY team lookups on
        this path are the projection's own — the raised error is exercised
        against the new fail-closed leg, not legacy plumbing."""
        item = self._item(assignment_group='')
        goro = _principal('goro@shizuha.com', 201)

        with patch.object(
            WorkflowService, '_is_team_member', side_effect=RuntimeError('roster db down'),
        ):
            self.assertFalse(
                WorkflowService._check_transition_conditions(self.accept, item, goro),
            )
        with self.assertRaises(WorkflowValidationError):
            with patch.object(
                WorkflowService, '_is_team_member', side_effect=RuntimeError('roster db down'),
            ):
                WorkflowService.execute_transition(item, 'plat8894_in_progress', goro)
        item.refresh_from_db()
        self.assertEqual(item.status, 'plat8894_open', 'denied transition must not mutate state')

    # ------------------------------------------------------------------ (e)
    def test_resolver_still_skips_team_membership_only_seats(self):
        """(e) PLS-203/PLS-3 byte-identical: the resolver's candidate filter
        does NOT include the projection — a roster member with no project row
        is still skipped as an assignment candidate."""
        @patch('tasks.services.workflow_service.WorkflowService.'
               '_is_agent_available_for_assignment', return_value=True)
        def _run(_avail):
            item = self._item(assignee_id=None, assignee_email='')
            return WorkflowService._resolve_team_assignee('security', item=item)

        uid, email = _run()
        self.assertIsNone(uid, 'team-membership-only seat must not be auto-assigned')
        self.assertIsNone(email)

        # The resolver-side predicate (opted out) denies the same seat that
        # execution-side authorizes — the asymmetry is the contract.
        item = self._item()
        goro = _principal('goro@shizuha.com', 201)
        self.assertFalse(
            WorkflowService._member_can_execute_from_status(
                'goro@shizuha.com', item, user_id=201,
            ),
        )
        self.assertTrue(
            WorkflowService._check_transition_conditions(self.accept, item, goro),
        )

    # ------------------------------------------------- project-grant governs
    def test_any_project_row_keeps_existing_grant_path_governing(self):
        """Conjunction leg 2: ANY project membership row (even inactive) means
        the project grant governs and the projection path never fires."""
        item = self._item()
        ProjectMembership.objects.create(
            project=self.project,
            user_id=201,
            user_email='goro@shizuha.com',
            role='member',  # NOT a matching role for the 'security' transition
            is_active=False,
        )
        goro = _principal('goro@shizuha.com', 201)

        self.assertFalse(
            WorkflowService._check_transition_conditions(self.accept, item, goro),
            'an existing (inactive, non-matching) row must keep the refusal',
        )
