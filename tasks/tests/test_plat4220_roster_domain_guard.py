"""PLAT-4220: roster write path rejects the unowned *.shizuha.io agent domain;
@shizuha.com + human members allowed; one-agent-one-team still auto-enforced."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from tasks.models import TeamRoster


class PLAT4220RosterDomainGuardTests(TestCase):
    def test_shizuha_io_domain_rejected(self):
        with self.assertRaises(ValidationError):
            TeamRoster.objects.create(team_name='securities-engineering',
                                      tiers=[['tora@agents.shizuha.io']])

    def test_shizuha_com_and_human_allowed(self):
        r = TeamRoster.objects.create(team_name='plat4220-ct',
                                      tiers=[['rin@shizuha.com']])
        self.assertEqual(r.tiers, [['rin@shizuha.com']])
        h = TeamRoster.objects.create(team_name='plat4220-hu',
                                      tiers=[['operator@example.test']])
        self.assertEqual(h.tiers, [['operator@example.test']])

    def test_one_agent_one_team_autoremoves(self):
        TeamRoster.objects.create(team_name='team-a', tiers=[['bot@shizuha.com']])
        TeamRoster.objects.create(team_name='team-b', tiers=[['bot@shizuha.com']])
        a = TeamRoster.objects.get(team_name='team-a')
        # bot moved to team-b → auto-removed from team-a
        emails_a = {e for tier in a.tiers for e in tier}
        self.assertNotIn('bot@shizuha.com', emails_a)

    def test_operator_may_sit_on_two_lanes(self):
        operator = 'operator@example.test'
        TeamRoster.objects.create(team_name='plat4220-lane-a', tiers=[[operator]])
        TeamRoster.objects.create(team_name='plat4220-lane-b', tiers=[[operator]])
        lane_a = TeamRoster.objects.get(team_name='plat4220-lane-a')
        lane_b = TeamRoster.objects.get(team_name='plat4220-lane-b')
        self.assertEqual(lane_a.tiers, [[operator]])
        self.assertEqual(lane_b.tiers, [[operator]])
