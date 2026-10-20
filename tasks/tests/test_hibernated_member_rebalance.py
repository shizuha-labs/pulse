"""Hibernated members must not keep ready work while a live teammate is idle.

Live Admin Ops 2026-08-25: Shion was Hive-hibernated (no model lease) with four
urgent ``todo`` items and a blank ``assignment_group``; Goro held the team's
only lease and sat idle. Availability already marked Shion down (exporter
enabled=0), but the failover drain skipped NULL-group rows per PLAT-916, so the
work stayed parked on the hibernated member.

These tests pin:
  1. Hive ``status=hibernated`` is a hard-unavailable routing signal (heartbeat
     does not protect already-owned ready work).
  2. Unique-team blank-group ``open``/``todo`` drains to the live teammate and
     stamps ``assignment_group``.
  3. Blocked/scheduled blanks stay; multi-team blanks stay (PLAT-916).
"""
import json
import os
import uuid
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIRequestFactory

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _SOFT_UNAVAILABILITY_REASONS,
    _hive_hibernated_agents,
    _soft_unavailable_owner_protected,
    _unique_org_roster_team,
    sweep_agent_availability,
    sweep_rebalance_pods,
)
from tasks.views import InternalAgentLifecycleView


_ENV = {
    'PULSE_API_HEALTH_URL': 'disabled',
    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    'PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED': '0',
}


