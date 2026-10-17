"""PLS-1035: lane-health aggregation + reporter-accessible API tests."""
from unittest import mock

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from tasks.models import AgentAvailability, Item, ItemActivity, TeamRoster
from tasks.services.lane_health import all_lanes_health, lane_health
from tasks.services.workflow_service import WorkflowService


ROSTER = {
    'engineering': [
        ['nagi@shizuha.com'],
        ['aoi@shizuha.com'],
    ],
    'review': [
        ['revi@shizuha.com'],
    ],
    'engineeringv1': [
        ['nagi@shizuha.com'],
        ['aoi@shizuha.com'],
    ],
    'engineeringqd': [
        ['nagi@shizuha.com'],
    ],
    'engineeringla': [
        ['nagi@shizuha.com'],
    ],
    'engineeringapi': [
        ['nagi@shizuha.com'],
    ],
    # Dedicated stall-test lanes: mocked roster MUST match the DB TeamRoster so
    # the item-relative verdict does not read seat_not_team_member.
    'engineeringstall': [
        ['aoi@shizuha.com'],
    ],
    'engineeringcap': [
        ['nagi@shizuha.com'],
    ],
    'engineeringmerge': [
        ['nagi@shizuha.com'],
    ],
    'engineeringmixed': [
        ['nagi@shizuha.com'],
        ['aoi@shizuha.com'],
    ],
    'architecture': [
        ['nagi@shizuha.com'],
    ],
}


