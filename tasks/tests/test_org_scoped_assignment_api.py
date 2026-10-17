"""Regression coverage for project-org scoped team and assignment APIs."""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import Item, Project, ProjectMembership, TeamRoster, WebhookKey
from tasks.serializers import AlertSubmitSerializer
from tasks.services.permissions import PermissionService
from tasks.services.workflow_service import WorkflowService
from tasks.views import ItemViewSet


User = get_user_model()


def _organization(org_id):
    return {
        1: {'id': 1, 'slug': 'shizuha', 'name': 'Shizuha'},
        4: {'id': 4, 'slug': 'personal-3', 'name': 'Personal'},
    }.get(int(org_id))


class TeamListOrganizationScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='team-list-admin', email='admin@example.com',
            password='x', is_staff=True,
        )
        self.project = Project.objects.create(
            name='Books', key='BKTEAM', org_id=4, access_level='private',
        )
        self.item = Item.objects.create(
            project=self.project, item_key='BKTEAM-1', mode='task',
            title='Collect records', assignment_group='accounting',
        )
        self.team = TeamRoster.objects.create(
            team_name='accounting', organization_slug='personal-3',
            tiers=[['sato@shizuha.com']], is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch.object(
        ItemViewSet,
        '_get_admin_teams',
        return_value=[{
            'id': 28,
            'name': 'Accounting',
            'slug': 'accounting',
            'lead_email': 'sato@shizuha.com',
        }, {
            'id': 29,
            'name': 'Directory Only',
            'slug': 'directory-only',
            'lead_email': 'foreign@shizuha.com',
        }],
    )
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_project_context_derives_org_and_returns_stable_identity(
        self, _get_org, get_teams,
    ):
        response = self.client.get(
            '/api/teams/', {'project_id': self.project.id},
        )

        self.assertEqual(response.status_code, 200, response.data)
        get_teams.assert_called_once_with(4, 'personal-3')
        self.assertEqual(response.data['organization_id'], 4)
        self.assertEqual(response.data['organization_slug'], 'personal-3')
        self.assertEqual(response.data['project_id'], self.project.id)
        self.assertNotIn(
            'directory-only',
            {row['team_key'] for row in response.data['teams']},
        )
        team = response.data['teams'][0]
        self.assertEqual(team['team_id'], self.team.id)
        self.assertEqual(team['admin_team_id'], 28)
        self.assertEqual(team['organization_id'], 4)
        self.assertEqual(team['organization_slug'], 'personal-3')
        self.assertEqual(team['organization_name'], 'Personal')
        self.assertFalse(team['shared'])
        self.assertFalse(team['is_shared_platform'])
        self.assertEqual(team['canonical_name'], 'accounting')
        self.assertEqual(team['team_key'], 'accounting')
        self.assertEqual(team['canonical_key'], 'personal-3:accounting')

    @patch.object(ItemViewSet, '_get_admin_teams', return_value=[])
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_item_context_is_authorized_and_scoped(self, _get_org, get_teams):
        response = self.client.get(
            '/api/teams/', {'item_key': self.item.item_key},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['item_key'], self.item.item_key)
        get_teams.assert_called_once_with(4, 'personal-3')

    @patch.object(ItemViewSet, '_get_admin_teams', return_value=[])
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_foreign_platform_team_is_not_exposed(self, *_):
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['revi@shizuha.com']], 'is_active': True},
        )

        response = self.client.get('/api/teams/', {'project_id': self.project.id})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(
            'review',
            {row['team_key'] for row in response.data['teams']},
        )

    def test_missing_or_caller_selected_org_scope_fails_closed(self):
        self.assertEqual(self.client.get('/api/teams/').status_code, 400)
        response = self.client.get(
            '/api/teams/',
            {'project_id': self.project.id, 'organization': 1},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('derived', response.data['error'])

    def test_inaccessible_project_is_non_enumerating(self):
        outsider = User.objects.create_user(
            username='team-list-outsider', email='outsider@example.com', password='x',
        )
        self.client.force_authenticate(outsider)
        with patch.object(
            PermissionService,
            'get_user_projects',
            return_value=Project.objects.none(),
        ):
            response = self.client.get(
                '/api/teams/', {'project_id': self.project.id},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['error'], 'context_not_found')


class ExactBusinessTeamGuardTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Personal Books', key='BKGRD', org_id=4,
        )
        self.item = Item.objects.create(
            project=self.project, item_key='BKGRD-1', mode='task',
            title='File return', assignment_group='accounting',
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='personal-3',
            tiers=[['sato@shizuha.com']], is_active=True,
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='shizuha',
            tiers=[['banto@shizuha.com']], is_active=True,
        )
        # ``review`` is a multi-org routing lane: it exists in the platform org
        # AND in shizuha-digital, so it is NOT shizuha-only. The PLS-1069
        # cell-C fallback must therefore NOT borrow the platform ``review`` row
        # for a personal-3 task (PLAT-4213 no-foreign-borrowing guard).
        TeamRoster.objects.create(
            team_name='review', organization_slug='shizuha-digital',
            tiers=[['revi@shizuha.com']], is_active=True,
        )

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_exact_member_allowed_and_foreign_or_unknown_target_denied(self, _):
        self.assertFalse(WorkflowService.assignment_org_mismatch(
            self.item, 'sato@shizuha.com', 'accounting',
        )[0])
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            self.item, 'banto@shizuha.com', 'accounting',
        )[0])
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'unknown@example.com', 'accounting',
        )
        self.assertTrue(mismatch)
        self.assertEqual(scope, 'not-in-team')
        self.assertEqual(task_org, 'personal-3')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value=None,
    )
    def test_unresolved_task_org_denies_business_assignment(self, _):
        # Known organization IDs have a persisted canonical slug fallback so
        # an Admin API outage does not make valid local work unroutable. An
        # unknown organization identity must still fail closed.
        self.item.project.org_id = 999
        mismatch, _scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'sato@shizuha.com', 'accounting',
        )
        self.assertTrue(mismatch)
        self.assertEqual(task_org, 'unresolved')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_platform_lane_does_not_borrow_foreign_agent(self, _):
        """PLAT-4213 no-foreign-borrowing guard: ``review`` is a multi-org lane
        (exists in shizuha AND shizuha-digital per setUp), so it is NOT
        shizuha-only. The PLS-1069 cell-C fallback must NOT borrow the platform
        ``review`` row for a personal-3 task — a shizuha member of ``review``
        stays fail-closed, as does an unknown target."""
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['banto@shizuha.com']], 'is_active': True},
        )
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            self.item, 'banto@shizuha.com', 'review',
        )[0])
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            self.item, 'unknown@example.com', 'review',
        )[0])

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_shizuha_only_team_cell_c_falls_back_for_foreign_task(self, _):
        """PLS-1069 acceptance criterion 2 (cell C): a genuinely shizuha-only
        team (no active row in any non-shizuha org — e.g. ``admin-ops``) DOES
        fall back to the platform roster for a non-shizuha task, so a shizuha
        member of that lane is authorized to Approve (matching the assignment
        path's cell C in ``_team_registered_for_item``, #321). An unknown
        target still fails closed."""
        # ``admin-ops`` is seeded by migration 0055 in the shizuha org; ensure
        # its tiers name a known member (update_or_create, not create — the
        # row already exists).
        TeamRoster.objects.update_or_create(
            team_name='admin-ops', organization_slug='shizuha',
            defaults={'tiers': [['ich@shizuha.com']], 'is_active': True},
        )
        # No org-local admin-ops row and the team is shizuha-only -> fallback
        # authorizes the platform member for the personal-3 task.
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'ich@shizuha.com', 'admin-ops',
        )
        self.assertFalse(mismatch, (mismatch, scope, task_org))
        self.assertEqual(task_org, 'personal-3')
        # Unknown target still fails closed.
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            self.item, 'unknown@example.com', 'admin-ops',
        )[0])

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_alert_serializer_cell_c_falls_back_to_platform_team(self, _):
        """PLS-731 cell C: with the org-local row deleted, a slug that still
        exists in the platform (shizuha) org remains routable — the majority
        case (20 of 22 platform slugs have no non-shizuha counterpart), not an
        edge. Rejecting it would strand shizuha-only escalation lanes
        (admin-ops/human-ops) from non-shizuha orgs."""
        self.assertTrue(AlertSubmitSerializer(data={
            'title': 'Tax alert',
            'source': 'unit-test',
            'project_id': self.project.id,
            'assignment_group': 'accounting',
        }).is_valid())

        TeamRoster.objects.filter(
            organization_slug='personal-3',
            team_name='accounting',
        ).delete()
        serializer = AlertSubmitSerializer(data={
            'title': 'Tax alert',
            'source': 'unit-test',
            'project_id': self.project.id,
            'assignment_group': 'accounting',
        })
        # Cell-C platform fallback: the shizuha 'accounting' row keeps the
        # team routable even though the org-local row is gone.
        self.assertTrue(serializer.is_valid(), serializer.errors)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_transition_validation_same_org_slug_keyed_team_found(self, _):
        """PLS-1069 (PLS-731 residual): the transition-validation path
        (``assignment_org_mismatch`` team branch via
        ``_team_roster_entry_for_item``) must find a team row keyed by the
        task's own org SLUG even when ``organization_id`` is NULL. Previously
        the lookup filtered by ``organization_id`` alone, so a shizuha task
        could not find its own ``organization_slug='shizuha'`` team row and
        failed closed on identical same-org strings (``shizuha != shizuha``),
        breaking Approve on workflow-change tasks (PLAT-4806/4807)."""
        # A shizuha-org task whose team row is keyed by slug only (the common
        # authoring shape; organization_id may be NULL).
        shizuha_project = Project.objects.create(
            name='Platform', key='PLSHZ', org_id=1,
        )
        shizuha_item = Item.objects.create(
            project=shizuha_project, item_key='PLSHZ-1', mode='task',
            title='Workflow change', assignment_group='review',
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['banto@shizuha.com']], 'is_active': True},
        )
        # Same-org (shizuha == shizuha): the slug-keyed row is found and the
        # member is authorized — no spurious mismatch.
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            shizuha_item, 'banto@shizuha.com', 'review',
        )
        self.assertFalse(mismatch, (mismatch, scope, task_org))
        self.assertEqual(task_org, 'shizuha')
        # A non-member still fails closed.
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            shizuha_item, 'unknown@example.com', 'review',
        )[0])
        # Cross-org guard (PLAT-4213) preserved: 'review' is a multi-org lane
        # (NOT shizuha-only — it also exists in shizuha-digital per setUp), so
        # the platform 'review' row must NOT be borrowed for a personal-3 task.
        self.assertTrue(WorkflowService.assignment_org_mismatch(
            self.item, 'banto@shizuha.com', 'review',
        )[0])


class ItemOrganizationMutationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='project-move-admin', email='move@example.com',
            password='x', is_staff=True,
        )
        self.source = Project.objects.create(
            name='Books', key='BKMOV', org_id=4,
        )
        self.foreign = Project.objects.create(
            name='Platform', key='PLMOV', org_id=1,
        )
        self.item = Item.objects.create(
            project=self.source, item_key='BKMOV-1', mode='task',
            title='Tenant-bound item',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_patch_cannot_move_item_across_organizations(self):
        response = self.client.patch(
            f'/api/items/{self.item.id}/',
            {'project': self.foreign.id},
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.project_id, self.source.id)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_patch_cell_c_routes_team_owned_only_by_platform_org(self, _):
        """PLS-731 cell C: a team owned ONLY by the platform (shizuha) org, with
        no org-local row, stays routable from a non-shizuha task — the majority
        case (20 of 22 platform slugs have no non-shizuha counterpart)."""
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='shizuha',
            tiers=[['banto@shizuha.com']], is_active=True,
        )

        response = self.client.patch(
            f'/api/items/{self.item.id}/',
            {'assignment_group': 'accounting'},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignment_group, 'accounting')


class WebhookTeamBoundaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='webhook-org-admin', email='webhook@example.com',
            password='x', is_staff=True,
        )
        self.project = Project.objects.create(
            name='Books', key='BKWEB', org_id=4, access_level='open',
        )
        self.foreign_project = Project.objects.create(
            name='Global Alerts', key='GLWEB', org_id=1, access_level='open',
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='personal-3',
            tiers=[['sato@shizuha.com']], is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_webhook_team_alias_is_canonicalized_in_project_org(self, _):
        response = self.client.post('/api/webhook-keys/', {
            'name': 'Personal finance alerts',
            'integration_type': 'generic',
            'default_project': self.project.id,
            'default_assignment_group': 'personal-accounting',
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        key = WebhookKey.objects.get(pk=response.data['id'])
        self.assertEqual(key.default_project_id, self.project.id)
        self.assertEqual(key.default_assignment_group, 'accounting')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_webhook_update_cannot_retain_team_across_project_org_change(self, _):
        key = WebhookKey.objects.create(
            user_id=self.user.id,
            user_email=self.user.email,
            name='Scoped finance alerts',
            integration_type='generic',
            default_project=self.project,
            default_assignment_group='accounting',
        )

        response = self.client.patch(
            f'/api/webhook-keys/{key.id}/',
            {'default_project': self.foreign_project.id},
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        key.refresh_from_db()
        self.assertEqual(key.default_project_id, self.project.id)
        self.assertEqual(key.default_assignment_group, 'accounting')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_public_alert_cannot_select_an_organization_project(self, _):
        self.client.force_authenticate(user=None)
        response = self.client.post('/api/alerts/', {
            'title': 'Anonymous routed alert',
            'source': 'unit-test',
            'source_id': 'anonymous-routed-alert',
            'project_id': self.project.id,
            'assignment_group': 'accounting',
        }, format='json')

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Item.objects.filter(
            source_id='anonymous-routed-alert',
        ).exists())


class BulkAssignmentOrganizationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='bulk-org-admin', email='bulk@example.com',
            password='x', is_staff=True,
        )
        self.global_project = Project.objects.create(
            name='Global Books', key='GBULK', org_id=1,
        )
        self.personal_project = Project.objects.create(
            name='Personal Books', key='PBULK', org_id=4,
        )
        self.global_item = Item.objects.create(
            project=self.global_project, item_key='GBULK-1', mode='task',
            title='Global ledger', assignment_group='accounting',
        )
        self.personal_item = Item.objects.create(
            project=self.personal_project, item_key='PBULK-1', mode='task',
            title='Personal ledger', assignment_group='accounting',
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='shizuha',
            tiers=[['banto@shizuha.com']], is_active=True,
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='personal-3',
            tiers=[['sato@shizuha.com']], is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch.object(
        Item,
        '_resolve_user_ref',
        return_value=(99, 'banto@shizuha.com'),
    )
    @patch(
        'tasks.views.user_service.get_user_by_id',
        return_value={'id': 99, 'email': 'banto@shizuha.com'},
    )
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_mixed_org_batch_is_rejected_before_any_write(self, *_):
        response = self.client.post(
            '/api/items/bulk/',
            {
                'operation': 'assign',
                'item_ids': [self.global_item.id, self.personal_item.id],
                'data': {
                    'assignee_id': 99,
                    'assignee_email': 'banto@shizuha.com',
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.global_item.refresh_from_db()
        self.personal_item.refresh_from_db()
        self.assertIsNone(self.global_item.assignee_id)
        self.assertIsNone(self.personal_item.assignee_id)

    @patch.object(
        Item,
        '_resolve_user_ref',
        return_value=(99, 'banto@shizuha.com'),
    )
    @patch(
        'tasks.views.user_service.get_user_by_id',
        return_value={'id': 99, 'email': 'banto@shizuha.com'},
    )
    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_batch_requires_item_aware_team_membership_even_for_shared_lane(self, *_):
        self.global_item.assignment_group = 'review'
        self.global_item.save(update_fields=['assignment_group'])
        response = self.client.post(
            '/api/items/bulk/',
            {
                'operation': 'assign',
                'item_ids': [self.global_item.id],
                'data': {
                    'assignee_id': 99,
                    'assignee_email': 'banto@shizuha.com',
                },
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.global_item.refresh_from_db()
        self.assertIsNone(self.global_item.assignee_id)


class AssignmentAuthorizationSeparationTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            username='assignment-admin', email='assignment-admin@example.com',
            password='x', is_staff=True,
        )
        self.target = User.objects.create_user(
            username='exact-item-owner', email='owner@example.com', password='x',
        )
        self.target.organization_memberships = {4: 'member'}
        self.project = Project.objects.create(
            name='Private Books', key='BKAUTH', org_id=4,
            access_level='private',
        )
        self.item = Item.objects.create(
            project=self.project, item_key='BKAUTH-1', mode='task',
            title='Exact item ownership only',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    @patch(
        'tasks.services.admin_service.admin_service.is_org_member',
        return_value=True,
    )
    @patch('tasks.user_service.user_service.resolve_user_info')
    @patch('tasks.user_service.user_service.get_user_by_id')
    def test_assignment_grants_exact_item_access_not_project_membership(
        self, get_user, resolve_user, _is_member, _get_org,
    ):
        user_record = {
            'id': self.target.id,
            'email': self.target.email,
            'username': self.target.username,
            'full_name': 'Exact Item Owner',
            'is_agent': False,
        }
        get_user.return_value = user_record
        resolve_user.return_value = user_record

        response = self.client.post(
            f'/api/items/{self.item.id}/assign/',
            {
                'assignee_id': self.target.id,
                'assignee_email': self.target.email,
                'direct_assignment_reason': 'explicit one-task handoff',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(ProjectMembership.objects.filter(
            project=self.project, user_id=self.target.id,
        ).exists())
        self.assertTrue(PermissionService.get_user_item_queryset(
            self.target,
        ).filter(pk=self.item.pk).exists())

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    @patch(
        'tasks.services.admin_service.admin_service.is_org_member',
        return_value=False,
    )
    @patch('tasks.user_service.user_service.resolve_user_info')
    @patch('tasks.user_service.user_service.get_user_by_id')
    def test_direct_assignment_cannot_bypass_task_organization(
        self, get_user, resolve_user, _is_member, _get_org,
    ):
        user_record = {
            'id': self.target.id,
            'email': self.target.email,
            'username': self.target.username,
            'is_agent': False,
        }
        get_user.return_value = user_record
        resolve_user.return_value = user_record

        response = self.client.post(
            f'/api/items/{self.item.id}/assign/',
            {
                'assignee_id': self.target.id,
                'assignee_email': self.target.email,
                'direct_assignment_reason': 'foreign handoff attempt',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)


class LegacyCreateWorkflowResolverTests(TestCase):
    def test_assign_team_post_function_passes_the_concrete_item(self):
        project = Project.objects.create(name='Books', key='BKLEG', org_id=4)
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='personal-3',
            defaults={'tiers': [['sato@shizuha.com']], 'is_active': True},
        )
        item = Item.objects.create(
            project=project, item_key='BKLEG-1', mode='task',
            title='Legacy create transition', created_by_email='author@example.com',
        )
        transition = SimpleNamespace(
            post_functions=[{'type': 'assign_team', 'value': 'accounting'}],
        )
        view = ItemViewSet()

        with patch(
            'tasks.services.admin_service.admin_service.get_organization',
            side_effect=_organization,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(17, 'sato@shizuha.com'),
        ) as resolver, patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
        ) as apply_assignee:
            view._execute_workflow_post_functions(item, transition)

        resolver.assert_called_once_with(
            'accounting',
            exclude_email='author@example.com',
            project_key='BKLEG',
            allow_operator=False,
            item=item,
        )
        apply_assignee.assert_called_once()


class ImportTargetAuthorizationTests(TestCase):
    def test_target_lookup_requires_create_authority_and_is_non_enumerating(self):
        user = User.objects.create_user(
            username='import-user', email='import@example.com', password='x',
        )
        project = Project.objects.create(
            name='Private Import', key='IMPORG', org_id=4,
            access_level='private',
        )
        request = SimpleNamespace(user=user)

        with patch.object(
            PermissionService,
            'get_user_projects',
            return_value=Project.objects.none(),
        ):
            self.assertIsNone(ItemViewSet._authorized_import_target_project(
                request, project.id,
            ))
        with patch.object(
            PermissionService,
            'get_user_projects',
            return_value=Project.objects.filter(pk=project.pk),
        ), patch.object(
            PermissionService,
            'can_access_project',
            return_value=False,
        ):
            self.assertIsNone(ItemViewSet._authorized_import_target_project(
                request, project.id,
            ))
