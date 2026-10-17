"""PLAT-1225 regression tests for EVOL-20 membership-matrix availability gating."""
import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from tasks.models import AgentProjectMembership, Project, ProjectTeamLead


class PLAT1225MembershipMatrixAvailabilityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1,
            access_level='private', is_active=True,
        )
        ProjectTeamLead.objects.create(
            org_id=1, project_key='EVOL', team_name='engineering',
            lead_email='akira@shizuha.com', lead_id=101, is_active=True,
        )
        ProjectTeamLead.objects.create(
            org_id=1, project_key='EVOL', team_name='architecture',
            lead_email='nagi@shizuha.com', lead_id=102, is_active=True,
        )

    def _run_matrix(self, *args):
        out = StringIO()
        call_command('derive_agent_membership_matrix', '--json', *args, stdout=out)
        return json.loads(out.getvalue())

    def _availability_side_effect(self, email):
        return str(email).strip().lower() != 'akira@shizuha.com'

    @patch(
        'tasks.management.commands.derive_agent_membership_matrix._known_agent_emails',
        return_value={'akira@shizuha.com', 'nagi@shizuha.com'},
    )
    @patch(
        'tasks.management.commands.derive_agent_membership_matrix.'
        'WorkflowService._is_agent_available_for_assignment',
    )
    def test_shadow_marks_unavailable_agents_without_writing_rows(self, mock_available, _):
        mock_available.side_effect = self._availability_side_effect

        data = self._run_matrix()

        self.assertEqual(data['summary']['mode'], 'shadow (report-only, no behaviour change)')
        self.assertEqual(data['summary']['skipped_unavailable'], 1)
        self.assertEqual(data['summary']['skipped_availability_unknown'], 0)
        self.assertEqual(data['summary']['skipped_at_cap'], 0)
        self.assertEqual(data['summary']['skipped_total'], 1)
        matrix = {(e['agent'], e['project']): e for e in data['matrix']}
        self.assertIn('SKIPPED: unavailable', matrix[('akira@shizuha.com', 'EVOL')]['action'])
        self.assertEqual(matrix[('nagi@shizuha.com', 'EVOL')]['action'], 'would-add primary/lead')
        self.assertFalse(AgentProjectMembership.objects.exists())

    @patch(
        'tasks.management.commands.derive_agent_membership_matrix._known_agent_emails',
        return_value={'akira@shizuha.com', 'nagi@shizuha.com'},
    )
    @patch(
        'tasks.management.commands.derive_agent_membership_matrix.'
        'WorkflowService._is_agent_available_for_assignment',
    )
    def test_apply_skips_unavailable_agents_and_seeds_only_available_rows(self, mock_available, _):
        mock_available.side_effect = self._availability_side_effect

        data = self._run_matrix('--apply')

        self.assertEqual(data['summary']['mode'], 'apply')
        self.assertEqual(data['summary']['seeded'], 1)
        self.assertEqual(data['summary']['skipped_unavailable'], 1)
        self.assertEqual(data['summary']['skipped_availability_unknown'], 0)
        self.assertEqual(data['summary']['skipped_at_cap'], 0)
        self.assertEqual(data['summary']['skipped_total'], 1)
        matrix = {(e['agent'], e['project']): e for e in data['matrix']}
        self.assertIn('SKIPPED: unavailable', matrix[('akira@shizuha.com', 'EVOL')]['action'])
        self.assertEqual(matrix[('nagi@shizuha.com', 'EVOL')]['action'], 'ADDED primary/lead')

        self.assertFalse(AgentProjectMembership.objects.filter(
            agent_user_email='akira@shizuha.com', active=True,
        ).exists())
        self.assertTrue(AgentProjectMembership.objects.filter(
            agent_user_email='nagi@shizuha.com', project=self.project,
            tier='primary', is_lead=True, active=True,
        ).exists())

    @patch(
        'tasks.management.commands.derive_agent_membership_matrix._known_agent_emails',
        return_value={'akira@shizuha.com', 'nagi@shizuha.com'},
    )
    @patch(
        'tasks.management.commands.derive_agent_membership_matrix.'
        'WorkflowService._is_agent_available_for_assignment',
    )
    def test_apply_fails_closed_when_availability_check_errors(self, mock_available, _):
        def availability(email):
            if str(email).strip().lower() == 'akira@shizuha.com':
                raise RuntimeError('resolver unavailable')
            return True

        mock_available.side_effect = availability

        data = self._run_matrix('--apply')

        self.assertEqual(data['summary']['mode'], 'apply')
        self.assertEqual(data['summary']['seeded'], 1)
        self.assertEqual(data['summary']['skipped_unavailable'], 0)
        self.assertEqual(data['summary']['skipped_availability_unknown'], 1)
        self.assertEqual(data['summary']['skipped_at_cap'], 0)
        self.assertEqual(data['summary']['skipped_total'], 1)
        matrix = {(e['agent'], e['project']): e for e in data['matrix']}
        self.assertIn('SKIPPED: availability_unknown', matrix[('akira@shizuha.com', 'EVOL')]['action'])
        self.assertEqual(matrix[('nagi@shizuha.com', 'EVOL')]['action'], 'ADDED primary/lead')
        self.assertFalse(AgentProjectMembership.objects.filter(
            agent_user_email='akira@shizuha.com', active=True,
        ).exists())

    @patch(
        'tasks.management.commands.derive_agent_membership_matrix._known_agent_emails',
        return_value={'nagi@shizuha.com'},
    )
    @patch(
        'tasks.management.commands.derive_agent_membership_matrix.'
        'WorkflowService._is_agent_available_for_assignment',
        return_value=True,
    )
    def test_cap_skips_are_counted_separately_from_unavailable_skips(self, _mock_available, _):
        ProjectTeamLead.objects.all().delete()
        for idx in range(5):
            key = f'P{idx}'
            Project.objects.create(
                name=f'Project {idx}', key=key, org_id=1,
                access_level='private', is_active=True,
            )
            ProjectTeamLead.objects.create(
                org_id=1, project_key=key, team_name='engineering',
                lead_email='nagi@shizuha.com', lead_id=102, is_active=True,
            )

        data = self._run_matrix()

        self.assertEqual(data['summary']['skipped_at_cap'], 1)
        self.assertEqual(data['summary']['skipped_unavailable'], 0)
        self.assertEqual(data['summary']['skipped_availability_unknown'], 0)
        self.assertEqual(data['summary']['skipped_total'], 1)
        skipped = [e for e in data['matrix'] if 'SKIPPED: at cap' in e['action']]
        self.assertEqual(len(skipped), 1)
