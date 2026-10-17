import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, Project, ProjectMembership, TeamRoster
from tasks.services.workflow_service import WorkflowService



class _NoopTimerManager:
    def schedule_item(self, *args, **kwargs):
        return None

    def cancel_recurring(self, *args, **kwargs):
        return None

    def cancel_item(self, *args, **kwargs):
        return None

    def cancel_all(self, *args, **kwargs):
        return None

class _FakeUserService:
    def __init__(self, users):
        self.users = {email.lower(): {'id': uid, 'email': canonical, 'username': email.split('@')[0],
                                      'full_name': email.split('@')[0].title(), 'is_agent': True}
                      for email, (uid, canonical) in users.items()}
        self.by_id = {info['id']: info for info in self.users.values()}

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())

    def get_user_by_id(self, user_id):
        try:
            return self.by_id.get(int(user_id))
        except Exception:
            return None

    def resolve_user_info(self, user_id=None, email=None):
        if user_id:
            info = self.get_user_by_id(user_id)
            if info:
                return info
        if email:
            return self.get_user_by_email(email) or {
                'id': user_id,
                'email': email,
                'username': email.split('@')[0],
                'full_name': email.split('@')[0].title(),
                'is_agent': email.endswith('@agents.shizuha.io'),
            }
        return None


@override_settings(ROOT_URLCONF='taskmanager.urls')
class PLAT1089AssignmentGroupResolverTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.actor = User.objects.create_user(
            username='ni', email='ni@shizuha.com', password='x', is_staff=True,
        )
        self.client.force_authenticate(user=self.actor)
        self.patchers = [
            patch('tasks.scheduler.get_timer_manager', return_value=_NoopTimerManager()),
            patch('tasks.tasks.send_item_notification.delay', return_value=None),
            patch('tasks.tasks.notify_comment_mentions.delay', return_value=None),
            patch('tasks.services.notifications.notification_service.notify_assignment_changed', return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.project = Project.objects.create(name='Platform', key='P1089', org_id=1, access_level='open')
        self.users = _FakeUserService({
            'banto@shizuha.com': (501, 'banto@agents.shizuha.io'),
            'hana@shizuha.com': (502, 'hana@shizuha.com'),
            'nao@shizuha.com': (503, 'nao@shizuha.com'),
        })
        ProjectMembership.objects.create(
            project=self.project,
            user_id=503,
            user_email='nao@shizuha.com',
            role='member',
        )

    def tearDown(self):
        for patcher in reversed(getattr(self, 'patchers', [])):
            patcher.stop()
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _item(self, **kwargs):
        data = dict(
            mode='task',
            title='route me',
            status='in_progress',
            priority='high',
            project=self.project,
            assignee_id=503,
            assignee_email='nao@shizuha.com',
            created_by_email='reporter@shizuha.com',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    def test_team_roster_save_invalidates_process_local_resolver_caches(self):
        WorkflowService._roster_cache = (time.monotonic(), {'analytics': [['nao@shizuha.com']]}, ('old', 1))
        # PLS-1283: the junior map is keyed by (organization_slug, canonical_team).
        WorkflowService._junior_cache = (time.monotonic(), {('shizuha', 'review'): ['junior@shizuha.com']}, ('old', 1))

        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='research-analytics',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['banto@shizuha.com']],
            },
        )

        self.assertFalse(hasattr(WorkflowService, '_roster_cache'))
        self.assertFalse(hasattr(WorkflowService, '_junior_cache'))

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True)
    @patch('tasks.services.admin_service.admin_service.get_org_members', return_value=[])
    def test_assignment_group_route_uses_current_roster_not_stale_cache_and_reports_assignee(
        self, _members, _is_member, _avail,
    ):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='research-analytics',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['banto@shizuha.com'], ['hana@shizuha.com']],
            },
        )
        stale_version = ('stale', 0)
        # Roster lookups key on the CANONICAL team name ('analytics' aliases to
        # 'research-analytics' since the single-team invariant sweep) — the
        # stale cache must be primed under that key or this test would pass
        # vacuously (lookup would miss the stale entry instead of beating it).
        WorkflowService._roster_cache = (time.monotonic(), {'research-analytics': [['nao@shizuha.com']]}, stale_version)
        WorkflowService._junior_cache = (time.monotonic(), {}, stale_version)
        item = self._item()

        with patch('tasks.user_service.user_service', self.users), \
             patch('tasks.views.user_service', self.users):
            resp = self.client.post(
                f'/api/items/{item.id}/assign/',
                {'assignment_group': 'analytics'},
                format='json',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 501)
        # The identity service canonicalized banto to the internal agent alias; the
        # roster contains banto@shizuha.com. PLAT-1089 must treat those as one agent.
        self.assertEqual(item.assignee_email, 'banto@agents.shizuha.io')
        self.assertEqual(resp.data['item_key'], item.item_key)
        self.assertEqual(resp.data['title'], item.title)
        self.assertEqual(resp.data['assignee_email'], 'banto@agents.shizuha.io')
        # The alias 'analytics' canonicalizes to today's roster name — the API
        # reports the canonical group so consumers see one truth.
        self.assertEqual(resp.data['assignment_group'], 'research-analytics')
        self.assertFalse(resp.data['queued_at_capacity'])

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True)
    @patch('tasks.services.admin_service.admin_service.get_org_members', return_value=[])
    def test_team_assignment_ignores_client_sent_lead_email_without_direct_reason(
        self, _members, _is_member, _avail,
    ):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='research-analytics',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['banto@shizuha.com'], ['hana@shizuha.com']],
            },
        )
        item = self._item()

        with patch('tasks.user_service.user_service', self.users), \
             patch('tasks.views.user_service', self.users):
            resp = self.client.post(
                f'/api/items/{item.id}/assign/',
                {
                    'assignment_group': 'analytics',
                    'assignee_id': None,
                    'assignee_email': 'nao@shizuha.com',
                },
                format='json',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        item.refresh_from_db()
        # Stored group is the canonical roster name (alias sweep, 2026-07-11).
        self.assertEqual(item.assignment_group, 'research-analytics')
        self.assertEqual(item.assignee_email, 'banto@agents.shizuha.io')
        self.assertNotEqual(item.assignee_email, 'nao@shizuha.com')

    @patch('tasks.views._scrape_agent_health', return_value={
        'shizuha': {'status': 'active', 'last_activity_s': 17, 'auth_errors': 0},
    })
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=False)
    def test_assignee_health_uses_assignment_availability_over_stale_active_scrape(
        self, _avail, _scrape,
    ):
        from tasks.serializers import _agent_health_for_email

        health = _agent_health_for_email('shizuha@shizuha.com')

        self.assertEqual(health['status'], 'unavailable')
        self.assertFalse(health['is_online'])

    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.models.Item.assign', return_value=False)
    def test_assignment_group_route_fails_closed_when_persist_is_noop(self, _assign, _avail):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='research-analytics',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['banto@shizuha.com']],
            },
        )
        item = self._item()

        with patch('tasks.user_service.user_service', self.users), \
             patch('tasks.views.user_service', self.users), \
             patch.object(
                 WorkflowService, '_team_roster_entry_for_item',
                 return_value=([['banto@shizuha.com']], 'shizuha'),
             ):
            resp = self.client.post(
                f'/api/items/{item.id}/assign/',
                {'assignment_group': 'analytics'},
                format='json',
            )

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.data)
        self.assertIn('could not be persisted', str(resp.data['error']))
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, 'nao@shizuha.com')

    @patch.dict('os.environ', {'PULSE_WIP_CAP_TEAMS': 'engineering', 'PULSE_HEALTH_EXPORTER_URL': 'disabled'})
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_capacity_pull_rechecks_current_active_roster_and_accepts_agent_alias(self, _avail):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['banto@shizuha.com']],
            },
        )
        queued = Item.objects.create(
            mode='task', title='queued engineering work', status='open', priority='high',
            project=self.project, assignment_group='engineering', assignee_id=None, assignee_email='',
        )
        WorkflowService._roster_cache = (time.monotonic(), {'engineering': [['nao@shizuha.com']]}, ('stale', 0))

        with patch('tasks.user_service.user_service', self.users):
            pulled = WorkflowService._pull_next_queued_for(501, 'banto@agents.shizuha.io')

        self.assertIsNotNone(pulled)
        queued.refresh_from_db()
        self.assertEqual(queued.assignee_id, 501)
        self.assertEqual(queued.assignee_email, 'banto@agents.shizuha.io')