class LaneHealthServiceTests(TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(WorkflowService, '_pod_roster', return_value=ROSTER),
            mock.patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                side_effect=lambda email, **kw: email == 'nagi@shizuha.com',
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def test_lane_health_returns_roster_version_and_seats(self):
        TeamRoster.objects.create(
            team_name='engineeringv1',
            organization_slug='shizuha',
            tiers=[['nagi@shizuha.com'], ['aoi@shizuha.com']],
            version=6,
            is_active=True,
            wip_enforced=True,
            ready_wip_cap=5,
            active_wip_cap=10,
        )
        snap = lane_health('engineeringv1')
        self.assertEqual(snap['team'], 'engineeringv1')
        self.assertEqual(snap['roster']['version'], 7)  # 6 + 1 auto-increment
        self.assertEqual(snap['roster']['active'], True)
        self.assertEqual(snap['roster']['ready_wip_cap'], 5)
        self.assertEqual(snap['roster']['member_count'], 2)
        # nagi is live, aoi is not (per the side_effect).
        by_email = {s['email']: s for s in snap['seats']}
        self.assertTrue(by_email['nagi@shizuha.com']['live'])
        self.assertFalse(by_email['aoi@shizuha.com']['live'])
        self.assertEqual(snap['resolver']['routable_seats'], 1)
        self.assertEqual(snap['resolver']['staffed'], True)

    def test_lane_health_queue_depth_counts_active_statuses(self):
        # Create a project + items assigned to the engineering lane.
        from tasks.models import Project
        proj = Project.objects.create(name='Lane Health Test', key='LH', org_id=1)
        for status in ('todo', 'in_progress', 'done'):
            Item.objects.create(
                project=proj, mode='task', status=status,
                assignment_group='engineeringqd', title=f'LH-{status}',
            )
        snap = lane_health('engineeringqd')
        qd = snap['queue_depth']
        self.assertEqual(qd['by_status'].get('todo'), 1)
        self.assertEqual(qd['by_status'].get('in_progress'), 1)
        # 'done' is not an active queue status.
        self.assertNotIn('done', qd['by_status'])
        self.assertEqual(qd['total_active'], 2)

    def test_lane_health_last_assignment(self):
        from tasks.models import Project
        proj = Project.objects.create(name='Lane Health Last Assign', key='LHLA', org_id=1)
        item = Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringla', title='LHLA-1',
        )
        ItemActivity.objects.create(
            item=item, action='assigned', user_email='nagi@shizuha.com', user_id=1,
        )
        snap = lane_health('engineeringla')
        self.assertIsNotNone(snap['last_assignment'])
        self.assertEqual(snap['last_assignment']['assignee'], 'nagi@shizuha.com')

    def test_all_lanes_health_returns_active_rosters(self):
        TeamRoster.objects.create(
            team_name='engineeringall', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com']], version=1, is_active=True,
        )
        TeamRoster.objects.create(
            team_name='reviewall', organization_slug='shizuha',
            tiers=[['revi@shizuha.com']], version=2, is_active=True,
        )
        TeamRoster.objects.create(
            team_name='retiredall', organization_slug='shizuha',
            tiers=[['old@shizuha.com']], version=3, is_active=False,
        )
        lanes = all_lanes_health()
        teams = {l['team'] for l in lanes}
        self.assertIn('engineeringall', teams)
        self.assertIn('reviewall', teams)
        self.assertNotIn('retiredall', teams)

    def test_stall_none_when_routable_seat_exists(self):
        TeamRoster.objects.create(
            team_name='engineeringv1', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com'], ['aoi@shizuha.com']],
            version=6, is_active=True,
        )
        snap = lane_health('engineeringv1')
        # nagi is live (mocked) → routable seat exists → not stalled.
        self.assertEqual(snap['stall']['failure_mode'], 'none')
        self.assertFalse(snap['stall']['is_stalled'])

    def test_stall_seat_not_live_when_all_non_routable_are_liveness(self):
        # Lane with only aoi (not live per the mock) and queued demand.
        TeamRoster.objects.create(
            team_name='engineeringstall', organization_slug='shizuha',
            tiers=[['aoi@shizuha.com']], version=1, is_active=True,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Stall Probe', key='SP', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringstall', title='SP-1',
        )
        snap = lane_health('engineeringstall')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'seat_not_live')
        # Per-seat reasons use the resolver vocabulary, not a parallel set.
        self.assertIn('seat_not_live', snap['seats'][0]['reasons'])

    def test_stall_over_capacity_from_wip_accounting(self):
        # All seats live (routable) but at/over WIP cap → over_capacity, from
        # WIP accounting (never availability).
        TeamRoster.objects.create(
            team_name='engineeringcap', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com']], version=1, is_active=True,
            wip_enforced=True, active_wip_cap=1, ready_wip_cap=1,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Cap Probe', key='CP', org_id=1)
        # Two active items assigned to nagi fill the active slot (cap=1); the
        # load comes from items OTHER than the probe.
        Item.objects.create(
            project=proj, mode='task', status='in_progress',
            assignment_group='engineeringcap', title='CP-1',
            assignee_email='nagi@shizuha.com', assignee_id=1,
        )
        Item.objects.create(
            project=proj, mode='task', status='in_progress',
            assignment_group='engineeringcap', title='CP-2',
            assignee_email='nagi@shizuha.com', assignee_id=1,
        )
        # Queued demand for the lane (stalest active → the probe).
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringcap', title='CP-3',
        )
        snap = lane_health('engineeringcap')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'over_capacity')
        self.assertTrue(snap['seats'][0]['capacity']['enforced'])
        self.assertTrue(snap['seats'][0]['capacity']['over_capacity'])

    def test_stall_authority_drift_first(self):
        # Roster version advanced vs last-healthy baseline → authority_drift
        # wins even though the seat also reads seat_not_live.
        from tasks.services.lane_health import cache
        cache.set('pulse:lane_stall:healthy_roster:engineeringstall', '1', timeout=86400)
        self.addCleanup(
            cache.delete, 'pulse:lane_stall:healthy_roster:engineeringstall',
        )
        TeamRoster.objects.create(
            team_name='engineeringstall', organization_slug='shizuha',
            tiers=[['aoi@shizuha.com']], version=2, is_active=True,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Auth Probe', key='AP', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringstall', title='AP-1',
        )
        snap = lane_health('engineeringstall')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'authority_drift')

    def test_stall_rollout_window_when_fleet_digest_changed_recently(self):
        # PLS-1061: a fleet-wide agent-runtime digest change within the window
        # classifies zero-routable-seats as rollout_window (not seat_not_live).
        from django.utils import timezone
        from tasks.services.lane_health import (
            _FLEET_RUNTIME_DIGEST_CACHE_KEY, cache,
        )
        cache.set(
            _FLEET_RUNTIME_DIGEST_CACHE_KEY,
            {
                'digest': '8878023b',
                'changed_at': (timezone.now() - timezone.timedelta(minutes=2)).isoformat(),
            },
            timeout=86400,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        TeamRoster.objects.create(
            team_name='engineeringstall', organization_slug='shizuha',
            tiers=[['aoi@shizuha.com']], version=1, is_active=True,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Rollout Probe', key='RP', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringstall', title='RP-1',
        )
        snap = lane_health('engineeringstall')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'rollout_window')
        # Per-seat runtime signal surfaces the digest for at-a-glance triage.
        self.assertEqual(snap['seats'][0]['runtime']['digest'], '8878023b')

    def test_stall_rollout_window_expired_after_window(self):
        # A digest change OLDER than the window is NOT a rollout — the lane
        # falls through to the per-seat family classification (seat_not_live).
        from django.utils import timezone
        from tasks.services.lane_health import (
            _FLEET_RUNTIME_DIGEST_CACHE_KEY, cache,
        )
        cache.set(
            _FLEET_RUNTIME_DIGEST_CACHE_KEY,
            {
                'digest': '8878023b',
                'changed_at': (
                    timezone.now() - timezone.timedelta(minutes=30)
                ).isoformat(),
            },
            timeout=86400,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        TeamRoster.objects.create(
            team_name='engineeringstall', organization_slug='shizuha',
            tiers=[['aoi@shizuha.com']], version=1, is_active=True,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Rollout Expired', key='RE', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringstall', title='RE-1',
        )
        snap = lane_health('engineeringstall')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'seat_not_live')

    def test_record_fleet_runtime_digest_producer(self):
        # PLS-1061 producer: recording a digest writes the signal; re-recording
        # the SAME digest keeps the original changed_at (rollout already
        # counted); a NEW digest refreshes changed_at.
        from django.utils import timezone
        from tasks.services.lane_health import (
            _FLEET_RUNTIME_DIGEST_CACHE_KEY, cache, record_fleet_runtime_digest,
        )
        self.addCleanup(cache.delete, _FLEET_RUNTIME_DIGEST_CACHE_KEY)
        s1 = record_fleet_runtime_digest('8878023b')
        self.assertIsNotNone(s1)
        self.assertEqual(s1['digest'], '8878023b')
        changed1 = s1['changed_at']
        # Same digest → changed_at preserved.
        s2 = record_fleet_runtime_digest('8878023b')
        self.assertEqual(s2['changed_at'], changed1)
        # New digest → changed_at refreshed.
        s3 = record_fleet_runtime_digest('deadbeef')
        self.assertEqual(s3['digest'], 'deadbeef')
        self.assertNotEqual(s3['changed_at'], changed1)
        # Empty digest → no write.
        self.assertIsNone(record_fleet_runtime_digest('  '))

    def test_stall_mixed_when_multiple_families(self):
        TeamRoster.objects.create(
            team_name='engineeringmixed', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com'], ['aoi@shizuha.com']],
            version=1, is_active=True, wip_enforced=True,
            active_wip_cap=1, ready_wip_cap=1,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Mixed Probe', key='MP', org_id=1)
        # nagi is live but at/over WIP cap (capacity family); aoi is not live
        # (liveness family) → mixed.
        Item.objects.create(
            project=proj, mode='task', status='in_progress',
            assignment_group='engineeringmixed', title='MP-1',
            assignee_email='nagi@shizuha.com', assignee_id=1,
        )
        Item.objects.create(
            project=proj, mode='task', status='in_progress',
            assignment_group='engineeringmixed', title='MP-2',
            assignee_email='nagi@shizuha.com', assignee_id=1,
        )
        Item.objects.create(
            project=proj, mode='task', status='todo',
            assignment_group='engineeringmixed', title='MP-3',
        )
        snap = lane_health('engineeringmixed')
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'mixed')

    def test_merge_sod_surfaces_no_distinct_available_merger(self):
        TeamRoster.objects.create(
            team_name='engineeringmerge', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com']], version=1, is_active=True,
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Merge Sod', key='MS', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='awaiting_merge',
            assignment_group='engineeringmerge', title='MS-1',
            metadata={'merge_sod_hold': {'reason': 'no_distinct_available_merger'}},
        )
        snap = lane_health('engineeringmerge')
        self.assertEqual(snap['merge_sod']['count'], 1)
        self.assertEqual(
            snap['merge_sod']['holds'][0]['reason'],
            'no_distinct_available_merger',
        )

    def test_effective_routable_sod_exhausted_architecture_author(self):
        """PLS-1133: only live seat is author of pending reviews → sod_exhausted."""
        # update_or_create, not create: 'architecture' is a real seeded team
        # (migration 0055) and the CI suite runs with --keepdb, so a stale
        # seeded/polluted row for (shizuha, architecture) may exist. The test
        # needs THIS roster content (nagi-only tier) regardless of prior state.
        TeamRoster.objects.update_or_create(
            team_name='architecture', organization_slug='shizuha',
            defaults={
                'tiers': [['nagi@shizuha.com']],
                'version': 1,
                'is_active': True,
            },
        )
        from tasks.models import Project
        proj = Project.objects.create(name='Arch Sod', key='AS', org_id=1)
        item = Item.objects.create(
            project=proj, mode='task', status='in_review',
            assignment_group='architecture', title='AS-1',
            created_by_email='nagi@shizuha.com',
        )
        # The default ``simple`` workflow's model save coerces unknown stages
        # (in_review → in_progress — run 5432 log), which empties
        # _pending_sod_demand for the architecture lane (_SOD_DEMAND_STATUSES
        # ['architecture'] = {'in_review'}) and defeats the SoD annotation.
        # Persist the production review stage directly — same pattern as
        # test_assignment.py's in_review fixtures.
        Item.objects.filter(pk=item.pk).update(
            workflow_name='Bug Workflow',
            status='in_review',
        )
        item.refresh_from_db()

        class _V:
            def __init__(self, ok, reasons):
                self.ok = ok
                self.reasons = reasons

        def _verdict(item, team, email, enforce_cap=False):
            # Probe may be the same in_review item; live-for-capacity uses
            # availability, SoD is applied in _annotate_sod per pending item.
            if getattr(item, 'status', '') == 'in_review':
                return _V(True, ('review_sod_author_identity',))
            return _V(True, ())

        with mock.patch.object(WorkflowService, '_seat_routability_verdict', side_effect=_verdict):
            snap = lane_health('architecture')
        self.assertEqual(snap['resolver']['routable_seats'], 1)
        self.assertEqual(snap['resolver']['effective_routable'], 0)
        self.assertTrue(snap['stall']['is_stalled'])
        self.assertEqual(snap['stall']['failure_mode'], 'sod_exhausted')
        self.assertTrue(snap['seats'][0]['sod_disqualified'])


class LaneHealthApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.patches = [
            mock.patch.object(WorkflowService, '_pod_roster', return_value=ROSTER),
            mock.patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                side_effect=lambda email, **kw: email == 'nagi@shizuha.com',
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def test_lane_health_endpoint_requires_auth(self):
        resp = self.client.get('/shizuha-pulse/api/lane-health/')
        self.assertIn(resp.status_code, (401, 403))

    def test_lane_health_endpoint_authenticated_any_seat(self):
        # Any authenticated seat (not DevOps-only) can read the lane-health view.
        user = self._make_user('reporter@shizuha.com')
        self.client.force_authenticate(user=user)
        TeamRoster.objects.create(
            team_name='engineeringapi', organization_slug='shizuha',
            tiers=[['nagi@shizuha.com']], version=3, is_active=True,
        )
        resp = self.client.get('/shizuha-pulse/api/lane-health/?team=engineeringapi')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['team'], 'engineeringapi')
        self.assertEqual(body['roster']['version'], 4)
        self.assertIn('seats', body)
        self.assertIn('resolver', body)
        self.assertIn('queue_depth', body)

    @staticmethod
    def _make_user(email):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.create_user(
            username=email.split('@')[0], email=email, password='x',
        )
