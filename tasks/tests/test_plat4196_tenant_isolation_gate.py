"""PLAT-4196 deploy gate for Pulse's tenant-data boundary.

PLAT-4195 proved that a green generic test suite was not enough: request-facing
item child resources and the bulk endpoint could grow raw global query paths
without any test explicitly requiring the canonical tenant scope.  This module
is intentionally narrow and fail-loud.  It pins the protected route manifest,
the implementation invariants for those routes, and adversarial two-org
requests (including client-controlled organization hints).

The full behavioral matrices remain in the three PLAT-4195 modules.  The Origin
deploy workflow runs this gate and those modules explicitly before the full
suite, so an isolation regression stops the release at the shortest useful
failure.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import resolve
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    AutomationRule, Board, BoardColumn, BoardQuickFilter, Comment, Component,
    CustomField, CustomFieldValue, Dashboard, DashboardGadget, Item, Project,
    ProjectConfiguration, ProjectMembership, ProjectWorkflow, SecurityScheme,
    Sprint, Status, TaskDraftProposal, Workflow, WorkLog,
)
from tasks.views import (
    CommentViewSet,
    CustomFieldValueViewSet,
    ItemBulkOperationView,
    ItemViewSet,
    WorkLogViewSet,
)


User = get_user_model()


# Every tenant-data route added to this boundary must state the mechanism that
# scopes it.  A new child/bulk route is not allowed to inherit a vague
# "authenticated" assumption; add it here together with an invariant test.
TENANT_DATA_ROUTE_MANIFEST = {
    '/api/items/': (ItemViewSet, '_get_user_scoped_queryset'),
    '/api/items/bulk/': (ItemBulkOperationView, 'PermissionService.get_user_item_queryset'),
    '/api/comments/1/': (CommentViewSet, 'PermissionService.get_user_item_queryset'),
    '/api/work-logs/1/': (WorkLogViewSet, 'PermissionService.get_user_item_queryset'),
    '/api/custom-field-values/1/': (
        CustomFieldValueViewSet,
        'PermissionService.get_user_item_queryset',
    ),
}


class PLAT4196TenantIsolationGateTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_a = User.objects.create_user(
            username='plat4196-a', email='plat4196-a@sec.test', password='x', is_staff=False)
        self.user_a.organization_memberships = {4101: 'owner'}
        self.user_b = User.objects.create_user(
            username='plat4196-b', email='plat4196-b@sec.test', password='x', is_staff=False)
        self.user_b.organization_memberships = {4102: 'owner'}

        self.project_a = Project.objects.create(
            name='PLAT4196 A', key='P4A', org_id=4101,
            access_level='private', is_active=True)
        self.project_b = Project.objects.create(
            name='PLAT4196 B', key='P4B', org_id=4102,
            access_level='private', is_active=True)
        self.item_a = Item.objects.create(
            mode='task', title='PLAT4196-A-MARKER', project=self.project_a,
            created_by_id=self.user_a.id, created_by_email=self.user_a.email)
        self.item_b = Item.objects.create(
            mode='task', title='PLAT4196-B-SECRET', project=self.project_b,
            created_by_id=self.user_b.id, created_by_email=self.user_b.email)
        self.comment_b = Comment.objects.create(
            item=self.item_b, author_id=self.user_b.id,
            author_email=self.user_b.email, content='PLAT4196-B-COMMENT')
        self.worklog_b = WorkLog.objects.create(
            item=self.item_b, user_id=self.user_b.id,
            user_email=self.user_b.email, duration_minutes=11,
            worked_date='2026-07-11', description='PLAT4196-B-WORKLOG')
        self.field = CustomField.objects.create(
            name='PLAT4196 marker', slug='plat4196-marker', field_type='text')
        self.value_b = CustomFieldValue.objects.create(
            item=self.item_b, custom_field=self.field,
            value='PLAT4196-B-CFV', value_text='PLAT4196-B-CFV')

    def _as_a(self):
        self.client.force_authenticate(self.user_a)

    def _org_b_hints(self):
        # Client-selected org context is a narrowing hint only.  It must never
        # grant membership in B or change the principal's canonical scope.
        return {
            'HTTP_X_ORGANIZATION_ID': '4102',
            'HTTP_X_ORG_ID': '4102',
            'HTTP_X_SHIZUHA_ORGANIZATION_ID': '4102',
        }

    def test_manifest_routes_resolve_to_the_reviewed_classes(self):
        for path, (expected_class, _) in TENANT_DATA_ROUTE_MANIFEST.items():
            with self.subTest(path=path):
                resolved_class = getattr(resolve(path).func, 'cls', None)
                self.assertIs(
                    resolved_class, expected_class,
                    f'{path} changed owner; classify and test its tenant boundary before release',
                )

    def test_manifest_classes_retain_canonical_scope_mechanisms(self):
        for path, (view_class, marker) in TENANT_DATA_ROUTE_MANIFEST.items():
            with self.subTest(path=path, view=view_class.__name__):
                source = inspect.getsource(view_class)
                self.assertIn(
                    marker, source,
                    f'{view_class.__name__} lost {marker!r}; authenticated-only is not tenant authz',
                )

        bulk_source = inspect.getsource(ItemBulkOperationView)
        self.assertIn('ItemProjectPermission', bulk_source)
        self.assertIn('_filter_mutation_authorized_items', bulk_source)

    def test_cross_org_marker_stays_unreadable_after_warm_and_org_smuggling(self):
        self._as_a()
        for attempt in range(2):  # second request catches unsafe shared-cache reuse
            with self.subTest(attempt=attempt):
                response = self.client.get(
                    f'/api/items/{self.item_b.id}/?organization_id=4102&org_id=4102',
                    **self._org_b_hints(),
                )
                # ItemProjectPermission may reject before or after queryset
                # resolution depending on the route/action; both 403 and 404
                # are deny outcomes.  The marker/state assertions below are
                # the non-disclosure invariant, not one exact deny code.
                self.assertIn(response.status_code, (403, 404))

        own = self.client.get(f'/api/items/{self.item_a.id}/')
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.data['title'], 'PLAT4196-A-MARKER')

    def test_cross_org_child_reads_stay_non_enumerating_with_org_smuggling(self):
        self._as_a()
        headers = self._org_b_hints()
        for path in (
            f'/api/comments/{self.comment_b.id}/?organization_id=4102',
            f'/api/comments/{self.comment_b.id}/reactions/?organization_id=4102',
            f'/api/work-logs/{self.worklog_b.id}/?organization_id=4102',
            f'/api/work-logs/by_item/?item={self.item_b.id}&organization_id=4102',
            f'/api/custom-field-values/{self.value_b.id}/?organization_id=4102',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, **headers).status_code, 404)

        values = self.client.get(
            f'/api/custom-field-values/?item_id={self.item_b.id}&organization_id=4102',
            **headers,
        )
        self.assertEqual(values.status_code, 200)
        rows = values.data.get('results') if isinstance(values.data, dict) else values.data
        self.assertEqual(rows or [], [])

    def test_cross_org_bulk_mutations_have_zero_effect(self):
        self._as_a()
        headers = self._org_b_hints()
        original = (self.item_b.status, self.item_b.title)

        transitioned = self.client.post(
            '/api/items/bulk/?organization_id=4102',
            {
                'item_ids': [self.item_b.id],
                'operation': 'transition',
                'data': {'to_status': 'done'},
            },
            format='json', **headers,
        )
        self.assertEqual(transitioned.status_code, 200)
        self.assertEqual(transitioned.data.get('updated'), 0)

        deleted = self.client.post(
            '/api/items/bulk/?organization_id=4102',
            {'item_ids': [self.item_b.id], 'operation': 'delete'},
            format='json', **headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.data.get('deleted'), 0)
        self.item_b.refresh_from_db()
        self.assertEqual((self.item_b.status, self.item_b.title), original)

    def test_project_owned_crud_is_scoped_and_foreign_state_is_unchanged(self):
        component_b = Component.objects.create(
            project=self.project_b, name='PLAT4196-B-COMPONENT-SECRET'
        )
        self._as_a()

        listed = self.client.get('/api/components/?project=%s' % self.project_b.id,
                                 **self._org_b_hints())
        self.assertEqual(listed.status_code, 200)
        rows = listed.data.get('results') if isinstance(listed.data, dict) else listed.data
        self.assertEqual(rows or [], [])
        self.assertEqual(
            self.client.get(f'/api/components/{component_b.id}/', **self._org_b_hints()).status_code,
            404,
        )
        denied = self.client.patch(
            f'/api/components/{component_b.id}/', {'name': 'ATTACKED'},
            format='json', **self._org_b_hints(),
        )
        self.assertEqual(denied.status_code, 404)
        component_b.refresh_from_db()
        self.assertEqual(component_b.name, 'PLAT4196-B-COMPONENT-SECRET')

    def test_global_configuration_is_readable_but_nonstaff_writes_are_denied(self):
        global_status = Status.objects.create(
            name='PLAT4196 Global', slug='plat4196-global', category='todo'
        )
        self._as_a()
        self.assertEqual(self.client.get(f'/api/statuses/{global_status.id}/').status_code, 200)
        denied = self.client.patch(
            f'/api/statuses/{global_status.id}/', {'name': 'ATTACKED'}, format='json'
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            self.client.post('/api/statuses/', {
                'name': 'ATTACKED NEW', 'slug': 'attacked-new', 'category': 'todo'
            }, format='json').status_code,
            403,
        )
        global_status.refresh_from_db()
        self.assertEqual(global_status.name, 'PLAT4196 Global')

    def test_private_dashboard_and_gadgets_are_owner_scoped(self):
        dashboard_b = Dashboard.objects.create(
            name='PLAT4196-B-DASHBOARD-SECRET', owner_id=self.user_b.id,
            owner_email=self.user_b.email, visibility='private',
        )
        gadget_b = DashboardGadget.objects.create(
            dashboard=dashboard_b, gadget_type='filter_results', title='PLAT4196-B-GADGET'
        )
        self._as_a()
        rows = self.client.get('/api/dashboards/', **self._org_b_hints()).data
        rows = rows.get('results') if isinstance(rows, dict) else rows
        self.assertNotIn(str(dashboard_b.id), {str(row.get('id')) for row in (rows or [])})
        self.assertEqual(self.client.get(f'/api/dashboards/{dashboard_b.id}/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/dashboard-gadgets/{gadget_b.id}/').status_code, 404)
        self.assertEqual(
            self.client.patch(
                f'/api/dashboards/{dashboard_b.id}/', {'name': 'ATTACKED'}, format='json'
            ).status_code,
            404,
        )
        dashboard_b.refresh_from_db()
        self.assertEqual(dashboard_b.name, 'PLAT4196-B-DASHBOARD-SECRET')

    def test_dashboard_visibility_contract_is_owner_only_except_public(self):
        dashboards = {
            visibility: Dashboard.objects.create(
                name=f'PLAT4196-B-{visibility}',
                owner_id=self.user_b.id,
                owner_email=self.user_b.email,
                visibility=visibility,
            )
            for visibility in ('private', 'project', 'org', 'public')
        }
        self._as_a()

        rows = self.client.get('/api/dashboards/').data
        rows = rows.get('results') if isinstance(rows, dict) else rows
        visible_ids = {str(row['id']) for row in rows}
        self.assertIn(str(dashboards['public'].id), visible_ids)
        for visibility in ('private', 'project', 'org'):
            with self.subTest(visibility=visibility):
                self.assertNotIn(str(dashboards[visibility].id), visible_ids)
                self.assertEqual(
                    self.client.get(
                        f'/api/dashboards/{dashboards[visibility].id}/'
                    ).status_code,
                    404,
                )
        self.assertEqual(
            self.client.get(f'/api/dashboards/{dashboards["public"].id}/').status_code,
            200,
        )

    def test_board_quick_filters_are_project_scoped_for_read_and_create(self):
        board_b = Board.objects.get(project=self.project_b, is_default=True)
        quick_filter_b = BoardQuickFilter.objects.create(
            board=board_b,
            name='PLAT4196-B-QUICK-FILTER',
            jql='priority = Highest',
        )
        self._as_a()

        listed = self.client.get(
            f'/api/board-quick-filters/?board_id={board_b.id}',
        )
        self.assertEqual(listed.status_code, 200)
        rows = listed.data.get('results') if isinstance(listed.data, dict) else listed.data
        self.assertEqual(rows or [], [])
        self.assertEqual(
            self.client.get(
                f'/api/board-quick-filters/{quick_filter_b.id}/',
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f'/api/board-quick-filters/?board_id={board_b.id}',
                **self._org_b_hints(),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                '/api/board-quick-filters/',
                {
                    'board': str(board_b.id),
                    'name': 'ATTACKED',
                    'jql': 'status = Done',
                },
                format='json',
                **self._org_b_hints(),
            ).status_code,
            403,
        )
        self.assertFalse(
            BoardQuickFilter.objects.filter(board=board_b, name='ATTACKED').exists()
        )

    def test_create_without_edit_cannot_run_detail_or_collection_mutations(self):
        actor = User.objects.create_user(
            username='plat4196-create-only',
            email='plat4196-create-only@sec.test',
            password='x',
            is_staff=False,
        )
        actor.organization_memberships = {4101: 'member'}
        ProjectMembership.objects.create(
            project=self.project_a,
            user_id=actor.id,
            user_email=actor.email,
            role='member',
            can_browse=True,
            can_create=True,
            can_edit=False,
        )
        rule = AutomationRule.objects.create(
            project=self.project_a,
            name='PLAT4196 create-only rule',
            trigger_type='manual',
            is_enabled=True,
        )
        sprint = Sprint.objects.create(
            project=self.project_a,
            name='PLAT4196 create-only sprint',
        )
        project_workflow, _ = ProjectWorkflow.objects.get_or_create(
            project=self.project_a,
            defaults={
                'statuses': [
                    {
                        'name': 'pending', 'label': 'Pending',
                        'category': 'todo', 'color': '#6b7280',
                    }
                ],
                'initial_status': 'pending',
            },
        )
        column_a = BoardColumn.objects.create(
            project=self.project_a,
            name='PLAT4196 create-only column',
            position=7,
        )
        original_statuses = list(project_workflow.statuses)
        self.client.force_authenticate(actor)

        self.assertEqual(
            self.client.post(f'/api/automation-rules/{rule.id}/toggle/').status_code,
            403,
        )
        # SprintViewSet's legacy list scope calls the admin-backed resolver
        # without the request user object.  Hold that read scope constant so
        # this regression isolates the detail-POST permission mapping.
        with patch(
            'tasks.views.PermissionService.get_user_projects',
            return_value=Project.objects.filter(pk=self.project_a.pk),
        ):
            self.assertEqual(
                self.client.post(f'/api/sprints/{sprint.id}/start/').status_code,
                403,
            )
        self.assertEqual(
            self.client.post(
                f'/api/project-workflows/{project_workflow.id}/add_status/',
                {
                    'name': 'attacked', 'label': 'Attacked',
                    'category': 'todo', 'color': '#000000',
                },
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                '/api/board-columns/reorder/',
                {'column_ids': [str(column_a.id)]},
                format='json',
            ).status_code,
            403,
        )
        rule.refresh_from_db()
        sprint.refresh_from_db()
        project_workflow.refresh_from_db()
        column_a.refresh_from_db()
        self.assertTrue(rule.is_enabled)
        self.assertEqual(sprint.status, 'planning')
        self.assertEqual(project_workflow.statuses, original_statuses)
        self.assertEqual(column_a.position, 7)

    def test_project_configuration_crud_denies_foreign_and_preserves_authorized(self):
        original_scheme = SecurityScheme.objects.create(
            name='PLAT4196 foreign original',
            slug='plat4196-foreign-original',
        )
        replacement_scheme = SecurityScheme.objects.create(
            name='PLAT4196 replacement',
            slug='plat4196-replacement',
        )
        foreign_config = ProjectConfiguration.objects.create(
            project=self.project_b,
            security_scheme=original_scheme,
        )
        self._as_a()
        detail = f'/api/project-configurations/{foreign_config.pk}/'

        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(
            self.client.patch(
                detail,
                {'security_scheme': str(replacement_scheme.pk)},
                format='json',
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.put(
                detail,
                {
                    'project': self.project_b.id,
                    'security_scheme': str(replacement_scheme.pk),
                },
                format='json',
            ).status_code,
            403,
        )
        self.assertEqual(self.client.delete(detail).status_code, 404)
        foreign_config.refresh_from_db()
        self.assertEqual(foreign_config.project_id, self.project_b.id)
        self.assertEqual(foreign_config.security_scheme_id, original_scheme.id)

        created = self.client.post(
            '/api/project-configurations/',
            {'project': self.project_a.id},
            format='json',
        )
        self.assertEqual(created.status_code, 201)
        own_config = ProjectConfiguration.objects.get(project=self.project_a)
        self.assertEqual(
            self.client.get(
                f'/api/project-configurations/{own_config.pk}/'
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/project-configurations/{own_config.pk}/',
                {'security_scheme': str(replacement_scheme.pk)},
                format='json',
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                f'/api/project-configurations/{own_config.pk}/',
                {
                    'project': self.project_a.id,
                    'security_scheme': str(replacement_scheme.pk),
                },
                format='json',
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete(
                f'/api/project-configurations/{own_config.pk}/'
            ).status_code,
            204,
        )

    def test_draft_creation_derives_non_default_org_from_authorized_project(self):
        workflow = Workflow.objects.create(
            name='PLAT4196 Draft Workflow',
            slug='plat4196-draft-workflow',
            initial_status=Status.objects.get(slug='open'),
        )
        self._as_a()
        response = self.client.post(
            '/api/ai-work-intake/drafts/',
            {
                'raw_prompt': 'PLAT4196 non-default org draft',
                'proposed_project_id': self.project_a.id,
                'proposed_workflow_slug': workflow.slug,
                'proposed_title': 'PLAT4196 non-default org draft',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        proposal = TaskDraftProposal.objects.get(pk=response.data['id'])
        self.assertEqual(proposal.org_id, self.project_a.org_id)

    def test_draft_confirm_rejects_foreign_parent_without_creating_item(self):
        workflow = Workflow.objects.create(
            name='PLAT4196 Parent Workflow',
            slug='plat4196-parent-workflow',
            initial_status=Status.objects.get(slug='open'),
        )
        foreign_parent_key = 'P4B-4196'
        Item.objects.filter(pk=self.item_b.pk).update(item_key=foreign_parent_key)
        proposal = TaskDraftProposal.objects.create(
            org_id=self.project_a.org_id,
            requester_id=self.user_a.id,
            requester_email=self.user_a.email,
            raw_prompt='PLAT4196 foreign parent attempt',
            proposed_project_id=self.project_a.id,
            proposed_workflow_slug=workflow.slug,
            proposed_title='PLAT4196 child must not exist',
            proposed_parent_item_id=foreign_parent_key,
            validation_status='valid',
        )
        self._as_a()
        response = self.client.post(
            f'/api/ai-work-intake/drafts/{proposal.id}/confirm/',
            {},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Item.objects.filter(
                project=self.project_a,
                title='PLAT4196 child must not exist',
            ).exists()
        )

    def test_draft_ids_and_org_hints_cannot_cross_principal_scope(self):
        draft_b = TaskDraftProposal.objects.create(
            org_id=self.project_b.org_id,
            requester_id=self.user_b.id,
            requester_email=self.user_b.email,
            raw_prompt='PLAT4196-B-DRAFT-SECRET',
            proposed_project_id=self.project_b.id,
            proposed_workflow_slug='default',
            proposed_title='PLAT4196-B-DRAFT-SECRET',
            validation_status='valid',
        )
        self._as_a()
        headers = self._org_b_hints()
        self.assertEqual(
            self.client.get(f'/api/ai-work-intake/drafts/{draft_b.id}/?org_id=4102', **headers).status_code,
            404,
        )
        listed = self.client.get('/api/ai-work-intake/drafts/?org_id=4102', **headers)
        self.assertEqual(listed.status_code, 200)
        self.assertNotContains(listed, 'PLAT4196-B-DRAFT-SECRET')
        self.assertEqual(
            self.client.post(
                f'/api/ai-work-intake/drafts/{draft_b.id}/confirm/', {},
                format='json', **headers,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f'/api/ai-work-intake/drafts/{draft_b.id}/reject/', {'reason': 'ATTACKED'},
                format='json', **headers,
            ).status_code,
            400,
        )
        draft_b.refresh_from_db()
        self.assertIsNone(draft_b.confirmed_at)
        self.assertIsNone(draft_b.rejected_at)

    def test_project_workflows_list_returns_200_and_scopes_by_accessible_projects(self):
        """PLS-604: GET /api/project-workflows/ must return 200 (not 500).

        The pre-PLS-604 queryset did ``prefetch_related('transitions')`` — a
        relation ProjectWorkflow does not have — which only blows up at queryset
        EVALUATION (the list handler), so a construction-only test would miss it.
        This test iterates the list path for both the bare and ?project= forms.
        """
        from tasks.models import ProjectWorkflow
        # A default ProjectWorkflow is auto-created when the Project is created.
        ProjectWorkflow.objects.get_or_create(
            project=self.project_a,
            defaults={
                'statuses': [{'name': 'todo', 'label': 'To Do', 'category': 'todo', 'color': '#808080'}],
                'initial_status': 'todo',
            },
        )
        self._as_a()
        bare = self.client.get('/api/project-workflows/')
        self.assertEqual(bare.status_code, 200)
        filtered = self.client.get(f'/api/project-workflows/?project={self.project_a.id}')
        self.assertEqual(filtered.status_code, 200)
        # Cross-tenant project must not appear in the scoped list.
        self.assertNotContains(bare, 'PLAT4196 B')
