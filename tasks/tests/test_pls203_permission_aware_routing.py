from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import (
    Comment,
    Item,
    Project,
    ProjectMembership,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.views import ItemViewSet


class _FakeUserService:
    def __init__(self):
        self.users = {
            'kei@shizuha.com': {'id': 101, 'email': 'kei@shizuha.com', 'username': 'kei'},
            'akira@shizuha.com': {'id': 102, 'email': 'akira@shizuha.com', 'username': 'akira'},
            'ren@shizuha.com': {'id': 103, 'email': 'ren@shizuha.com', 'username': 'ren'},
        }

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())

    def get_user_by_id(self, user_id):
        try:
            uid = int(user_id)
        except Exception:
            return None
        for info in self.users.values():
            if info['id'] == uid:
                return info
        return None


class _NoopTimerManager:
    def schedule_item(self, *args, **kwargs):
        return None

    def cancel_recurring(self, *args, **kwargs):
        return None

    def cancel_item(self, *args, **kwargs):
        return None

    def cancel_all(self, *args, **kwargs):
        return None


@override_settings(PULSE_HEALTH_EXPORTER_URL='disabled', PULSE_API_HEALTH_URL='disabled')
class PLS203PermissionAwareRoutingTests(TestCase):
    def setUp(self):
        self.fake_users = _FakeUserService()
        self.patchers = [
            patch.dict('os.environ', {
                'PULSE_HEALTH_EXPORTER_URL': 'disabled',
                'PULSE_API_HEALTH_URL': 'disabled',
            }),
            patch('tasks.user_service.user_service', self.fake_users),
            patch('tasks.scheduler.get_timer_manager', return_value=_NoopTimerManager()),
            patch('tasks.tasks.send_item_notification.delay', return_value=None),
            patch('tasks.tasks.notify_comment_mentions.delay', return_value=None),
            patch('tasks.services.notifications.notification_service.notify_assignment_changed', return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.open_status, _ = Status.objects.get_or_create(
            slug='pls203_open',
            defaults={'name': 'PLS203 Open', 'category': 'todo', 'color': '#ccc'},
        )
        self.doing_status, _ = Status.objects.get_or_create(
            slug='pls203_in_progress',
            defaults={'name': 'PLS203 In Progress', 'category': 'in_progress', 'color': '#3b82f6'},
        )
        self.rejected_status, _ = Status.objects.get_or_create(
            slug='pls203_rejected',
            defaults={'name': 'PLS203 Rejected', 'category': 'done', 'color': '#999'},
        )
        self.workflow = Workflow.objects.create(
            name='PLS203 Security Finding',
            slug='pls203-security-finding',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.doing_status, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.rejected_status, position=2)
        self.accept = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Accept',
            from_status='pls203_open',
            to_status='pls203_in_progress',
            allowed_roles=['security'],
            position=1,
        )
        WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Reject',
            from_status='pls203_open',
            to_status='pls203_rejected',
            allowed_roles=['security'],
            position=2,
        )
        self.project = Project.objects.create(name='Pulse', key='PLS203', org_id=1, access_level='open')
        TeamRoster.objects.update_or_create(
            team_name='security',
            defaults={'tiers': [['kei@shizuha.com', 'akira@shizuha.com', 'ren@shizuha.com']]},
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=102,
            user_email='akira@shizuha.com',
            role='admin',
            is_active=True,
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        for patcher in reversed(getattr(self, 'patchers', [])):
            patcher.stop()
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='security finding',
            mode='task',
            item_type='task.bug',
            workflow_name='pls203-security-finding',
            status='pls203_open',
            workflow_status='pls203_open',
            assignment_group='security',
            priority='urgent',
            created_by_id=999,
            created_by_email='reporter@shizuha.com',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_resolver_seats_an_available_member_without_transition_permission(self, _avail):
        item = self._item()

        uid, email = WorkflowService._resolve_team_assignee('security', item=item)

        self.assertEqual(uid, 101)
        self.assertEqual(email, 'kei@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_resolver_seats_when_no_member_can_execute_the_status(self, _avail):
        ProjectMembership.objects.all().delete()
        item = self._item()

        uid, email = WorkflowService._resolve_team_assignee('security', item=item)

        self.assertEqual(uid, 101)
        self.assertEqual(email, 'kei@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_create_time_assignment_group_routing_is_permission_aware(self, _avail):
        request = APIRequestFactory().post('/api/items/', {
            'project': self.project.id,
            'title': 'new security finding',
            'mode': 'task',
            'item_type': 'task.bug',
            'workflow_name': 'pls203-security-finding',
            'assignment_group': 'security',
            'priority': 'urgent',
        }, format='json')
        actor = SimpleNamespace(
            id=999, email='reporter@shizuha.com', is_staff=True,
            is_superuser=False, is_authenticated=True, organization_memberships={},
        )
        force_authenticate(request, user=actor)

        response = ItemViewSet.as_view({'post': 'create'})(request)

        self.assertEqual(response.status_code, 201)
        item = Item.objects.get(id=response.data['id'])
        self.assertEqual(item.assignee_id, 101)
        self.assertEqual(item.assignee_email, 'kei@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_assignee_only_transition_is_checked_against_candidate(self, _avail):
        self.accept.assignee_only = True
        self.accept.save(update_fields=['assignee_only'])
        item = self._item(assignee_id=None, assignee_email='')

        uid, email = WorkflowService._resolve_team_assignee('security', item=item)

        self.assertEqual(uid, 101)
        self.assertEqual(email, 'kei@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_capacity_sweep_seats_an_available_team_member(self, _avail):
        from tasks.tasks import _sweep_rebalance_pods_impl

        item = self._item(assignee_id=None, assignee_email='')

        result = _sweep_rebalance_pods_impl(max_skew=1)

        item.refresh_from_db()
        self.assertGreaterEqual(result['moves'], 1)
        self.assertEqual(item.assignee_id, 102)
        self.assertEqual(item.assignee_email, 'akira@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_unassigned_scheduled_item_is_not_a_ghost_runtime_identity(self, _avail):
        from tasks.tasks import _sweep_rebalance_pods_impl

        item = self._item(assignee_id=None, assignee_email='')
        Item.objects.filter(pk=item.pk).update(
            status='scheduled',
            workflow_status='scheduled',
        )

        with patch.object(
            self.fake_users,
            'get_user_by_email',
            wraps=self.fake_users.get_user_by_email,
        ) as lookup:
            _sweep_rebalance_pods_impl(max_skew=1)

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, '')
        self.assertIsNone(item.assignee_id)
        self.assertNotIn(
            '',
            [call.args[0] for call in lookup.call_args_list if call.args],
            'the empty queue-owner sentinel must not be resolved as a user',
        )

    def test_failover_does_not_move_role_gated_item_to_permission_blocked_live_peer(self):
        from tasks.tasks import _sweep_rebalance_pods_impl

        item = self._item(assignee_id=101, assignee_email='kei@shizuha.com')

        def available(email):
            return (email or '').strip().lower() != 'kei@shizuha.com'

        with patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', side_effect=available):
            result = _sweep_rebalance_pods_impl(max_skew=1)

        item.refresh_from_db()
        self.assertGreaterEqual(result['moves'], 1)
        self.assertEqual(item.assignee_id, 102)
        self.assertEqual(item.assignee_email, 'akira@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_permission_denied_current_assignee_is_rerouted_to_permission_holder(self, _avail):
        # PLAT-8894 (goro decision comment 772253) supersedes part of this
        # test's original fixture: kei — a security-roster member with NO
        # project row — is now AUTHORIZED for 'security'-declared transitions
        # (pinned by
        # test_plat8894_roster_owned_role_authorizes_roster_member_execution
        # below). The backstop reroute contract is still proven here with an
        # UNMAPPED declared role: 'owner' is not keyed in
        # DECLARED_ROLE_TEAM_PROJECTION, so the projection never fires — kei
        # (on the security roster, satisfying the assignee∈group invariant)
        # is denied, and the backstop reroutes to the permission holder
        # (akira, whose project 'admin' row matches via the owner→admin
        # compatibility mapping).
        owner_accept = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Owner Accept',
            from_status='pls203_open',
            to_status='pls203_in_progress',
            allowed_roles=['owner'],
            position=3,
        )
        item = self._item(assignee_id=101, assignee_email='kei@shizuha.com')
        kei_user = SimpleNamespace(id=101, email='kei@shizuha.com', is_staff=False, is_superuser=False,
                                   organization_memberships={})

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'pls203_in_progress',
                user=kei_user,
                transition_id=str(owner_accept.id),
            )

        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 102)
        self.assertEqual(item.assignee_email, 'akira@shizuha.com')
        self.assertTrue(Comment.objects.filter(item=item, content__icontains='PLS-203 backstop').exists())

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_plat8894_roster_owned_role_authorizes_roster_member_execution(self, _avail):
        """PLAT-8894 (goro decision comment 772253): the original fixture of
        the backstop test above — kei, security-roster member, NO project row,
        current assignee — now executes the declared-role transition instead
        of being denied and rerouted. Execution-side authorization only; the
        resolver candidate filter still skips kei (see the resolver tests)."""
        item = self._item(assignee_id=101, assignee_email='kei@shizuha.com')
        kei_user = SimpleNamespace(id=101, email='kei@shizuha.com', is_staff=False, is_superuser=False,
                                   organization_memberships={})

        WorkflowService.execute_transition(
            item,
            'pls203_in_progress',
            user=kei_user,
            transition_id=str(self.accept.id),
        )

        item.refresh_from_db()
        self.assertEqual(item.status, 'pls203_in_progress')
        self.assertEqual(item.assignee_id, 101, 'execution must not reroute an authorized assignee')
