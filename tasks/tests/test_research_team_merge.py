"""Operator 2026-07-16: Research has one canonical routing identity."""
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from tasks.models import Item, TeamRoster
from tasks.services.workflow_service import WorkflowService


class ResearchTeamCanonicalizationTests(SimpleTestCase):
    def test_legacy_labels_resolve_to_research_analytics(self):
        for label in ('research', 'analytics', 'research-analytics'):
            self.assertEqual(
                WorkflowService._canonical_team_name(label),
                'research-analytics',
            )

    def test_roster_writes_canonicalize_legacy_labels(self):
        self.assertEqual(
            TeamRoster._CANONICAL_ALIASES['research'],
            'research-analytics',
        )
        self.assertEqual(
            TeamRoster._CANONICAL_ALIASES['analytics'],
            'research-analytics',
        )

    def test_canonical_team_name_does_not_encode_an_organization(self):
        self.assertEqual(
            WorkflowService._canonical_team_name('research'),
            'research-analytics',
        )


class ResearchTeamRosterWriteTests(TestCase):
    def test_update_fields_cannot_leave_a_legacy_roster_identity(self):
        roster = TeamRoster.objects.create(
            organization_slug='test-org',
            team_name='legacy-placeholder',
            tiers=[],
        )
        roster.team_name = 'research'
        roster.tiers = [['hana@shizuha.com']]
        roster.save(update_fields=['tiers'])

        roster.refresh_from_db()
        self.assertEqual(roster.team_name, 'research-analytics')

    def test_new_task_persists_only_the_canonical_assignment_group(self):
        item = Item.objects.create(
            title='Canonical research task',
            assignment_group='analytics',
            workflow_name='simple',
        )
        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'research-analytics')


