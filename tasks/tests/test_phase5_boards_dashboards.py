"""
Exhaustive E2E tests for Phase 5: Boards (enhanced), Dashboards, Gadgets.
"""
from io import StringIO

from django.core.management import call_command
from rest_framework import status as http_status
from tasks.default_boards import ensure_default_board_for_project
from tasks.models import (
    Board, BoardQuickFilter,
    Dashboard, DashboardGadget,
    Project,
)
from .base import AuthenticatedAPITestCase


class BoardAPITests(AuthenticatedAPITestCase):
    """CRUD + swimlane + quick filter tests for Board API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Board Project', key='BRD', org_id=1
        )

    def test_create_kanban_board(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Main Kanban',
            'board_type': 'kanban',
            'swimlane_by': 'none',
            'is_default': True,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['board_type'], 'kanban')
        self.assertTrue(resp.json()['is_default'])

    def test_create_scrum_board(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Sprint Board',
            'board_type': 'scrum',
            'estimation_field': 'story_points',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['board_type'], 'scrum')

    def test_multiple_boards_per_project(self):
        """Multiple boards per project."""
        Board.objects.create(
            project=self.project, name='Board 1',
            board_type='kanban', position=0
        )
        Board.objects.create(
            project=self.project, name='Board 2',
            board_type='scrum', position=1
        )
        Board.objects.create(
            project=self.project, name='Board 3',
            board_type='kanban', position=2
        )
        resp = self.client.get(f'/shizuha-pulse/api/boards/?project={self.project.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertEqual(len(results), 4)
        self.assertTrue(Board.objects.filter(
            project=self.project,
            name='Board Project — Kanban',
            is_default=True,
        ).exists())

    def test_swimlane_by_assignee(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Assignee Lanes',
            'board_type': 'kanban',
            'swimlane_by': 'assignee',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['swimlane_by'], 'assignee')

    def test_swimlane_by_priority(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Priority Lanes',
            'board_type': 'kanban',
            'swimlane_by': 'priority',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['swimlane_by'], 'priority')

    def test_swimlane_by_epic(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Epic Lanes',
            'board_type': 'kanban',
            'swimlane_by': 'epic',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_swimlane_by_issue_type(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Type Lanes',
            'board_type': 'kanban',
            'swimlane_by': 'issue_type',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_swimlane_by_jql(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'JQL Lanes',
            'board_type': 'kanban',
            'swimlane_by': 'jql',
            'swimlane_jql': 'priority = high',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['swimlane_by'], 'jql')

    def test_board_with_filter_jql(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Filtered Board',
            'board_type': 'kanban',
            'filter_jql': 'status != Done AND assignee is not EMPTY',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertIn('assignee', resp.json()['filter_jql'])

    def test_board_card_layout(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Custom Cards',
            'board_type': 'kanban',
            'card_layout': {
                'fields': ['priority', 'assignee', 'labels', 'due_date'],
                'show_days_in_column': True,
            },
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(len(resp.json()['card_layout']['fields']), 4)

    def test_board_card_colors(self):
        resp = self.client.post('/shizuha-pulse/api/boards/', {
            'project': self.project.id,
            'name': 'Colored Cards',
            'board_type': 'kanban',
            'card_colors': [
                {'field': 'priority', 'value': 'urgent', 'color': '#ef4444'},
                {'field': 'priority', 'value': 'high', 'color': '#f59e0b'},
                {'jql': 'labels = blocked', 'color': '#dc2626'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(len(resp.json()['card_colors']), 3)

    def test_add_quick_filter(self):
        board = Board.objects.create(
            project=self.project, name='QF Board', board_type='kanban'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/boards/{board.id}/add_quick_filter/',
            {
                'name': 'Only My Issues',
                'jql': 'assignee = currentUser()',
                'position': 0,
            }
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(board.quick_filters.count(), 1)

    def test_multiple_quick_filters(self):
        board = Board.objects.create(
            project=self.project, name='Multi QF', board_type='kanban'
        )
        filters_data = [
            {'name': 'My Issues', 'jql': 'assignee = currentUser()', 'position': 0},
            {'name': 'Blockers', 'jql': 'priority = urgent', 'position': 1},
            {'name': 'Recently Updated', 'jql': 'updated >= -1d', 'position': 2},
        ]
        for fd in filters_data:
            self.client.post(
                f'/shizuha-pulse/api/boards/{board.id}/add_quick_filter/',
                fd
            )
        self.assertEqual(board.quick_filters.count(), 3)

    def test_remove_quick_filter(self):
        board = Board.objects.create(
            project=self.project, name='Rm QF', board_type='kanban'
        )
        qf = BoardQuickFilter.objects.create(
            board=board, name='Test', jql='status = open', position=0
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/boards/{board.id}/remove_quick_filter/',
            {'filter_id': str(qf.id)}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(board.quick_filters.count(), 0)

    def test_standalone_quick_filter_crud(self):
        """Test the standalone BoardQuickFilter viewset."""
        board = Board.objects.create(
            project=self.project, name='Standalone QF', board_type='kanban'
        )
        # Create
        resp = self.client.post('/shizuha-pulse/api/board-quick-filters/', {
            'board': str(board.id),
            'name': 'Standalone Filter',
            'jql': 'status = open',
            'position': 0,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        qf_id = resp.json()['id']

        # List with filter
        resp = self.client.get(f'/shizuha-pulse/api/board-quick-filters/?board_id={board.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

        # Delete
        resp = self.client.delete(f'/shizuha-pulse/api/board-quick-filters/{qf_id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)

    def test_update_board(self):
        board = Board.objects.create(
            project=self.project, name='Upd Board', board_type='kanban'
        )
        resp = self.client.patch(f'/shizuha-pulse/api/boards/{board.id}/', {
            'name': 'Updated Board',
            'swimlane_by': 'priority',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        board.refresh_from_db()
        self.assertEqual(board.name, 'Updated Board')
        self.assertEqual(board.swimlane_by, 'priority')

    def test_delete_board(self):
        board = Board.objects.create(
            project=self.project, name='Del Board', board_type='kanban'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/boards/{board.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class DefaultBoardProvisioningTests(AuthenticatedAPITestCase):
    """Project signal + backfill coverage for PLS-58 default boards."""

    def test_project_post_save_creates_default_board_for_software_project(self):
        project = Project.objects.create(
            name='Pulse', key='PLS', org_id=1, project_type='software',
            created_by_id=self.user.id,
        )

        board = Board.objects.get(project=project, is_default=True)
        self.assertEqual(board.name, 'Pulse — Kanban')
        self.assertEqual(board.board_type, 'kanban')
        self.assertEqual(board.created_by_id, self.user.id)

    def test_project_post_save_skips_non_software_project(self):
        project = Project.objects.create(
            name='Ops', key='OPS', org_id=1, project_type='operations',
        )

        self.assertFalse(Board.objects.filter(project=project).exists())

    def test_default_board_helper_is_idempotent(self):
        project = Project.objects.create(name='Connect', key='CON', org_id=1)
        first = Board.objects.get(project=project, is_default=True)

        second, created = ensure_default_board_for_project(project)

        self.assertFalse(created)
        self.assertEqual(second.id, first.id)
        self.assertEqual(Board.objects.filter(project=project, is_default=True).count(), 1)
        self.assertEqual(Board.objects.filter(project=project, name='Connect — Kanban').count(), 1)

    def test_backfill_command_creates_missing_default_boards_only_once(self):
        project = Project.objects.create(name='Cortex', key='CTX', org_id=1)
        Board.objects.filter(project=project).delete()

        out = StringIO()
        call_command('backfill_default_boards', project_key=['CTX'], stdout=out)
        call_command('backfill_default_boards', project_key=['CTX'], stdout=StringIO())

        self.assertIn('Created 1', out.getvalue())
        self.assertEqual(Board.objects.filter(project=project, is_default=True).count(), 1)
        self.assertEqual(Board.objects.get(project=project, is_default=True).name, 'Cortex — Kanban')


class DashboardAPITests(AuthenticatedAPITestCase):
    """CRUD + gadget tests for Dashboard API."""

    def test_create_private_dashboard(self):
        resp = self.client.post('/shizuha-pulse/api/dashboards/', {
            'name': 'My Dashboard',
            'owner_id': self.user.id,
            'owner_email': self.user.email,
            'visibility': 'private',
            'layout': {'columns': 2, 'layout': '2-1'},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['visibility'], 'private')

    def test_create_shared_dashboard(self):
        resp = self.client.post('/shizuha-pulse/api/dashboards/', {
            'name': 'Team Dashboard',
            'owner_id': self.user.id,
            'owner_email': self.user.email,
            'visibility': 'org',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['visibility'], 'org')

    def test_create_wallboard(self):
        resp = self.client.post('/shizuha-pulse/api/dashboards/', {
            'name': 'Wallboard',
            'owner_id': self.user.id,
            'owner_email': self.user.email,
            'visibility': 'public',
            'is_wallboard': True,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertTrue(resp.json()['is_wallboard'])

    def test_filter_by_owner(self):
        Dashboard.objects.create(
            name='Mine', owner_id=self.user.id,
            owner_email=self.user.email
        )
        resp = self.client.get(f'/shizuha-pulse/api/dashboards/?owner_id={self.user.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_update_dashboard(self):
        dash = Dashboard.objects.create(
            name='Upd', owner_id=self.user.id,
            owner_email=self.user.email
        )
        resp = self.client.patch(f'/shizuha-pulse/api/dashboards/{dash.id}/', {
            'name': 'Updated Dashboard',
            'layout': {'columns': 3},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        dash.refresh_from_db()
        self.assertEqual(dash.name, 'Updated Dashboard')

    def test_delete_dashboard(self):
        dash = Dashboard.objects.create(
            name='Del', owner_id=self.user.id,
            owner_email=self.user.email
        )
        resp = self.client.delete(f'/shizuha-pulse/api/dashboards/{dash.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class DashboardGadgetAPITests(AuthenticatedAPITestCase):
    """CRUD tests for all 21 gadget types."""

    def setUp(self):
        super().setUp()
        self.dashboard = Dashboard.objects.create(
            name='Gadget Test', owner_id=self.user.id,
            owner_email=self.user.email
        )

    def _create_gadget(self, gadget_type, config=None, **kwargs):
        data = {
            'dashboard': str(self.dashboard.id),
            'gadget_type': gadget_type,
            'column': kwargs.get('column', 0),
            'position': kwargs.get('position', 0),
            'config': config or {},
        }
        data.update(kwargs)
        return self.client.post('/shizuha-pulse/api/dashboard-gadgets/', data, format='json')

    def test_filter_results_gadget(self):
        resp = self._create_gadget('filter_results', {
            'filter_id': 'my-filter',
            'columns': ['key', 'summary', 'status', 'assignee'],
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_pie_chart_gadget(self):
        resp = self._create_gadget('pie_chart', {
            'stat_type': 'status', 'project': 'TEST',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_two_dimensional_gadget(self):
        resp = self._create_gadget('two_dimensional', {
            'x_axis': 'status', 'y_axis': 'priority',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_created_vs_resolved_gadget(self):
        resp = self._create_gadget('created_vs_resolved', {
            'project': 'TEST', 'period': '30d',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_sprint_burndown_gadget(self):
        resp = self._create_gadget('sprint_burndown', {
            'board_id': 'some-board', 'sprint_id': 'some-sprint',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_sprint_health_gadget(self):
        resp = self._create_gadget('sprint_health', {'board_id': 'b1'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_velocity_chart_gadget(self):
        resp = self._create_gadget('velocity_chart', {
            'board_id': 'b1', 'sprints': 7,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_cumulative_flow_gadget(self):
        resp = self._create_gadget('cumulative_flow', {'board_id': 'b1'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_control_chart_gadget(self):
        resp = self._create_gadget('control_chart', {'board_id': 'b1'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_workload_gadget(self):
        resp = self._create_gadget('workload', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_time_since_gadget(self):
        resp = self._create_gadget('time_since', {
            'field': 'created_at', 'project': 'TEST',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_activity_stream_gadget(self):
        resp = self._create_gadget('activity_stream', {'max_items': 25})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_assigned_to_me_gadget(self):
        resp = self._create_gadget('assigned_to_me', {})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_watched_issues_gadget(self):
        resp = self._create_gadget('watched_issues', {})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_text_gadget(self):
        resp = self._create_gadget('text', {
            'html': '<h1>Welcome</h1><p>Sprint dashboard</p>',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_road_map_gadget(self):
        resp = self._create_gadget('road_map', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_recently_created_gadget(self):
        resp = self._create_gadget('recently_created', {'period': '7d'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_resolution_time_gadget(self):
        resp = self._create_gadget('resolution_time', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_average_age_gadget(self):
        resp = self._create_gadget('average_age', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_time_tracking_gadget(self):
        resp = self._create_gadget('time_tracking', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_sla_met_vs_breached_gadget(self):
        resp = self._create_gadget('sla_met_vs_breached', {'project': 'TEST'})
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_gadget_with_refresh_interval(self):
        resp = self._create_gadget('activity_stream', {'max_items': 10},
                                    refresh_interval=60)
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['refresh_interval'], 60)

    def test_gadget_with_custom_title(self):
        resp = self._create_gadget('pie_chart', {}, title='My Pie Chart')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['title'], 'My Pie Chart')

    def test_gadget_minimized(self):
        resp = self._create_gadget('text', {'html': 'test'}, is_minimized=True)
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertTrue(resp.json()['is_minimized'])

    def test_update_gadget(self):
        gadget = DashboardGadget.objects.create(
            dashboard=self.dashboard, gadget_type='pie_chart',
            column=0, position=0, config={'stat_type': 'status'}
        )
        resp = self.client.patch(f'/shizuha-pulse/api/dashboard-gadgets/{gadget.id}/', {
            'config': {'stat_type': 'priority'},
            'column': 1,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        gadget.refresh_from_db()
        self.assertEqual(gadget.column, 1)

    def test_delete_gadget(self):
        gadget = DashboardGadget.objects.create(
            dashboard=self.dashboard, gadget_type='text',
            column=0, position=0
        )
        resp = self.client.delete(f'/shizuha-pulse/api/dashboard-gadgets/{gadget.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)

    def test_filter_by_dashboard(self):
        DashboardGadget.objects.create(
            dashboard=self.dashboard, gadget_type='pie_chart',
            column=0, position=0
        )
        resp = self.client.get(
            f'/shizuha-pulse/api/dashboard-gadgets/?dashboard={self.dashboard.id}'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
