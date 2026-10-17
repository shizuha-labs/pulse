"""PLS-305: agent coordination console read model (PLS-270 Slice B).

The projection is built ONLY from canonical Pulse state — Item, ItemLink,
metadata.pull_requests, TeamRoster, AgentAvailability — and must surface WIP,
blockers, PR/review state, next transitions, WIP pressure, and stale/offline
agents without becoming a second source of truth.
"""
from datetime import timedelta

from django.utils import timezone

from tasks.models import (
    AgentAvailability, Item, ItemLink, Project, TeamRoster,
)
from tasks.services import agent_activity

from .base import AuthenticatedAPITestCase, create_blocked_item, satisfy_blocked_invariant

ENG = 'pls305-eng'
ALICE = 'alice-pls305@shizuha.com'
BOB = 'bob-pls305@shizuha.com'


class AgentActivityProjectionTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        TeamRoster.objects.create(team_name=ENG, tiers=[[ALICE, BOB]], is_active=True)
        self.project = Project.objects.create(
            name='PLS305 Project', key='P305', org_id=1)

    def _item(self, key, assignee, status='in_progress', **kw):
        assignee_id = kw.pop('assignee_id', 3051 if assignee == ALICE else 3052)
        factory = create_blocked_item if status == 'blocked' else Item.objects.create
        return factory(
            mode='task', item_key=key, title=f'{key} title',
            assignee_id=assignee_id, assignee_email=assignee,
            status=status, priority=kw.pop('priority', 'normal'),
            project=self.project, workflow_name=kw.pop('workflow_name', None),
            metadata=kw.pop('metadata', {}) or {},
            **kw,
        )

    def _snapshot(self, **kw):
        return agent_activity.agent_activity_snapshot(team=ENG, **kw)

    def test_wip_projection_with_blockers_and_prs(self):
        work = self._item('P305-1', ALICE, metadata={
            'pull_requests': [{'url': 'https://github.com/x/y/pull/1', 'status': 'open'}],
        })
        blocker = self._item('P305-2', BOB, status='open')
        ItemLink.objects.create(source_item=work, target_item=blocker,
                                link_type='is_blocked_by', created_by_id=self.user.id)
        self._item('P305-3', ALICE, status='done')  # terminal -> excluded

        snap = self._snapshot()
        alice = next(a for a in snap['agents'] if a['email'] == ALICE)
        # Linking the active blocker auto-blocks the work item (canonical
        # workflow behavior) -> it counts as waiting, not working WIP.
        self.assertEqual(alice['wip_count'], 0)
        self.assertEqual(alice['waiting_count'], 1)
        item = alice['items'][0]
        self.assertEqual(item['key'], 'P305-1')
        self.assertTrue(item['waiting'])
        self.assertEqual([b['key'] for b in item['blockers']], ['P305-2'])
        self.assertEqual(item['pull_requests'],
                         [{'url': 'https://github.com/x/y/pull/1', 'status': 'open'}])
        self.assertIn(ENG, alice['teams'])

    def test_terminal_blocker_not_reported(self):
        work = self._item('P305-10', ALICE)
        resolved = self._item('P305-11', BOB, status='done')
        ItemLink.objects.create(source_item=work, target_item=resolved,
                                link_type='is_blocked_by', created_by_id=self.user.id)
        snap = self._snapshot()
        alice = next(a for a in snap['agents'] if a['email'] == ALICE)
        self.assertEqual(alice['items'][0]['blockers'], [])

    def test_blocks_direction_also_projects(self):
        blocker = self._item('P305-20', BOB, status='open')
        work = self._item('P305-21', ALICE)
        ItemLink.objects.create(source_item=blocker, target_item=work,
                                link_type='blocks', created_by_id=self.user.id)
        snap = self._snapshot()
        alice = next(a for a in snap['agents'] if a['email'] == ALICE)
        self.assertEqual([b['key'] for b in alice['items'][0]['blockers']],
                         ['P305-20'])

    def test_waiting_items_counted_separately_from_wip_pressure(self):
        self._item('P305-30', ALICE, status='in_progress')
        satisfy_blocked_invariant(self._item('P305-31', ALICE, status='blocked'))
        snap = self._snapshot()
        alice = next(a for a in snap['agents'] if a['email'] == ALICE)
        self.assertEqual(alice['wip_count'], 1)
        self.assertEqual(alice['waiting_count'], 1)
        self.assertEqual(alice['wip_pressure'], round(1 / agent_activity.WIP_CAP, 2))
        blocked_row = next(i for i in alice['items'] if i['key'] == 'P305-31')
        self.assertTrue(blocked_row['waiting'])

    def test_offline_agent_state_comes_from_agent_availability(self):
        AgentAvailability.objects.create(
            email=BOB, active=False, status='on_leave', reason='provider outage')
        item = self._item('P305-40', BOB)
        old = timezone.now() - timedelta(hours=agent_activity.STALE_AFTER_HOURS + 2)
        Item.objects.filter(pk=item.pk).update(updated_at=old)
        snap = self._snapshot()
        bob = next(a for a in snap['agents'] if a['email'] == BOB)
        self.assertEqual(bob['availability'],
                         {'state': 'on_leave', 'reason': 'provider outage'})
        # An unavailable agent is not additionally flagged stale — its state
        # already explains the silence.
        self.assertFalse(bob['stale'])

    def test_stale_agent_flagged_when_wip_untouched_past_cutoff(self):
        old = timezone.now() - timedelta(hours=agent_activity.STALE_AFTER_HOURS + 2)
        item = self._item('P305-50', ALICE)
        Item.objects.filter(pk=item.pk).update(updated_at=old)
        snap = self._snapshot()
        alice = next(a for a in snap['agents'] if a['email'] == ALICE)
        self.assertTrue(alice['stale'])
        bob = next(a for a in snap['agents'] if a['email'] == BOB)
        self.assertFalse(bob['stale'])  # no WIP -> idle, not stale

    def test_agent_with_no_items_has_empty_projection(self):
        snap = self._snapshot()
        bob = next(a for a in snap['agents'] if a['email'] == BOB)
        self.assertEqual(bob['wip_count'], 0)
        self.assertEqual(bob['items'], [])
        self.assertEqual(bob['availability'], {'state': 'active', 'reason': ''})

    def test_team_filter_limits_roster(self):
        TeamRoster.objects.create(team_name='pls305-other',
                                  tiers=[['carol-pls305@shizuha.com']], is_active=True)
        snap = agent_activity.agent_activity_snapshot(team=ENG)
        emails = {a['email'] for a in snap['agents']}
        self.assertIn(ALICE, emails)
        self.assertNotIn('carol-pls305@shizuha.com', emails)


class AgentActivityApiTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        TeamRoster.objects.create(team_name=ENG, tiers=[[ALICE]], is_active=True)
        self.project = Project.objects.create(name='PLS305 API', key='P305A', org_id=1)
        Item.objects.create(
            mode='task', item_key='P305A-1', title='api item',
            assignee_email=ALICE, status='in_progress', project=self.project,
        )

    def test_endpoint_returns_snapshot(self):
        response = self.client.get(f'/api/items/agent-activity/?team={ENG}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('agents', data)
        self.assertIn('lanes', data)
        alice = next(a for a in data['agents'] if a['email'] == ALICE)
        self.assertEqual(alice['items'][0]['key'], 'P305A-1')

    def test_endpoint_requires_auth(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/items/agent-activity/')
        self.assertIn(response.status_code, (401, 403))