class ResearchTeamDataMigrationTests(TransactionTestCase):
    reset_sequences = True

    def test_forward_merge_and_exact_backward_restore(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0102_merge_plat4609_plat4215'),
        ])
        apps = state.apps
        TeamRoster = apps.get_model('tasks', 'TeamRoster')
        Project = apps.get_model('tasks', 'Project')
        Item = apps.get_model('tasks', 'Item')
        ProjectTeamLead = apps.get_model('tasks', 'ProjectTeamLead')
        TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')

        TeamRoster.objects.filter(
            organization_slug='shizuha',
            team_name__in=['research', 'analytics', 'research-analytics'],
        ).delete()
        TeamCapabilityProfile.objects.filter(
            team_name__in=['research', 'analytics', 'research-analytics'],
        ).delete()

        canonical = TeamRoster.objects.create(
            id=90001,
            organization_slug='shizuha',
            team_name='research-analytics',
            tiers=[['tomo@shizuha.com']],
            junior_emails=[],
            is_active=True,
            note='canonical-before',
        )
        legacy = TeamRoster.objects.create(
            id=90002,
            organization_slug='shizuha',
            team_name='research',
            tiers=[['scout@shizuha.com', 'hana@shizuha.com']],
            junior_emails=[],
            is_active=True,
            note='legacy-before',
        )
        analytics_roster = TeamRoster.objects.create(
            id=90003,
            organization_slug='shizuha',
            team_name='analytics',
            tiers=[['old-analyst@example.com']],
            junior_emails=[],
            is_active=False,
            note='analytics-roster-before',
        )
        project = Project.objects.create(
            id=90001,
            org_id=1,
            name='Research merge test',
            key='RMR1',
        )
        other_project = Project.objects.create(
            id=90002,
            org_id=2,
            name='Other tenant research',
            key='RMR2',
        )
        item = Item.objects.create(
            id=90001,
            title='Legacy research task',
            project=project,
            assignment_group='research',
            workflow_name='simple',
        )
        other_item = Item.objects.create(
            id=90002,
            title='Other tenant research task',
            project=other_project,
            assignment_group='research',
            workflow_name='simple',
        )
        analytics_item = Item.objects.create(
            id=90004,
            title='Legacy analytics task',
            project=project,
            assignment_group='analytics',
            workflow_name='simple',
        )
        recurring_item = Item.objects.create(
            id=90003,
            title='Recurring research task',
            project=project,
            assignment_group='research-analytics',
            workflow_name='simple',
            metadata={'recurring_owner_team': 'analytics', 'keep': True},
        )
        lead = ProjectTeamLead.objects.create(
            id=90001,
            org_id=1,
            project_key='ctx',
            team_name='research',
            lead_email='hana@shizuha.com',
            is_active=True,
        )
        other_lead = ProjectTeamLead.objects.create(
            id=90002,
            org_id=2,
            project_key='rmr2',
            team_name='research',
            lead_email='other@example.com',
            is_active=True,
        )
        analytics_lead = ProjectTeamLead.objects.create(
            id=90003,
            org_id=1,
            project_key='rmr1',
            team_name='analytics',
            lead_email='banto@shizuha.com',
            is_active=False,
        )
        TeamCapabilityProfile.objects.create(
            id=90001,
            team_name='analytics',
            capabilities=['code-substrate'],
            note='analytics-before',
        )
        TeamCapabilityProfile.objects.create(
            id=90002,
            team_name='research-analytics',
            capabilities=['research-repository'],
            note='canonical-profile-before',
        )
        migration = import_module(
            'tasks.migrations.0103_merge_research_into_research_analytics'
        )

        migration.forward(apps, None)
        canonical.refresh_from_db()
        legacy.refresh_from_db()
        analytics_roster.refresh_from_db()
        item.refresh_from_db()
        other_item.refresh_from_db()
        analytics_item.refresh_from_db()
        recurring_item.refresh_from_db()
        lead.refresh_from_db()
        other_lead.refresh_from_db()
        analytics_lead.refresh_from_db()
        self.assertEqual(
            canonical.tiers,
            [['hana@shizuha.com', 'tomo@shizuha.com', 'scout@shizuha.com']],
        )
        self.assertFalse(legacy.is_active)
        self.assertFalse(analytics_roster.is_active)
        self.assertEqual(item.assignment_group, 'research-analytics')
        self.assertEqual(lead.team_name, 'research-analytics')
        self.assertEqual(other_item.assignment_group, 'research')
        self.assertEqual(analytics_item.assignment_group, 'research-analytics')
        self.assertEqual(
            recurring_item.metadata,
            {'recurring_owner_team': 'research-analytics', 'keep': True},
        )
        self.assertEqual(other_lead.team_name, 'research')
        self.assertEqual(analytics_lead.team_name, 'research-analytics')
        self.assertFalse(
            TeamCapabilityProfile.objects.filter(team_name='analytics').exists()
        )
        merged_profile = TeamCapabilityProfile.objects.get(team_name='research-analytics')
        self.assertEqual(
            merged_profile.capabilities,
            ['code-substrate', 'research-repository'],
        )

        migration.backward(apps, None)
        canonical.refresh_from_db()
        legacy.refresh_from_db()
        analytics_roster.refresh_from_db()
        item.refresh_from_db()
        other_item.refresh_from_db()
        analytics_item.refresh_from_db()
        recurring_item.refresh_from_db()
        lead.refresh_from_db()
        other_lead.refresh_from_db()
        analytics_lead.refresh_from_db()
        self.assertEqual(canonical.tiers, [['tomo@shizuha.com']])
        self.assertEqual(canonical.note, 'canonical-before')
        self.assertTrue(legacy.is_active)
        self.assertEqual(legacy.tiers, [['scout@shizuha.com', 'hana@shizuha.com']])
        self.assertEqual(legacy.note, 'legacy-before')
        self.assertFalse(analytics_roster.is_active)
        self.assertEqual(analytics_roster.tiers, [['old-analyst@example.com']])
        self.assertEqual(analytics_roster.note, 'analytics-roster-before')
        self.assertEqual(item.assignment_group, 'research')
        self.assertEqual(lead.team_name, 'research')
        self.assertEqual(other_item.assignment_group, 'research')
        self.assertEqual(analytics_item.assignment_group, 'analytics')
        self.assertEqual(
            recurring_item.metadata,
            {'recurring_owner_team': 'analytics', 'keep': True},
        )
        self.assertEqual(other_lead.team_name, 'research')
        self.assertEqual(analytics_lead.team_name, 'analytics')
        analytics_profile = TeamCapabilityProfile.objects.get(team_name='analytics')
        canonical_profile = TeamCapabilityProfile.objects.get(team_name='research-analytics')
        self.assertEqual(analytics_profile.capabilities, ['code-substrate'])
        self.assertEqual(analytics_profile.note, 'analytics-before')
        self.assertEqual(canonical_profile.capabilities, ['research-repository'])
        self.assertEqual(canonical_profile.note, 'canonical-profile-before')
