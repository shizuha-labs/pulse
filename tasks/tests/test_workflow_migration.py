"""PLS-54 — pulse_admin_migrate_task_workflow: migrate a task between workflows
with category-based status remap, dry_run preview, reversible metadata, and
admin/CoS role-gating.

Runs in CI (the authoritative gate for shizuha-tasks; the author sandbox's Django
version is incompatible with the repo's models). Self-contained: builds its own
workflows/statuses so it doesn't depend on seeded data.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, Status, TeamRoster, Workflow, WorkflowStatus

User = get_user_model()


def _status(slug, name, category):
    return Status.objects.get_or_create(
        slug=slug, defaults={'name': name, 'category': category})[0]


class WorkflowMigrationTests(APITestCase):
    def setUp(self):
        # Source workflow A — standard categories.
        self.s_open = _status('open', 'Open', 'todo')
        self.s_prog = _status('in_progress', 'In Progress', 'in_progress')
        self.s_review = _status('in_review', 'In Review', 'in_progress')
        self.s_done = _status('done', 'Done', 'done')
        # Target workflow B — DIFFERENT slugs in the SAME categories (proves remap).
        self.b_todo = _status('b_todo', 'B Todo', 'todo')
        self.b_prog = _status('b_prog', 'B Doing', 'in_progress')
        self.b_done = _status('b_done', 'B Closed', 'done')

        self.wf_a = self._mk_wf('Simple Test', 'simple-test',
                                [self.s_open, self.s_prog, self.s_review, self.s_done], self.s_open)
        self.wf_b = self._mk_wf('Autonomous Dev Test', 'autonomous-dev-test',
                                [self.b_todo, self.b_prog, self.b_done], self.b_todo)

        self.project = Project.objects.create(name='Mig Test', key='MIG', org_id=1, access_level='open')

        self.staff = User.objects.create_user('staff', 'staff@example.com', 'pw', is_staff=True)
        self.member = User.objects.create_user('member', 'member@example.com', 'pw', is_staff=False)
        self.admin_ops_backup = User.objects.create_user(
            'admin-ops-backup', 'backup@shizuha.com', 'pw', is_staff=False,
        )
        roster, _ = TeamRoster.objects.get_or_create(
            team_name='admin-ops',
            organization_slug='shizuha',
            organization_id=1,
            defaults={'tiers': []},
        )
        roster.tiers = [[self.admin_ops_backup.email]]
        roster.updated_by = 'test:plat6037'
        roster.save(update_fields=['tiers', 'updated_by', 'updated_at'])
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        self.url = reverse('item-migrate-workflow')

    def _mk_wf(self, name, slug, statuses, initial):
        wf = Workflow.objects.create(name=name, slug=slug, initial_status=initial)
        for pos, st in enumerate(statuses):
            WorkflowStatus.objects.create(workflow=wf, status=st, position=pos)
        return wf

    def _mk_item(self, status_slug):
        # item_key is minted on the serializer/service create paths, not in
        # Item.save(); a bare Item.objects.create leaves it blank, so the migrate
        # endpoint (which resolves tasks by item_key) can't find the row. Mint one.
        return Item.objects.create(mode='task', title='t', project=self.project,
                                   workflow_name=self.wf_a.name, status=status_slug,
                                   item_key=self.project.get_next_item_key())

    # --- pure remap helper: category remap for each status class -----------
    def test_remap_status_by_category(self):
        R = Item.remap_status_for_workflow
        self.assertEqual(R('in_progress', self.wf_b), 'b_prog')   # in_progress class
        self.assertEqual(R('in_review', self.wf_b), 'b_prog')     # in_progress class (review)
        self.assertEqual(R('open', self.wf_b), 'b_todo')          # todo class
        self.assertEqual(R('done', self.wf_b), 'b_done')          # done class
        self.assertEqual(R('b_prog', self.wf_b), 'b_prog')        # already valid → unchanged
        self.assertEqual(R('totally-unknown', self.wf_b), 'b_todo')  # → initial fallback

    # --- dry_run previews without mutating ---------------------------------
    def test_dry_run_no_mutation(self):
        item = self._mk_item('in_progress')
        resp = self.client.post(self.url, {
            'task_keys': [item.item_key], 'target_workflow': 'autonomous-dev-test', 'dry_run': True,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['dry_run'])
        plan = body['results'][0]
        self.assertEqual(plan['to']['status'], 'b_prog')
        self.assertFalse(plan['applied'])
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_a.name)   # unchanged
        self.assertEqual(item.status, 'in_progress')

    # --- apply: status remapped by category + reversible metadata ----------
    def test_apply_migrates_and_records_metadata(self):
        item = self._mk_item('in_progress')
        resp = self.client.post(self.url, {
            'task_keys': [item.item_key], 'target_workflow': 'autonomous-dev-test', 'dry_run': False,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_b.name)
        self.assertEqual(item.status, 'b_prog')                # in_progress category preserved
        migs = (item.metadata or {}).get('workflow_migrations')
        self.assertTrue(migs and migs[-1]['from']['status'] == 'in_progress')
        self.assertEqual(migs[-1]['from']['workflow_name'], self.wf_a.name)

    def test_exact_org_admin_ops_backup_applies_and_records_authority(self):
        item = self._mk_item('in_progress')
        c = APIClient(); c.force_authenticate(user=self.admin_ops_backup)
        resp = c.post(self.url, {
            'task_keys': [item.item_key],
            'target_workflow': 'autonomous-dev-test',
            'dry_run': False,
        }, format='json')

        self.assertEqual(resp.status_code, 200, resp.content)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_b.name)
        migration = item.metadata['workflow_migrations'][-1]
        self.assertEqual(migration['by'], self.admin_ops_backup.email)
        self.assertEqual(migration['authority'], 'admin-ops-delegated')

    def test_admin_ops_backup_cannot_borrow_authority_across_orgs(self):
        foreign_project = Project.objects.create(
            name='Foreign Mig Test', key='FMIG', org_id=2, access_level='open',
        )
        item = Item.objects.create(
            mode='task', title='foreign', project=foreign_project,
            workflow_name=self.wf_a.name, status='in_progress',
            item_key=foreign_project.get_next_item_key(),
        )
        c = APIClient(); c.force_authenticate(user=self.admin_ops_backup)
        resp = c.post(self.url, {
            'task_keys': [item.item_key],
            'target_workflow': 'autonomous-dev-test',
            'dry_run': False,
        }, format='json')

        self.assertEqual(resp.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_a.name)

    # --- role gating: a non-admin can NEVER apply a migration --------------
    def test_role_gating_apply_forbidden(self):
        item = self._mk_item('in_progress')
        c = APIClient(); c.force_authenticate(user=self.member)
        resp = c.post(self.url, {
            'task_keys': [item.item_key], 'target_workflow': 'autonomous-dev-test', 'dry_run': False,
        }, format='json')
        self.assertEqual(resp.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_a.name)   # untouched
        self.assertEqual(item.status, 'in_progress')

    # --- bulk explicit list -------------------------------------------------
    def test_bulk_explicit_list(self):
        a, b = self._mk_item('in_progress'), self._mk_item('done')
        resp = self.client.post(self.url, {
            'task_keys': [a.item_key, b.item_key], 'target_workflow': 'autonomous-dev-test', 'dry_run': False,
        }, format='json')
        self.assertEqual(resp.json()['count'], 2)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.status, 'b_prog')
        self.assertEqual(b.status, 'b_done')

    def test_resolves_project_key_uuid_numeric_key_and_display_key(self):
        project_key = self._mk_item('in_progress')
        uuid_item = self._mk_item('in_progress')
        numeric_key = Item.objects.create(
            mode='task', title='numeric-key', project=self.project, item_key='6559',
            workflow_name=self.wf_a.name, status='in_progress')
        display_key = Item.objects.create(
            mode='task', title='display-key', project=self.project, item_key='6560',
            workflow_name=self.wf_a.name, status='in_progress')

        resp = self.client.post(self.url, {
            'task_keys': [project_key.item_key, str(uuid_item.id), numeric_key.item_key, '#6560'],
            'target_workflow': 'autonomous-dev-test',
            'dry_run': True,
        }, format='json')

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['count'], 4)
        self.assertEqual(
            [r['task_key'] for r in body['results']],
            [project_key.item_key, uuid_item.item_key, '6559', '6560'],
        )
        self.assertTrue(all('error' not in r for r in body['results']))

    def test_not_found_error_names_identifier_type_and_suggestion(self):
        resp = self.client.post(self.url, {
            'task_key': '#999999',
            'target_workflow': 'autonomous-dev-test',
            'dry_run': True,
        }, format='json')

        self.assertEqual(resp.status_code, 200, resp.content)
        result = resp.json()['results'][0]
        self.assertEqual(result['identifier_type'], 'display_key')
        self.assertEqual(result['suggested_identifier'], '999999')
        self.assertIn("display key", result['error'])
        self.assertIn("999999", result['error'])

    # --- reverse round-trip preserves status category ----------------------
    def test_reverse_round_trip(self):
        item = self._mk_item('in_progress')
        self.client.post(self.url, {'task_key': item.item_key,
                                    'target_workflow': 'autonomous-dev-test', 'dry_run': False}, format='json')
        item.refresh_from_db(); self.assertEqual(item.status, 'b_prog')
        # back to A
        self.client.post(self.url, {'task_key': item.item_key,
                                    'target_workflow': 'simple-test', 'dry_run': False}, format='json')
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, self.wf_a.name)
        self.assertEqual(Status.objects.get(slug=item.status).category, 'in_progress')  # category preserved