def _urlopen_json(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode('utf-8')
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _requests_get_metrics(text):
    resp = MagicMock()
    resp.text = text
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return MagicMock(return_value=resp)


def _admin_ops_fixture(org_seed=810000):
    tag = uuid.uuid4().hex[:10]
    hibernated = f'shion{tag}@shizuha.com'
    idle = f'goro{tag}@shizuha.com'
    base = 810000 + (int(tag, 16) % 100000)
    org_id = org_seed + (int(tag, 16) % 30000)
    users = {hibernated: (base, hibernated), idle: (base + 1, idle)}
    TeamRoster.objects.create(
        team_name='admin-ops',
        organization_slug=f'torg{org_id}',
        organization_id=org_id,
        tiers=[[hibernated, idle]],
        is_active=True,
    )
    project = Project.objects.create(
        org_id=org_id, name=f'Admin Ops {org_id}', key=f'AO{org_id % 1000:03d}',
    )
    return hibernated, idle, users, project, org_id, base


def _fake_user_service(users):
    class FakeUS:
        def get_user_by_email(self, email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def get_user_by_id(self, user_id):
            for uid, email in users.values():
                if int(user_id) == uid:
                    return {'id': uid, 'email': email}
            return None
    return FakeUS()


class UniqueOrgRosterTeamTests(TestCase):
    def test_unique_team_is_returned(self):
        hibernated, idle, users, project, org_id, _ = _admin_ops_fixture()
        self.assertEqual(
            _unique_org_roster_team(hibernated, org_id), 'admin-ops',
        )
        self.assertEqual(_unique_org_roster_team(idle, org_id), 'admin-ops')

    def test_other_org_membership_does_not_break_uniqueness(self):
        hibernated, idle, users, project, org_id, _ = _admin_ops_fixture()
        other_org = org_id + 1
        TeamRoster.objects.create(
            team_name='devops',
            organization_slug=f'torg{other_org}',
            organization_id=other_org,
            tiers=[[hibernated]],
            is_active=True,
        )
        self.assertEqual(_unique_org_roster_team(hibernated, org_id), 'admin-ops')
        self.assertEqual(_unique_org_roster_team(hibernated, other_org), 'devops')


class HibernatedHardUnavailableTests(TestCase):
    def test_hibernated_reason_is_not_soft(self):
        self.assertNotIn('hibernated', _SOFT_UNAVAILABILITY_REASONS)

    def test_hibernated_owner_drains_even_with_heartbeat(self):
        with patch('tasks.tasks._agent_unavailability_reason', return_value='hibernated'), \
             patch('tasks.tasks._agent_has_recent_pulse_heartbeat', return_value=True):
            self.assertFalse(
                _soft_unavailable_owner_protected('shion@shizuha.com'),
            )

    def test_hibernated_kill_switch_beats_recent_productivity(self):
        WorkflowService._hibernated_emails_cache = None
        WorkflowService._disabled_emails_cache = None
        with patch.object(
            WorkflowService, '_hibernated_agent_emails',
            return_value={'shion@shizuha.com'},
        ), patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), patch.object(
            WorkflowService, '_exporter_down_but_recently_productive',
            return_value=True,
        ), patch.object(
            WorkflowService, '_health_availability_map',
            return_value={'shion@shizuha.com': False},
        ):
            self.assertFalse(
                WorkflowService._is_agent_available_for_assignment(
                    'shion@shizuha.com',
                ),
            )


class HibernatedBlankGroupDrainTests(TestCase):
    def _run(self, *, hibernated, idle, users, available):
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', _fake_user_service(users)), \
             patch.object(
                 WorkflowService, '_is_agent_available_for_assignment',
                 side_effect=lambda e: e.lower() in available,
             ), \
             patch.object(
                 WorkflowService, '_hibernated_agent_emails',
                 return_value={hibernated},
             ), \
             patch('tasks.tasks._agent_unavailability_reason', return_value='hibernated'), \
             patch('tasks.tasks._agent_has_recent_pulse_heartbeat', return_value=True):
            return sweep_rebalance_pods.apply(args=[], kwargs={}).get()

    def test_blank_group_todo_drains_to_idle_teammate_and_stamps_group(self):
        hibernated, idle, users, project, org_id, base = _admin_ops_fixture()
        task = Item.objects.create(
            title='Admin Ops escalation parked on hibernated member',
            mode='task', status='todo', priority='urgent',
            project=project,
            assignment_group='',
            assignee_id=base, assignee_email=hibernated,
        )
        self._run(
            hibernated=hibernated, idle=idle, users=users, available={idle},
        )
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, idle)
        self.assertEqual(task.assignment_group, 'admin-ops')

    def test_grouped_todo_still_drains(self):
        hibernated, idle, users, project, org_id, base = _admin_ops_fixture()
        task = Item.objects.create(
            title='Already team-owned ready work',
            mode='task', status='open', priority='urgent',
            project=project,
            assignment_group='admin-ops',
            assignee_id=base, assignee_email=hibernated,
        )
        self._run(
            hibernated=hibernated, idle=idle, users=users, available={idle},
        )
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, idle)

    def test_scheduled_and_in_progress_blanks_stay(self):
        hibernated, idle, users, project, org_id, base = _admin_ops_fixture()
        scheduled = Item.objects.create(
            title='Future-due, not ready',
            mode='task', status='scheduled', priority='high',
            project=project, assignment_group='',
            assignee_id=base, assignee_email=hibernated,
        )
        started = Item.objects.create(
            title='Already started, not unstarted-ready',
            mode='task', status='in_progress', priority='normal',
            project=project, assignment_group='',
            assignee_id=base, assignee_email=hibernated,
        )
        self._run(
            hibernated=hibernated, idle=idle, users=users, available={idle},
        )
        scheduled.refresh_from_db()
        started.refresh_from_db()
        self.assertEqual(scheduled.assignee_email, hibernated)
        self.assertEqual(started.assignee_email, hibernated)

    def test_other_org_membership_does_not_block_this_org_drain(self):
        hibernated, idle, users, project, org_id, base = _admin_ops_fixture()
        other_org = org_id + 1
        TeamRoster.objects.create(
            team_name='devops',
            organization_slug=f'torg{other_org}',
            organization_id=other_org,
            tiers=[[hibernated]],
            is_active=True,
        )
        task = Item.objects.create(
            title='Blank-group work still unique in this org',
            mode='task', status='todo', priority='urgent',
            project=project, assignment_group='',
            assignee_id=base, assignee_email=hibernated,
        )
        self._run(
            hibernated=hibernated, idle=idle, users=users, available={idle},
        )
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, idle)
        self.assertEqual(task.assignment_group, 'admin-ops')


