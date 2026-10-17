"""Merge routing authority and required-seat drift regressions."""
import json
import os
from io import StringIO
from unittest import mock

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase

from tasks.models import TeamRoster
from tasks.services.team_roster_drift import (
    EXPECTED_TEAM_ROSTER_MEMBERS,
    compare_team_roster_members,
)
from tasks.services.workflow_service import WorkflowService


ROSTER = {
    'merge': [
        ['ni@shizuha.com'],          # TeamRoster is source-of-truth, even if env allow-list disagrees
        ['aoi@shizuha.com'],
    ],
}
UIDS = {'ni@shizuha.com': 10, 'aoi@shizuha.com': 11}


class MergeRoutingTeamRosterTests(SimpleTestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(WorkflowService, '_pod_roster', return_value=ROSTER),
            mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True),
            mock.patch.object(WorkflowService, '_agent_active_load', return_value=0),
            mock.patch.object(WorkflowService, '_agent_workable_load', return_value=0),
            mock.patch.object(WorkflowService, '_junior_emails', return_value=frozenset()),
            mock.patch(
                'tasks.user_service.user_service',
                **{'get_user_by_email.side_effect': lambda email: {'id': UIDS[email]} if email in UIDS else None},
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_merge_resolver_uses_teamroster_without_static_env_allowlist(self):
        with mock.patch.dict('os.environ', {'PULSE_MERGE_CAPABLE_EMAILS': 'aoi@shizuha.com'}, clear=False):
            uid, email = WorkflowService._resolve_team_assignee('merge')

        self.assertEqual((uid, email), (10, 'ni@shizuha.com'))

    def test_current_assignee_keep_list_is_full_teamroster(self):
        roster = WorkflowService._merge_capable_roster_emails()

        self.assertIn('ni@shizuha.com', roster)
        self.assertIn('aoi@shizuha.com', roster)

    def test_merge_resolver_uses_review_failover_when_primary_down(self):
        roster = {'merge': [['sora@shizuha.com'], ['kei@shizuha.com']]}
        uids = {'sora@shizuha.com': 21, 'kei@shizuha.com': 22}

        with mock.patch.object(WorkflowService, '_pod_roster', return_value=roster), \
             mock.patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 side_effect=lambda email: email == 'kei@shizuha.com',
             ), \
             mock.patch(
                 'tasks.user_service.user_service',
                 **{'get_user_by_email.side_effect': lambda email: {'id': uids[email]} if email in uids else None},
             ):
            uid, email = WorkflowService._resolve_team_assignee('merge')

        self.assertEqual((uid, email), (22, 'kei@shizuha.com'))

    def test_missing_hive_merge_credential_is_diagnostic_not_roster_filter(self):
        diagnostic = WorkflowService._merge_credential_diagnostic(
            'ni@shizuha.com',
            {'ni@shizuha.com': {'health': 'missing', 'message': 'no github grant applied'}},
        )

        self.assertIn('TeamRoster.merge', diagnostic)
        self.assertIn('DevOps/Hive', diagnostic)
        uid, email = WorkflowService._resolve_team_assignee('merge')
        self.assertEqual((uid, email), (10, 'ni@shizuha.com'))


class MergeRosterDriftGuardTests(TestCase):
    """PLS-866: a missing intended Merge seat is a loud CI/ops signal."""

    canonical_key = 'shizuha:merge'
    required = frozenset({
        'aoi@shizuha.com',
        'haru@shizuha.com',
        'kei@shizuha.com',
        'ryo@shizuha.com',
    })

    frozen_intended_roster_fixture = (
        ('aoi@shizuha.com', 'haru@shizuha.com'),
        ('kei@shizuha.com', 'ryo@shizuha.com'),
    )

    def test_architecture_pinned_merge_seats_cannot_silently_shrink(self):
        self.assertEqual(
            EXPECTED_TEAM_ROSTER_MEMBERS[self.canonical_key],
            self.required,
        )
        signal = compare_team_roster_members(
            self.canonical_key,
            TeamRoster._flat_member_emails(
                self.frozen_intended_roster_fixture,
            ),
        )
        self.assertFalse(signal.drifted, signal.as_dict())

    def test_missing_aoi_emits_explicit_drift_signal(self):
        signal = compare_team_roster_members(
            self.canonical_key,
            self.required - {'aoi@shizuha.com'},
        )
        self.assertTrue(signal.drifted)
        self.assertEqual(signal.missing, ('aoi@shizuha.com',))
        self.assertEqual(signal.source, 'team_roster_compat')

    def test_live_read_command_passes_for_exact_org_reference_row(self):
        roster = TeamRoster.objects.get(
            organization_slug='shizuha',
            organization_id=1,
            team_name='merge',
        )
        TeamRoster.objects.filter(pk=roster.pk).update(
            tiers=[sorted(self.required)],
            is_active=True,
        )
        output = StringIO()
        call_command('check_team_roster_drift', stdout=output)
        payload = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(payload['canonical_key'], self.canonical_key)
        self.assertEqual(payload['source'], 'team_roster_compat')
        self.assertEqual(payload['status'], 'ok')

    def test_live_read_command_fails_when_required_seat_is_removed(self):
        roster = TeamRoster.objects.get(
            organization_slug='shizuha',
            organization_id=1,
            team_name='merge',
        )
        remaining = self.required - {'aoi@shizuha.com'}
        TeamRoster.objects.filter(pk=roster.pk).update(
            tiers=[sorted(remaining)],
        )

        with self.assertRaisesMessage(CommandError, 'aoi@shizuha.com'):
            call_command('check_team_roster_drift', stdout=StringIO())

    def test_integration_live_exact_org_row_has_required_seats(self):
        """Optional safe live-read probe; CI remains hermetic and deterministic."""
        if os.environ.get('PULSE_RUN_LIVE_ROSTER_DRIFT_PROBE') != '1':
            self.skipTest('set PULSE_RUN_LIVE_ROSTER_DRIFT_PROBE=1 for live DB probe')

        roster = TeamRoster.objects.get(
            organization_slug='shizuha',
            organization_id=1,
            team_name='merge',
            is_active=True,
        )
        signal = compare_team_roster_members(
            self.canonical_key,
            TeamRoster._flat_member_emails(
                roster.tiers,
                roster.junior_emails,
            ),
            source_version=f'row:{roster.pk};version:{roster.version}',
        )
        self.assertFalse(signal.drifted, signal.as_dict())
