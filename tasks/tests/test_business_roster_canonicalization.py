"""Regression coverage for organization-scoped business roster aliases."""

from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BusinessRosterCanonicalizationMigrationTests(TransactionTestCase):
    reset_sequences = True

    def test_scoped_renames_collision_merge_and_authority_preservation(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0103_merge_research_into_research_analytics'),
        ])
        apps = state.apps
        TeamRoster = apps.get_model('tasks', 'TeamRoster')
        TeamCapabilityProfile = apps.get_model('tasks', 'TeamCapabilityProfile')
        Project = apps.get_model('tasks', 'Project')
        ProjectMembership = apps.get_model('tasks', 'ProjectMembership')
        Item = apps.get_model('tasks', 'Item')
        WebhookKey = apps.get_model('tasks', 'WebhookKey')

        scoped_names = {
            'personal-3': ('personal-accounting', 'accounting'),
            'shizuha-digital': (
                'digital-accounting', 'accounting', 'digital-quant', 'quant',
            ),
            'shizuha-trading': (
                'securities-engineering', 'trading-engineering', 'engineering',
            ),
        }
        for organization_slug, names in scoped_names.items():
            TeamRoster.objects.filter(
                organization_slug=organization_slug,
                team_name__in=names,
            ).delete()

        personal = TeamRoster.objects.create(
            id=91001,
            organization_slug='personal-3',
            team_name='personal-accounting',
            tiers=[['sato@shizuha.com']],
            junior_emails=[],
            is_active=True,
        )

        digital_accounting = TeamRoster.objects.create(
            id=91002,
            organization_slug='shizuha-digital',
            team_name='accounting',
            tiers=[['kane@shizuha.com']],
            junior_emails=[],
            is_active=True,
        )
        TeamRoster.objects.create(
            id=91003,
            organization_slug='shizuha-digital',
            team_name='digital-accounting',
            tiers=[['inactive-accountant@example.com']],
            junior_emails=['inactive-junior@example.com'],
            is_active=False,
        )

        digital_quant = TeamRoster.objects.create(
            id=91004,
            organization_slug='shizuha-digital',
            team_name='quant',
            tiers=[['inactive-quant@example.com']],
            junior_emails=['inactive-quant-junior@example.com'],
            is_active=False,
        )
        TeamRoster.objects.create(
            id=91005,
            organization_slug='shizuha-digital',
            team_name='digital-quant',
            tiers=[['active-quant@shizuha.com']],
            junior_emails=['quant-junior@shizuha.com'],
            is_active=True,
        )

        trading_engineering = TeamRoster.objects.create(
            id=91006,
            organization_slug='shizuha-trading',
            team_name='engineering',
            tiers=[['nao@shizuha.com'], ['senior@shizuha.com']],
            junior_emails=[],
            is_active=True,
        )
        TeamRoster.objects.create(
            id=91007,
            organization_slug='shizuha-trading',
            team_name='securities-engineering',
            tiers=[['ryo@shizuha.com'], ['junior@shizuha.com']],
            junior_emails=['junior@shizuha.com'],
            is_active=True,
        )
        duplicate_placement = TeamRoster.objects.create(
            id=91008,
            organization_slug='shizuha',
            team_name='migration-placement-fixture',
            tiers=[['ryo@shizuha.com', 'keeper@shizuha.com']],
            junior_emails=['ryo@shizuha.com'],
            is_active=True,
        )
        wrong_org = TeamRoster.objects.create(
            id=91009,
            organization_slug='shizuha',
            team_name='personal-accounting',
            tiers=[['wrong-org@example.com']],
            junior_emails=[],
            is_active=True,
        )
        TeamCapabilityProfile.objects.filter(
            team_name__in=['engineering', 'securities-engineering', 'trading-engineering'],
        ).delete()
        engineering_profile = TeamCapabilityProfile.objects.create(
            id=92001, team_name='engineering', capabilities=['code-substrate'],
        )
        TeamCapabilityProfile.objects.create(
            id=92002, team_name='securities-engineering', capabilities=['repo-write'],
        )
        TeamCapabilityProfile.objects.create(
            id=92003, team_name='trading-engineering', capabilities=['github-token'],
        )
        personal_project = Project.objects.create(
            id=91010, name='Books', key='BKMG', org_id=4,
        )
        personal_item = Item.objects.create(
            id=91011, project=personal_project, item_key='BKMG-1',
            mode='task', title='Legacy team task',
            workflow_name='simple',
            assignment_group='personal-accounting',
        )
        webhook_key = WebhookKey.objects.create(
            id=91012, user_id=1, user_email='owner@example.com',
            name='Legacy books webhook', key='migration-legacy-books-key',
            integration_type='generic', default_project=personal_project,
            default_assignment_group='personal-accounting',
        )
        assignment_grant = ProjectMembership.objects.create(
            id=91013, project=personal_project, user_id=91013,
            user_email='assigned-only@example.com', grant_source='assignment_seed',
            is_active=True,
        )
        explicit_grant = ProjectMembership.objects.create(
            id=91014, project=personal_project, user_id=91014,
            user_email='explicit@example.com', grant_source='membership_api',
            is_active=True,
        )

        migration = import_module(
            'tasks.migrations.0104_canonicalize_org_business_rosters'
        )
        migration.forwards(apps, None)

        personal.refresh_from_db()
        self.assertEqual(personal.team_name, 'accounting')
        self.assertEqual(personal.pk, 91001)

        digital_accounting.refresh_from_db()
        self.assertEqual(digital_accounting.pk, 91002)
        self.assertEqual(digital_accounting.tiers, [['kane@shizuha.com']])
        self.assertEqual(digital_accounting.junior_emails, [])
        self.assertTrue(digital_accounting.is_active)
        self.assertFalse(TeamRoster.objects.filter(pk=91003).exists())

        digital_quant.refresh_from_db()
        self.assertEqual(digital_quant.pk, 91004)
        self.assertEqual(digital_quant.tiers, [['active-quant@shizuha.com']])
        self.assertEqual(digital_quant.junior_emails, ['quant-junior@shizuha.com'])
        self.assertTrue(digital_quant.is_active)
        self.assertFalse(TeamRoster.objects.filter(pk=91005).exists())

        trading_engineering.refresh_from_db()
        self.assertEqual(
            trading_engineering.tiers,
            [
                ['nao@shizuha.com', 'ryo@shizuha.com'],
                ['senior@shizuha.com', 'junior@shizuha.com'],
            ],
        )
        self.assertEqual(
            trading_engineering.junior_emails,
            ['junior@shizuha.com'],
        )
        self.assertFalse(TeamRoster.objects.filter(pk=91007).exists())

        duplicate_placement.refresh_from_db()
        self.assertEqual(duplicate_placement.tiers, [['keeper@shizuha.com']])
        self.assertEqual(duplicate_placement.junior_emails, [])

        wrong_org.refresh_from_db()
        self.assertEqual(wrong_org.team_name, 'personal-accounting')
        self.assertTrue(wrong_org.is_active)

        engineering_profile.refresh_from_db()
        self.assertEqual(
            engineering_profile.capabilities,
            ['code-substrate', 'github-token', 'repo-write'],
        )
        self.assertFalse(TeamCapabilityProfile.objects.filter(
            team_name__in=['securities-engineering', 'trading-engineering'],
        ).exists())
        personal_item.refresh_from_db()
        webhook_key.refresh_from_db()
        self.assertEqual(personal_item.assignment_group, 'accounting')
        self.assertEqual(webhook_key.default_assignment_group, 'accounting')
        assignment_grant.refresh_from_db()
        explicit_grant.refresh_from_db()
        self.assertFalse(assignment_grant.is_active)
        self.assertTrue(explicit_grant.is_active)

        # Re-running the data function is deliberately a no-op.
        migration.forwards(apps, None)
        trading_engineering.refresh_from_db()
        self.assertEqual(
            trading_engineering.tiers,
            [
                ['nao@shizuha.com', 'ryo@shizuha.com'],
                ['senior@shizuha.com', 'junior@shizuha.com'],
            ],
        )