class HiveHibernatedAvailabilitySweepTests(TestCase):
    def setUp(self):
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._hibernated_emails_cache = None

    def test_hive_hibernated_fold_marks_agent_down_even_with_process_up(self):
        metrics = '\n'.join([
            'shizuha_agent_process_up{agent="shion",backend="scli"} 1',
            'shizuha_agent_process_up{agent="goro",backend="scli"} 1',
            'shizuha_agent_enabled{agent="shion",backend="scli"} 1',
            'shizuha_agent_enabled{agent="goro",backend="scli"} 1',
        ]) + '\n'
        hive_payload = {
            'agents': [
                {
                    'username': 'shion',
                    'email': 'shion@shizuha.com',
                    'lifecycle_status': 'hibernated',
                    'enabled': False,
                },
                {
                    'username': 'goro',
                    'email': 'goro@shizuha.com',
                    'lifecycle_status': 'running',
                    'enabled': True,
                },
            ],
        }
        captured = {}
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        redis_mock.scan_iter.return_value = [
            b'pulse:agent_hb:shion@shizuha.com',
        ]

        def _setex(key, ttl, val):
            captured[key] = json.loads(val)

        redis_mock.setex.side_effect = _setex
        apply_async = MagicMock()

        def _urlopen(url, timeout=10, **kw):
            target = getattr(url, 'full_url', url)
            if 'lifecycle-availability' in str(target):
                resp = MagicMock()
                resp.read.return_value = json.dumps(hive_payload).encode('utf-8')
                cm = MagicMock()
                cm.__enter__.return_value = resp
                cm.__exit__.return_value = False
                return cm
            resp = MagicMock()
            resp.read.return_value = metrics.encode('utf-8')
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        with patch('urllib.request.urlopen', side_effect=_urlopen), \
             patch('requests.get', _requests_get_metrics(metrics)), \
             patch(
                 'tasks.services.workflow_service.WorkflowService._redis_client',
                 return_value=redis_mock,
             ), \
             patch('tasks.tasks.sweep_rebalance_pods.apply_async', apply_async), \
             patch.dict(
                 os.environ,
                 {
                     'PULSE_HEALTH_EXPORTER_URL': 'http://exporter.test/metrics',
                     'PULSE_HIVE_LIFECYCLE_URL': 'http://hive.test/internal/v1/fleet/lifecycle-availability',
                     'PULSE_HIVE_LIFECYCLE_TOKEN': 'secret',
                     'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
                     'PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED': '1',
                 },
             ):
            result = sweep_agent_availability.run()

        self.assertTrue(result['ok'])
        avail = captured['pulse:agent_availability']
        self.assertFalse(avail['shion@shizuha.com'])
        self.assertTrue(avail['goro@shizuha.com'])
        self.assertIn('shion@shizuha.com', captured['pulse:agent_hibernated'])
        self.assertEqual(
            captured['pulse:agent_unavailability_reason']['shion@shizuha.com'],
            'hibernated',
        )
        apply_async.assert_called()
        self.assertIn('shion@shizuha.com', result['hibernated'])
        self.assertIn('shion@shizuha.com', result['down'])

    def test_missing_hive_source_is_noop(self):
        with patch.dict(os.environ, {
            'PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED': '0',
        }, clear=False):
            emails, ok = _hive_hibernated_agents()
        self.assertEqual(emails, [])
        self.assertTrue(ok)


class InternalAgentLifecycleViewTests(TestCase):
    def test_hibernate_event_marks_unavailable_and_dispatches_drain(self):
        WorkflowService._hibernated_emails_cache = None
        redis_mock = MagicMock()
        redis_mock.get.return_value = None
        captured = {}

        def _setex(key, ttl, val):
            captured[key] = json.loads(val)

        redis_mock.setex.side_effect = _setex
        factory = APIRequestFactory()
        request = factory.post(
            '/api/internal/agent-lifecycle/',
            {'username': 'shion', 'status': 'hibernated', 'reason': 'no lease'},
            format='json',
        )
        with patch.object(WorkflowService, '_redis_client', return_value=redis_mock), \
             patch('tasks.tasks.sweep_rebalance_pods.apply_async') as apply_async:
            response = InternalAgentLifecycleView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['draining'])
        self.assertFalse(captured['pulse:agent_availability']['shion@shizuha.com'])
        self.assertIn('shion@shizuha.com', captured['pulse:agent_hibernated'])
        apply_async.assert_called_once()
