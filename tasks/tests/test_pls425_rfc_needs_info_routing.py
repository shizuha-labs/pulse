"""PLS-425: feature-request-rfc 'Needs Info' must PRESERVE the RFC's owning team.

The wait-for-info state is owner-preserving: entering `rfc_needs_info` (via
`Needs Info` from triage/design/approved/impl-linked/validation, or `Request More
Info` from intake) must keep the item's current `assignment_group`/`assignee` and
must never enter the operator-escalation gate.

Regression history:
- Original 0072 seed routed these transitions to `admin-ops`, which
  `_is_operator_bound_escalation_transition` classifies as operator-bound → the
  PLAT-3136 hygiene gate false-successed the transition back to `rfc_intake` and
  reassigned the team to `security` (corrupted HIVE-624 mid-review).
- Corrective 0092 re-pointed them to a hard-coded `architecture`, which fixed the
  operator gate but still mis-routed any NON-Architecture-owned RFC (e.g. the
  QA-owned PLAT-4055, `zen@shizuha.com`) — silently reassigning it to Architecture.
- 0093 strips the `assign_team` post-function entirely so the owning team is
  preserved for every owner (this is what these tests lock in).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster, Workflow, WorkflowTransition
from tasks.services.workflow_service import WorkflowService


class Pls425RfcNeedsInfoOwnerPreservingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='pls425', email='pls425@example.com', password='x', is_staff=True,
        )
        self.workflow = Workflow.objects.get(slug='feature-request-rfc')
        self.project = Project.objects.create(name='RFC Project', key='RFC', org_id=1)
        for team_name, email in (
            ('architecture', 'hiro@shizuha.com'),
            ('qa', 'zen@shizuha.com'),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'is_active': True,
                },
            )

    def _rfc(self, status, key, team, assignee_id, assignee_email):
        return Item.objects.create(
            mode='task',
            title='Amended trio HLD',
            project=self.project,
            item_key=key,
            status=status,
            workflow_status=status,
            workflow_name=self.workflow.name,
            assignment_group=team,
            assignee_id=assignee_id,
            assignee_email=assignee_email,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

    def _transition(self, name, from_status):
        return WorkflowTransition.objects.get(
            workflow=self.workflow, name=name,
            from_status=from_status, to_status='rfc_needs_info',
        )

    def test_no_needs_info_transition_assigns_a_team(self):
        # Data-integrity: an owner-preserving wait state carries NO assign_team
        # post-function on any transition entering rfc_needs_info (covers both the
        # admin-ops corruption path and the architecture mis-route).
        transitions = WorkflowTransition.objects.filter(
            workflow=self.workflow, to_status='rfc_needs_info',
        )
        self.assertTrue(transitions.exists())
        for transition in transitions:
            assign_team_pfs = [
                pf for pf in (transition.post_functions or [])
                if isinstance(pf, dict) and pf.get('type') == 'assign_team'
            ]
            self.assertEqual(
                assign_team_pfs, [],
                f"{transition.name} ({transition.from_status}->rfc_needs_info) still "
                f"reassigns the team: {assign_team_pfs}",
            )

    def _assert_preserved(self, transition_name, from_status, team, assignee_id,
                          assignee_email, key):
        item = self._rfc(from_status, key, team, assignee_id, assignee_email)
        transition = self._transition(transition_name, from_status)

        # A distinct sentinel: if any transition erroneously re-resolves a team,
        # the assignee becomes sentinel@ and the preservation asserts fail loudly.
        with patch.object(WorkflowService, '_is_team_member', return_value=True), \
                patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True), \
                patch.object(WorkflowService, '_resolve_team_assignee',
                             return_value=(999, 'sentinel@shizuha.com')):
            WorkflowService.execute_transition(
                item, 'rfc_needs_info', user=self.user, transition_id=str(transition.id),
                screen_data={'comment': 'Clarify the payer failover/commit semantics.'},
            )
        item.refresh_from_db()

        self.assertEqual(item.status, 'rfc_needs_info')            # NOT rfc_intake
        self.assertEqual(item.assignment_group, team)              # owner preserved
        self.assertEqual(item.assignee_email, assignee_email)      # assignee preserved

    def test_architecture_owned_design_needs_info_preserves_team(self):
        self._assert_preserved(
            'Needs Info', 'rfc_design',
            team='architecture', assignee_id=1, assignee_email='hiro@shizuha.com',
            key='RFC-ARCH',
        )

    def test_qa_owned_request_more_info_preserves_team(self):
        # The PLAT-4055 counterexample: a QA-owned RFC must stay with QA.
        self._assert_preserved(
            'Request More Info', 'rfc_intake',
            team='qa', assignee_id=42, assignee_email='zen@shizuha.com',
            key='RFC-QA1',
        )

    def test_qa_owned_design_needs_info_preserves_team(self):
        self._assert_preserved(
            'Needs Info', 'rfc_design',
            team='qa', assignee_id=42, assignee_email='zen@shizuha.com',
            key='RFC-QA2',
        )
