from importlib import import_module
from types import SimpleNamespace

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class OrganizationIsolationMigrationTests(TransactionTestCase):
    reset_sequences = True

    def _set_org_triggers(self, enabled):
        if connection.vendor != 'postgresql':
            return
        action = 'ENABLE' if enabled else 'DISABLE'
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE tasks_item_link {action} TRIGGER '
                'tasks_item_link_org_guard'
            )
            cursor.execute(
                f'ALTER TABLE tasks_task {action} TRIGGER '
                'tasks_item_hierarchy_org_guard'
            )
            cursor.execute(
                f'ALTER TABLE tasks_task {action} TRIGGER '
                'tasks_item_assignment_org_guard'
            )

    def test_shizuha_decision_team_seed_is_exact_org_and_idempotent(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0127_seed_trading_decision_team'),
        ])
        apps = state.apps
        ProjectMembership = apps.get_model('tasks', 'ProjectMembership')
        TeamRoster = apps.get_model('tasks', 'TeamRoster')
        # The dependency migrations seed roster rows during database setup,
        # while TransactionTestCase resets the backing sequence. Isolate this
        # fixture so its first insert cannot collide with retained seed IDs.
        TeamRoster.objects.all().delete()
        foreign, _ = TeamRoster.objects.update_or_create(
            organization_id=2,
            team_name='ceo-decisions',
            defaults={
                'organization_slug': 'shizuha-digital',
                'tiers': [['foreign-holder@example.com']],
                'junior_emails': [],
                'is_active': True,
            },
        )
        membership_count = ProjectMembership.objects.count()
        migration = import_module(
            'tasks.migrations.0128_pls597_seed_shizuha_decision_team'
        )
        schema_editor = SimpleNamespace(connection=connection)

        migration.seed_shizuha_decision_team(apps, schema_editor)
        migration.seed_shizuha_decision_team(apps, schema_editor)

        roster = TeamRoster.objects.get(
            organization_id=1,
            team_name='ceo-decisions',
        )
        self.assertEqual(roster.organization_slug, 'shizuha')
        self.assertEqual(roster.tiers, [['operator@example.test']])
        self.assertEqual(roster.junior_emails, [])
        self.assertTrue(roster.is_active)
        self.assertEqual(
            TeamRoster.objects.filter(
                organization_id=1,
                team_name='ceo-decisions',
            ).count(),
            1,
        )
        foreign.refresh_from_db()
        self.assertEqual(foreign.tiers, [['foreign-holder@example.com']])
        self.assertEqual(ProjectMembership.objects.count(), membership_count)

    def test_personal_decision_team_seed_repairs_only_bksh_10(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0118_repair_org_local_focused_qa_review_routes'),
        ])
        apps = state.apps
        Comment = apps.get_model('tasks', 'Comment')
        Item = apps.get_model('tasks', 'Item')
        ItemLink = apps.get_model('tasks', 'ItemLink')
        Project = apps.get_model('tasks', 'Project')
        TeamRoster = apps.get_model('tasks', 'TeamRoster')

        # Data migrations seed roster rows during test-database setup. Parallel
        # clones can retain those primary keys while their identity sequence is
        # reset, so isolate this migration fixture before its first insert.
        TeamRoster.objects.all().delete()
        accounting, _ = TeamRoster.objects.update_or_create(
            organization_id=4,
            team_name='accounting',
            defaults={
                'organization_slug': 'personal-3',
                'tiers': [['sato@shizuha.com']],
                'is_active': True,
            },
        )
        project = Project.objects.create(
            org_id=4,
            name='Personal Books',
            key='BKSH',
        )

        self._set_org_triggers(False)
        try:
            stranded = Item.objects.create(
                project=project,
                item_key='BKSH-10',
                mode='task',
                title='Remove public holder-controlled artifact',
                status='todo',
                workflow_name='autonomous-dev',
                assignment_group='human-ops',
                assignee_id=77,
                assignee_email='operator@example.test',
            )
            physical = Item.objects.create(
                project=project,
                item_key='BKSH-11',
                mode='task',
                title='Physical action remains human ops',
                status='todo',
                workflow_name='autonomous-dev',
                assignment_group='human-ops',
                assignee_id=77,
                assignee_email='operator@example.test',
            )
            blocker = Item.objects.create(
                project=project,
                item_key='BKSH-9',
                mode='task',
                title='Provide the holder-controlled source evidence',
                status='todo',
                workflow_name='autonomous-dev',
                assignment_group='accounting',
                assignee_id=88,
                assignee_email='sato@shizuha.com',
            )
            for target in (stranded, physical):
                ItemLink.objects.create(
                    source_item=blocker,
                    target_item=target,
                    link_type='blocks',
                    created_by_id=1,
                    created_by_email='owner@example.com',
                )
                Item.objects.filter(pk=target.pk).update(status='blocked')
        finally:
            self._set_org_triggers(True)

        migration = import_module(
            'tasks.migrations.0124_seed_personal_decision_team'
        )
        schema_editor = SimpleNamespace(connection=connection)
        migration.seed_personal_decision_team(apps, schema_editor)
        migration.seed_personal_decision_team(apps, schema_editor)

        roster = TeamRoster.objects.get(
            organization_id=4,
            team_name='ceo-decisions',
        )
        self.assertEqual(roster.organization_slug, 'personal-3')
        self.assertEqual(roster.tiers, [['operator@example.test']])
        accounting.refresh_from_db()
        self.assertEqual(accounting.tiers, [['sato@shizuha.com']])

        stranded.refresh_from_db()
        physical.refresh_from_db()
        self.assertEqual(stranded.assignment_group, 'ceo-decisions')
        self.assertEqual(stranded.assignee_id, 77)
        self.assertEqual(
            stranded.metadata['org_local_operator_normalizations'][0]['version'],
            '0124',
        )
        self.assertEqual(physical.assignment_group, 'human-ops')
        self.assertEqual(
            Comment.objects.filter(
                item=stranded,
                content__contains='migration 0124',
            ).count(),
            1,
        )

        # A later valid routing decision must not be overwritten by a replay of
        # this one-time human-ops repair.
        stranded.assignment_group = 'accounting'
        stranded.assignee_id = 88
        stranded.assignee_email = 'sato@shizuha.com'
        stranded.save(update_fields=[
            'assignment_group',
            'assignee_id',
            'assignee_email',
            'updated_at',
        ])
        migration.seed_personal_decision_team(apps, schema_editor)
        stranded.refresh_from_db()
        self.assertEqual(stranded.assignment_group, 'accounting')
        self.assertEqual(
            Comment.objects.filter(
                item=stranded,
                content__contains='migration 0124',
            ).count(),
            1,
        )

    def test_trading_decision_team_seed_repairs_only_trd_26(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0126_hive1223_project_is_default'),
        ])
        apps = state.apps
        Comment = apps.get_model('tasks', 'Comment')
        Item = apps.get_model('tasks', 'Item')
        Project = apps.get_model('tasks', 'Project')
        TeamRoster = apps.get_model('tasks', 'TeamRoster')

        # See the personal seed fixture above: isolate retained clone rows from
        # the reset primary-key sequence before this migration creates a roster.
        TeamRoster.objects.all().delete()
        project = Project.objects.create(
            org_id=3,
            name='Shizuha Trading',
            key='TRD',
        )
        self._set_org_triggers(False)
        try:
            stranded = Item.objects.create(
                project=project,
                item_key='TRD-26',
                mode='task',
                title='Repair decision escalation schema',
                status='todo',
                workflow_name='autonomous-dev',
                assignment_group='human-ops',
                assignee_id=77,
                assignee_email='operator@example.test',
            )
            physical = Item.objects.create(
                project=project,
                item_key='TRD-27',
                mode='task',
                title='Physical action remains human ops',
                status='todo',
                workflow_name='autonomous-dev',
                assignment_group='human-ops',
                assignee_id=77,
                assignee_email='operator@example.test',
            )
        finally:
            self._set_org_triggers(True)

        migration = import_module(
            'tasks.migrations.0127_seed_trading_decision_team'
        )
        schema_editor = SimpleNamespace(connection=connection)
        migration.seed_trading_decision_team(apps, schema_editor)
        migration.seed_trading_decision_team(apps, schema_editor)

        roster = TeamRoster.objects.get(
            organization_id=3,
            team_name='ceo-decisions',
        )
        self.assertEqual(roster.organization_slug, 'shizuha-trading')
        self.assertEqual(roster.tiers, [['operator@example.test']])

        stranded.refresh_from_db()
        physical.refresh_from_db()
        self.assertEqual(stranded.assignment_group, 'ceo-decisions')
        self.assertEqual(stranded.assignee_id, 77)
        self.assertEqual(
            stranded.metadata['org_local_operator_normalizations'][0]['version'],
            '0127',
        )
        self.assertEqual(physical.assignment_group, 'human-ops')
        self.assertEqual(
            Comment.objects.filter(
                item=stranded,
                content__contains='migration 0127',
            ).count(),
            1,
        )

    def test_forward_groups_active_dependencies_and_reverse_restores_snapshot(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0105_enforce_item_org_isolation'),
        ])
        apps = state.apps
        Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
        Item = apps.get_model('tasks', 'Item')
        ItemLink = apps.get_model('tasks', 'ItemLink')
        Project = apps.get_model('tasks', 'Project')

        local_project = Project.objects.create(
            org_id=1,
            name='Local',
            key='LCL',
        )
        Project.objects.create(
            org_id=1,
            name='Fallback',
            key='AT',
        )
        local_project_two = Project.objects.create(
            org_id=1,
            name='Local Two',
            key='LC2',
        )
        foreign_project = Project.objects.create(
            org_id=2,
            name='Foreign',
            key='FRN',
        )
        dependent_one = Item.objects.create(
            project=local_project,
            item_key='LCL-1',
            mode='task',
            title='Dependent one',
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
        )
        dependent_two = Item.objects.create(
            project=local_project_two,
            item_key='LC2-1',
            mode='task',
            title='Dependent two',
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
        )
        active_one = Item.objects.create(
            project=foreign_project,
            item_key='FRN-1',
            mode='task',
            title='Active foreign one',
            description=(
                'Provide the period bank statements and tax returns in the '
                'canonical accounting folder.\n\n'
                'Password: must-never-cross-org\n\n'
                '## Acceptance criteria\n'
                '- Local Accounting confirms every period statement exists.\n'
                '- The books-close task can proceed from local evidence.'
            ),
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
        )
        active_two = Item.objects.create(
            project=foreign_project,
            item_key='FRN-2',
            mode='task',
            title='Active foreign two',
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
        )
        terminal = Item.objects.create(
            project=foreign_project,
            item_key='FRN-3',
            mode='task',
            title='Done foreign dependency',
            status='done',
            workflow_name='simple',
            assignment_group='accounting',
        )
        unscoped = Item.objects.create(
            mode='task',
            title='[Workflow Change] LCL-99: Legacy projectless task',
            status='todo',
            workflow_name='simple',
        )

        self._set_org_triggers(False)
        try:
            with transaction.atomic():
                links = [
                    ItemLink.objects.create(
                        source_item=active_one,
                        target_item=dependent_one,
                        link_type='blocks',
                        created_by_id=1,
                        created_by_email='owner@example.com',
                    ),
                    ItemLink.objects.create(
                        source_item=active_one,
                        target_item=dependent_two,
                        link_type='blocks',
                        created_by_id=1,
                        created_by_email='owner@example.com',
                    ),
                    ItemLink.objects.create(
                        source_item=active_two,
                        target_item=dependent_one,
                        link_type='blocks',
                        created_by_id=1,
                        created_by_email='owner@example.com',
                    ),
                    ItemLink.objects.create(
                        source_item=terminal,
                        target_item=dependent_one,
                        link_type='blocks',
                        created_by_id=1,
                        created_by_email='owner@example.com',
                    ),
                ]
                Item.objects.filter(
                    pk__in=[dependent_one.id, dependent_two.id],
                ).update(status='blocked')
                Item.objects.filter(pk=dependent_two.id).update(
                    parent_item_id=active_one.id,
                )
        finally:
            self._set_org_triggers(True)

        migration = import_module(
            'tasks.migrations.0105_enforce_item_org_isolation'
        )
        # The real migration repairs data before installing the organization
        # triggers, and RunPython executes inside its atomic transaction. Mirror
        # both contracts when exercising the data function directly because key
        # allocation deliberately locks the destination project.
        self._set_org_triggers(False)
        try:
            with transaction.atomic():
                migration.remove_cross_organization_relationships(apps, None)
        finally:
            self._set_org_triggers(True)

        unscoped.refresh_from_db()
        self.assertEqual(unscoped.project_id, local_project.id)
        self.assertTrue(unscoped.item_key.startswith('LCL-'))
        self.assertEqual(Archive.objects.filter(kind='item_scope').count(), 1)
        self.assertFalse(
            ItemLink.objects.filter(pk__in=[link.pk for link in links]).exists()
        )
        replacements = list(
            Item.objects.filter(source='org_isolation_migration').order_by('id')
        )
        self.assertEqual(len(replacements), 3)
        active_one_replacements = [
            item for item in replacements
            if item.metadata['former_external_item_id'] == active_one.id
        ]
        self.assertEqual(len(active_one_replacements), 2)
        self.assertEqual(
            {
                item.metadata['target_project_id']
                for item in active_one_replacements
            },
            {local_project.id, local_project_two.id},
        )
        self.assertTrue(all(
            '## Org-safe condition capsule (migration)' in item.description
            for item in active_one_replacements
        ))
        self.assertTrue(all(
            'Local Accounting confirms every period statement exists.'
            in item.description
            for item in active_one_replacements
        ))
        self.assertTrue(all(
            'must-never-cross-org' not in item.description
            for item in active_one_replacements
        ))
        self.assertTrue(all(
            item.metadata['org_safe_condition_capsule']['version'] == 1
            for item in active_one_replacements
        ))
        active_two_replacement = next(
            item for item in replacements
            if item.metadata['former_external_item_id'] == active_two.id
        )
        self.assertIn(
            'UNKNOWN — source had no org-safe extractable condition.',
            active_two_replacement.description,
        )
        self.assertIn(
            'migration-condition-unknown',
            active_two_replacement.tags,
        )
        self.assertTrue(all(
            ItemLink.objects.filter(
                source_item_id=item.id,
                link_type='blocks',
            ).count() == 1
            for item in active_one_replacements
        ))
        self.assertFalse(
            Item.objects.filter(
                source='org_isolation_migration',
                metadata__former_external_item_id=terminal.id,
            ).exists()
        )
        dependent_two.refresh_from_db()
        self.assertIsNone(dependent_two.parent_item_id)
        self.assertEqual(Archive.objects.filter(kind='item_link').count(), 4)
        self.assertEqual(
            Archive.objects.filter(kind='replacement_task').count(),
            3,
        )
        self.assertEqual(Archive.objects.filter(kind='item_parent').count(), 1)

        self._set_org_triggers(False)
        try:
            with transaction.atomic():
                migration.restore_cross_organization_relationships(apps, None)
        finally:
            self._set_org_triggers(True)

        self.assertFalse(
            Item.objects.filter(source='org_isolation_migration').exists()
        )
        self.assertEqual(
            ItemLink.objects.filter(pk__in=[link.pk for link in links]).count(),
            4,
        )
        dependent_two.refresh_from_db()
        self.assertEqual(dependent_two.parent_item_id, active_one.id)
        unscoped.refresh_from_db()
        self.assertIsNone(unscoped.project_id)
        self.assertIsNone(unscoped.item_key)

    def test_roster_backfill_merges_legacy_alias_with_canonical_team(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0105_enforce_item_org_isolation'),
        ])
        apps = state.apps
        Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
        TeamRoster = apps.get_model('tasks', 'TeamRoster')
        TeamRoster.objects.all().delete()

        legacy = TeamRoster.objects.create(
            team_name='digital-accounting',
            organization_slug='shizuha-digital',
            organization_id=None,
            tiers=[['legacy@shizuha.com']],
            is_active=True,
        )
        canonical = TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            organization_id=None,
            tiers=[['current@shizuha.com']],
            is_active=True,
        )

        migration = import_module(
            'tasks.migrations.0105_enforce_item_org_isolation'
        )
        migration.backfill_team_roster_organization_ids(apps, None)

        rows = list(TeamRoster.objects.all())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, canonical.id)
        self.assertEqual(rows[0].organization_id, 2)
        self.assertEqual(rows[0].team_name, 'accounting')
        self.assertEqual(
            set(rows[0].tiers[0]),
            {'legacy@shizuha.com', 'current@shizuha.com'},
        )
        self.assertEqual(
            Archive.objects.filter(kind='team_roster_alias_group').count(),
            1,
        )

        migration.clear_team_roster_organization_ids(apps, None)

        restored = {
            row.id: row
            for row in TeamRoster.objects.order_by('id')
        }
        self.assertEqual(set(restored), {legacy.id, canonical.id})
        self.assertEqual(
            restored[legacy.id].team_name,
            'digital-accounting',
        )
        self.assertEqual(restored[canonical.id].team_name, 'accounting')
        self.assertTrue(all(
            row.organization_id is None for row in restored.values()
        ))

    def test_forward_canonicalizes_team_and_clears_foreign_concrete_owner(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0105_enforce_item_org_isolation'),
        ])
        apps = state.apps
        Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
        Item = apps.get_model('tasks', 'Item')
        Project = apps.get_model('tasks', 'Project')
        TeamRoster = apps.get_model('tasks', 'TeamRoster')

        # Data migrations legitimately seed roster rows. With parallel cloned
        # databases and reset_sequences=True, their retained primary keys can
        # otherwise collide nondeterministically with this test's first insert.
        Archive.objects.all().delete()
        TeamRoster.objects.all().delete()

        personal_project = Project.objects.create(
            org_id=4,
            name='Personal Books',
            key='BKSH',
        )
        Project.objects.create(
            org_id=1,
            name='Fallback',
            key='AT',
        )
        TeamRoster.objects.create(
            team_name='accounting',
            organization_slug='personal-3',
            organization_id=4,
            tiers=[['sato@shizuha.com']],
            is_active=True,
        )
        TeamRoster.objects.create(
            team_name='engineering',
            organization_slug='shizuha',
            organization_id=1,
            tiers=[['banto@shizuha.com']],
            is_active=True,
        )

        self._set_org_triggers(False)
        try:
            item = Item.objects.create(
                project=personal_project,
                item_key='BKSH-3',
                mode='task',
                title='Legacy foreign assignment',
                status='todo',
                workflow_name='simple',
                assignment_group='personal-accounting',
                assignee_id=99,
                assignee_email='banto@agents.shizuha.io',
            )
        finally:
            self._set_org_triggers(True)

        migration = import_module(
            'tasks.migrations.0105_enforce_item_org_isolation'
        )
        migration.remove_cross_organization_relationships(apps, None)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'accounting')
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)
        archive = Archive.objects.get(
            kind='item_assignment',
            object_key=str(item.id),
        )
        self.assertEqual(
            archive.payload['reason'],
            'cross_organization_assignee',
        )

        self._set_org_triggers(False)
        try:
            migration.restore_cross_organization_relationships(apps, None)
        finally:
            self._set_org_triggers(True)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'personal-accounting')
        self.assertEqual(item.assignee_id, 99)
        self.assertEqual(
            item.assignee_email,
            'banto@agents.shizuha.io',
        )

    def test_capsule_backfill_is_redacted_idempotent_and_reversible(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0105_enforce_item_org_isolation'),
        ])
        apps = state.apps
        Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
        Item = apps.get_model('tasks', 'Item')
        Project = apps.get_model('tasks', 'Project')
        # Data migrations seed archive rows outside TransactionTestCase's
        # reset-sequence accounting. Clear them before this test's first insert
        # so PostgreSQL's sequence cannot collide with a retained seed row.
        Archive.objects.all().delete()

        source_project = Project.objects.create(
            org_id=1,
            name='Source',
            key='SRC',
        )
        target_project = Project.objects.create(
            org_id=2,
            name='Target',
            key='TGT',
        )
        source = Item.objects.create(
            project=source_project,
            item_key='SRC-1',
            mode='task',
            title='Collect year-end accounting evidence',
            description=(
                'Collect bank statements, invoices, and tax returns for the '
                'period.\n'
                'Bearer abcdefghijklmnopqrstuvwxyz123456 must never copy.\n'
                '## Verification criteria\n'
                '- Local Accounting verifies the complete period folder.'
            ),
            status='in_progress',
            workflow_name='simple',
            assignment_group='accounting',
        )
        replacement = Item.objects.create(
            project=target_project,
            item_key='TGT-1',
            mode='task',
            title='[Org-local dependency] Resolve SRC-1',
            description='Human-authored local note that must remain.',
            source='org_isolation_migration',
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
            tags='existing-tag',
            metadata={
                'org_isolation_replacement': True,
                'former_external_item_id': source.id,
                'former_external_item_key': source.item_key,
            },
        )
        unknown = Item.objects.create(
            project=target_project,
            item_key='TGT-2',
            mode='task',
            title='[Org-local dependency] Missing source',
            description='Keep this second human note too.',
            source='org_isolation_migration',
            status='todo',
            workflow_name='simple',
            assignment_group='admin-ops',
            metadata={
                'org_isolation_replacement': True,
                'former_external_item_id': 999999,
                'former_external_item_key': 'SRC-404',
            },
        )
        schema_editor = SimpleNamespace(connection=connection)
        migration = import_module(
            'tasks.migrations.0106_backfill_org_safe_condition_capsules'
        )

        migration.backfill_condition_capsules(apps, schema_editor)
        migration.backfill_condition_capsules(apps, schema_editor)

        replacement.refresh_from_db()
        self.assertTrue(
            replacement.description.startswith(
                'Human-authored local note that must remain.'
            )
        )
        self.assertEqual(
            replacement.description.count(
                '## Org-safe condition capsule (migration)'
            ),
            1,
        )
        self.assertIn(
            'Local Accounting verifies the complete period folder.',
            replacement.description,
        )
        self.assertNotIn(
            'abcdefghijklmnopqrstuvwxyz123456',
            replacement.description,
        )
        self.assertEqual(replacement.tags, 'existing-tag')
        self.assertEqual(
            replacement.metadata['org_safe_condition_capsule']['version'],
            1,
        )

        unknown.refresh_from_db()
        self.assertIn(
            'UNKNOWN — source had no org-safe extractable condition.',
            unknown.description,
        )
        self.assertEqual(unknown.tags, 'migration-condition-unknown')
        self.assertEqual(
            Archive.objects.filter(
                kind='replacement_condition_capsule',
            ).count(),
            2,
        )

        migration.restore_condition_capsules(apps, schema_editor)

        replacement.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(
            replacement.description,
            'Human-authored local note that must remain.',
        )
        self.assertEqual(replacement.tags, 'existing-tag')
        self.assertNotIn(
            'org_safe_condition_capsule',
            replacement.metadata,
        )
        self.assertEqual(
            unknown.description,
            'Keep this second human note too.',
        )
        self.assertIsNone(unknown.tags)
        self.assertFalse(
            Archive.objects.filter(
                kind='replacement_condition_capsule',
            ).exists()
        )

    def test_duplicate_generated_capsule_preserves_richer_local_record(self):
        state = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0113_pls539_direct_handoff_receipt'),
        ])
        apps = state.apps
        Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
        Item = apps.get_model('tasks', 'Item')
        Project = apps.get_model('tasks', 'Project')
        Archive.objects.all().delete()

        project = Project.objects.create(
            org_id=2,
            name='Digital Books',
            key='DBK',
        )
        local_capsule = """## Org-safe condition capsule (hand-minted)

- **Condition summary:** Verify the tenant-local accounting evidence.
- **Verification criteria:** Local Accounting confirms the Drive index.
- **Source status at mint:** needs_triage.
- **Migration stamp:** hand-minted from the local handoff.
"""
        generated_unknown = """## Org-safe condition capsule (migration)

- **Legacy source key:** unknown (foreign organization; do not re-link)
- **Condition summary:** UNKNOWN — source had no org-safe extractable condition.
- **Verification criteria (local evidence only):**
  - [ ] Operator or Admin Ops must restate the condition.
- **Owning team (local):** admin-ops
- **Migration stamp:** 0106_backfill_org_safe_condition_capsules
"""
        duplicate = Item.objects.create(
            project=project,
            item_key='DBK-1',
            mode='task',
            title='Replacement with a richer local capsule',
            description=(
                f'Local introduction.\n\n{local_capsule}\n'
                f'{generated_unknown}\n## Durable publication\nKeep this.'
            ),
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
            tags='data-migration,migration-condition-unknown',
            metadata={
                'org_isolation_replacement': True,
                'org_safe_condition_capsule': {
                    'version': 1,
                    'extraction_state': 'unknown',
                },
            },
        )
        generated_only = Item.objects.create(
            project=project,
            item_key='DBK-2',
            mode='task',
            title='Replacement with one generated capsule',
            description=generated_unknown,
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
            tags='migration-condition-unknown',
        )
        generated_rich = """## Org-safe condition capsule (migration)

- **Condition summary:** Generated source snapshot.
- **Verification criteria (local evidence only):**
  - [ ] Confirm the generated source snapshot.
- **Source status at migration:** blocked (non-terminal)
- **Migration stamp:** 0106_backfill_org_safe_condition_capsules
"""
        duplicate_rich = Item.objects.create(
            project=project,
            item_key='DBK-3',
            mode='task',
            title='Replacement with conflicting rich capsules',
            description=f'{local_capsule}\n{generated_rich}',
            status='todo',
            workflow_name='simple',
            assignment_group='accounting',
        )
        migration = import_module(
            'tasks.migrations.'
            '0114_canonicalize_org_safe_condition_capsules'
        )
        schema_editor = SimpleNamespace(connection=connection)

        migration.canonicalize_condition_capsules(apps, schema_editor)
        migration.canonicalize_condition_capsules(apps, schema_editor)

        duplicate.refresh_from_db()
        generated_only.refresh_from_db()
        duplicate_rich.refresh_from_db()
        self.assertEqual(
            duplicate.description.lower().count(
                '## org-safe condition capsule'
            ),
            1,
        )
        self.assertIn(
            'Verify the tenant-local accounting evidence.',
            duplicate.description,
        )
        self.assertNotIn(
            'UNKNOWN — source had no org-safe extractable condition.',
            duplicate.description,
        )
        self.assertIn('## Durable publication\nKeep this.', duplicate.description)
        self.assertEqual(duplicate.tags, 'data-migration')
        self.assertEqual(
            duplicate.metadata['org_safe_condition_capsule'],
            {
                'version': 2,
                'canonical_source': 'existing_local_capsule',
                'canonical_heading': (
                    '## Org-safe condition capsule (hand-minted)'
                ),
                'canonicalization_migration': (
                    '0114_canonicalize_org_safe_condition_capsules'
                ),
                'superseded_capsule_count': 1,
                'superseded_generated_capsule_count': 1,
            },
        )
        self.assertEqual(generated_only.description, generated_unknown)
        self.assertEqual(generated_only.tags, 'migration-condition-unknown')
        self.assertEqual(
            duplicate_rich.description.lower().count(
                '## org-safe condition capsule'
            ),
            1,
        )
        self.assertIn('needs_triage', duplicate_rich.description)
        self.assertNotIn(
            'Source status at migration:** blocked',
            duplicate_rich.description,
        )
        self.assertEqual(
            Archive.objects.filter(
                kind=(
                    'condition_capsule_canonicalization'
                ),
            ).count(),
            2,
        )

        migration.restore_condition_capsules(apps, schema_editor)

        duplicate.refresh_from_db()
        generated_only.refresh_from_db()
        duplicate_rich.refresh_from_db()
        self.assertIn(generated_unknown, duplicate.description)
        self.assertEqual(
            duplicate.tags,
            'data-migration,migration-condition-unknown',
        )
        self.assertEqual(
            duplicate.metadata['org_safe_condition_capsule'],
            {
                'version': 1,
                'extraction_state': 'unknown',
            },
        )
        self.assertEqual(generated_only.description, generated_unknown)
        self.assertIn(generated_rich, duplicate_rich.description)
