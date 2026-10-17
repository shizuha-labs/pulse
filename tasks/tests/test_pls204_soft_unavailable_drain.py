"""PLS-204: soft provider/capacity unavailability must not drain live owners.

Target eligibility and failover-drain eligibility are different contracts:
provider/capacity-limited agents must not receive new work, but if they are
still making authenticated Pulse calls they keep already-owned work during the
short drain grace. Hard-off states still drain.
"""
import json
import time
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _agent_has_recent_pulse_heartbeat,
    _agent_unavailability_reason,
    _soft_unavailable_owner_protected,
    refresh_agent_health_and_assign_group_queue,
    restore_false_positive_failovers_for_agent,
    sweep_agent_availability,
    sweep_rebalance_pods,
)
from tasks.views import AgentSelfAvailabilityView
from .base import create_blocked_item, satisfy_blocked_invariant

NOVA = 'nova@shizuha.com'
SORA = 'sora@shizuha.com'


class _FakeUserService:
    USERS = {
        NOVA: (701, NOVA),
        SORA: (702, SORA),
    }

    def get_user_by_email(self, email):
        rec = self.USERS.get((email or '').strip().lower())
        return {'id': rec[0], 'email': rec[1]} if rec else None

    def get_user_by_id(self, user_id):
        for uid, email in self.USERS.values():
            if int(user_id) == uid:
                return {'id': uid, 'email': email}
        return None


class _FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = {}
        self.ttls = {key: 300_000 for key in self.values}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.writes[key] = value
        self.ttls[key] = int(ttl) * 1000

    def set(self, key, value, nx=False, ex=None, px=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.writes[key] = value
        ttl = px if px is not None else ((int(ex) * 1000) if ex else None)
        if ttl is not None:
            self.ttls[key] = ttl
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.writes[key] = None

    def scan_iter(self, pattern, count=200):
        return []

    def pipeline(self):
        return _FakeRedisPipeline(self)


class _FakeRedisPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def watch(self, *keys):
        return None

    def unwatch(self):
        return None

    def get(self, key):
        return self.redis.get(key)

    def pttl(self, key):
        return self.redis.ttls.get(key, -2)

    def multi(self):
        return None

    def set(self, key, value, px=None):
        self.commands.append(('set', key, value, px))

    def delete(self, key):
        self.commands.append(('delete', key))

    def execute(self):
        for command in self.commands:
            if command[0] == 'set':
                _, key, value, ttl = command
                self.redis.values[key] = value
                self.redis.writes[key] = value
                if ttl is not None:
                    self.redis.ttls[key] = ttl
            else:
                _, key = command
                self.redis.delete(key)
        self.commands = []

    def reset(self):
        self.commands = []


class SoftUnavailableDrainTests(TestCase):
    def setUp(self):
        from tasks.models import Project, TeamRoster

        self.project = Project.objects.create(
            name='PLS-204 soft-unavailable drain',
            key='P204',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[NOVA, SORA]],
                'wip_enforced': False,
                'ready_wip_cap': 5,
                'active_wip_cap': 0,
                'is_active': True,
            },
        )

    def _run_sweep(
        self,
        reason,
        recent_heartbeat,
        only_agent_email=None,
        assignment_delay=None,
    ):
        def _available(email):
            return (email or '').strip().lower() == SORA

        patches = [
            mock.patch.dict('os.environ', {
                'PULSE_API_HEALTH_URL': 'disabled',
                'PULSE_HEALTH_EXPORTER_URL': 'disabled',
                'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
            }),
            mock.patch('tasks.user_service.user_service', _FakeUserService()),
            mock.patch.object(WorkflowService, '_pod_roster', return_value={
                'engineering': [NOVA, SORA],
            }),
            mock.patch.object(WorkflowService, '_is_agent_available_for_assignment', side_effect=_available),
            mock.patch('tasks.tasks._agent_unavailability_reason', return_value=reason),
            mock.patch('tasks.tasks._agent_has_recent_pulse_heartbeat', return_value=recent_heartbeat),
            mock.patch(
                'tasks.tasks.send_item_notification.delay',
                new=assignment_delay or mock.Mock(),
            ),
            mock.patch('tasks.tasks.notify_comment_mentions.delay', return_value=None),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            return sweep_rebalance_pods.run(
                max_skew=0,
                only_agent_email=only_agent_email,
            )

    def test_provider_down_recent_heartbeat_keeps_owned_work(self):
        from tasks.models import Comment, Item

        owned = Item.objects.create(
            title='VEN launch blocker should stay with live owner',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='provider_down', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)
        self.assertEqual(Comment.objects.filter(item=owned, author_email='system@pulse').count(), 0)

    def test_flapping_provider_down_owner_releases_work_after_window(self):
        """A restart-refreshed heartbeat must not pin work forever.

        Observed 2026-07-27: kai (provider_down, Codex) was restarted by Hive
        ~10x in an hour. Each start issued one authenticated Pulse request,
        refreshing pulse:agent_hb and resetting the 20-minute drain grace, so 54
        items (1 open + 53 in_progress) could never drain to two healthy peers.
        The heartbeat stays FRESH here -- only the continuous-downtime window is
        exhausted -- and the work must still move.
        """
        from tasks.models import Item

        owned = Item.objects.create(
            title='stranded on a flapping provider_down owner',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with mock.patch('tasks.tasks._agent_unavailable_seconds', return_value=4200):
            result = self._run_sweep(reason='provider_down', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertGreaterEqual(result['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)

    def test_brief_provider_blip_still_keeps_owned_work(self):
        """The PLS-204 contract survives: a SHORT soft outage still protects."""
        from tasks.models import Item

        owned = Item.objects.create(
            title='short provider blip must not churn ownership',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with mock.patch('tasks.tasks._agent_unavailable_seconds', return_value=60):
            result = self._run_sweep(reason='provider_down', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)

    def test_capacity_limited_recent_heartbeat_keeps_owned_work(self):
        from tasks.models import Item

        owned = Item.objects.create(
            title='owned task should stay with capacity-limited live owner',
            mode='task', status='todo', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='capacity_limited', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)

    def test_provider_down_without_recent_heartbeat_drains_as_before(self):
        from tasks.models import Item

        assignment_delay = mock.Mock()
        owned = Item.objects.create(
            title='stale provider-down owner should drain',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = self._run_sweep(
                reason='provider_down',
                recent_heartbeat=False,
                assignment_delay=assignment_delay,
            )

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertEqual(owned.assignee_id, 702)
        assignment_delay.assert_not_called()

    def test_disabled_owner_drains_even_with_recent_heartbeat(self):
        from tasks.models import Item

        owned = Item.objects.create(
            title='disabled owner must still drain',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='disabled', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertNotIn('auto_failover_restore', owned.metadata or {})

    def test_failover_aborts_when_every_live_peer_is_at_capacity(self):
        """PLAT-5032: a hard-down verdict cannot override target capacity.

        Production order: the real rebalance sweep sees the current owner as
        unavailable, resolves the only live peer, and finds that peer already at
        the enforced assigned-ready WIP cap.  The item must stay with its current owner;
        choosing the saturated peer would merely move stranded work and destroy
        the review/ownership context without creating execution headroom.
        """
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.filter(
            organization_id=1,
            team_name='engineering',
        ).update(
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )

        Item.objects.create(
            title='live peer already at its assigned-ready cap',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=702, assignee_email=SORA,
            workflow_name='autonomous-dev',
        )
        owned = Item.objects.create(
            title='unavailable owner work must not move into saturation',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with self.assertLogs('tasks.tasks', level='WARNING') as logs:
            result = self._run_sweep(reason='disabled', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)
        self.assertTrue(any(
            'all routable live peers are at capacity' in message
            for message in logs.output
        ))

    def test_urgent_failover_seats_a_peer_already_at_cap(self):
        """Operator 2026-09-27: urgent assignment ignores the WIP cap.

        The same saturated peer that refuses a high-priority failover accepts
        the urgent item, so a disabled owner's urgent work does not stay
        stranded.
        """
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.filter(
            organization_id=1,
            team_name='engineering',
        ).update(
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        Item.objects.create(
            title='live peer already at its assigned-ready cap',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=702, assignee_email=SORA,
            workflow_name='autonomous-dev',
        )
        owned = Item.objects.create(
            title='urgent work leaves the disabled owner',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='disabled', recent_heartbeat=True)

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)

    def test_unavailable_plus_overcap_reaches_fixed_point_across_two_sweeps(self):
        """PLAT-5032 AC7: one unavailable seat + over-cap peer must not oscillate.

        Production order (reika ORIG-191 / SCS-95): failover moves work off the
        unavailable seat, then the next rebalance pass moves it back because the
        peer is overloaded.  Two consecutive real sweeps must reach a fixed
        point on the NOVA↔SORA ownership edge when:

          * NOVA is hard-unavailable (disabled) and holds open work
          * SORA is the only live peer and is saturated on both axes
            (at ready_cap + over active_cap)

        Failover of NOVA's open item must abort (AC2). Shed/skew must not
        donate SORA's excess onto NOVA (AC5). PLS-509 may reclaim SORA's own
        over-cap active work onto the group queue / a legal todo transition —
        that is not oscillation and is out of scope for this assertion. The
        load-bearing invariant is: NOVA's item never leaves NOVA, and none of
        SORA's items ever land on NOVA, across two consecutive sweeps.
        """
        from tasks.models import Comment, Item, TeamRoster

        TeamRoster.objects.filter(
            organization_id=1,
            team_name='engineering',
        ).update(
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )

        # Live peer saturated on both axes the sweep cares about:
        #   - 1 open → at ready_cap (failover of NOVA's open item must abort)
        #   - 2 in_progress → over active_cap (shed/skew would donate if an
        #     unavailable seat were wrongly eligible as an under-cap target)
        peer_ready = Item.objects.create(
            title='live peer at ready-cap (AC7)',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=702, assignee_email=SORA,
            workflow_name='autonomous-dev',
        )
        peer_active_a = Item.objects.create(
            title='live peer over active-cap A (AC7)',
            mode='task', status='in_progress', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=702, assignee_email=SORA,
            workflow_name='autonomous-dev',
        )
        peer_active_b = Item.objects.create(
            title='live peer over active-cap B (AC7)',
            mode='task', status='in_progress', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=702, assignee_email=SORA,
            workflow_name='autonomous-dev',
        )
        owned = Item.objects.create(
            title='unavailable owner work must stay put across two sweeps (AC7)',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )
        peer_ids = {peer_ready.pk, peer_active_a.pk, peer_active_b.pk}

        def _available(email):
            return (email or '').strip().lower() == SORA

        abort_logs = []
        for _ in range(2):
            patches = [
                mock.patch.dict('os.environ', {
                    'PULSE_API_HEALTH_URL': 'disabled',
                    'PULSE_HEALTH_EXPORTER_URL': 'disabled',
                    'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
                }),
                mock.patch('tasks.user_service.user_service', _FakeUserService()),
                mock.patch.object(WorkflowService, '_pod_roster', return_value={
                    'engineering': [NOVA, SORA],
                }),
                mock.patch.object(
                    WorkflowService,
                    '_is_agent_available_for_assignment',
                    side_effect=_available,
                ),
                mock.patch('tasks.tasks._agent_unavailability_reason', return_value='disabled'),
                mock.patch('tasks.tasks._agent_has_recent_pulse_heartbeat', return_value=True),
                mock.patch('tasks.tasks.send_item_notification.delay', new=mock.Mock()),
                mock.patch('tasks.tasks.notify_comment_mentions.delay', return_value=None),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                # max_skew=1 arms skew rebalance; AC5 must still refuse NOVA.
                with self.assertLogs('tasks.tasks', level='WARNING') as logs:
                    sweep_rebalance_pods.run(max_skew=1)
                abort_logs.extend(logs.output)

        owned.refresh_from_db()
        peer_ready.refresh_from_db()
        peer_active_a.refresh_from_db()
        peer_active_b.refresh_from_db()

        # Fixed point on the unavailable↔over-cap edge.
        self.assertEqual(owned.assignee_email, NOVA)
        self.assertEqual(owned.assignee_id, 701)
        for peer in (peer_ready, peer_active_a, peer_active_b):
            self.assertNotEqual(
                (peer.assignee_email or '').strip().lower(),
                NOVA,
                f'AC7: peer item {peer.pk} must never land on the unavailable seat',
            )

        # No Auto-failover / Auto-rebalance comments that transfer ownership
        # across the NOVA↔SORA edge in either direction.
        cross_edge = Comment.objects.filter(
            item_id__in=[owned.pk, *peer_ids],
            author_email='system@pulse',
        )
        for comment in cross_edge:
            body = comment.content or ''
            if not body.startswith('Auto-'):
                continue
            lowered = body.lower()
            mentions_nova = 'nova@shizuha.com' in lowered
            mentions_sora = 'sora@shizuha.com' in lowered
            self.assertFalse(
                mentions_nova and mentions_sora,
                f'AC7 oscillation comment on item {comment.item_id}: {body!r}',
            )
            if comment.item_id == owned.pk:
                self.assertNotIn(
                    'sora@shizuha.com',
                    lowered,
                    f'AC7: NOVA-owned item must not Auto-failover to SORA: {body!r}',
                )
            if comment.item_id in peer_ids:
                self.assertNotIn(
                    'nova@shizuha.com',
                    lowered,
                    f'AC7: SORA-owned item must not Auto-rebalance onto NOVA: {body!r}',
                )

        self.assertTrue(
            any('all routable live peers are at capacity' in message for message in abort_logs),
            'AC7 expects the PLAT-5032 capacity-abort path on the unavailable owner',
        )


    def test_failover_aborts_when_atomic_target_reservation_loses_race(self):
        """The write fence re-checks capacity after candidate selection.

        A peer may have headroom in the sweep snapshot and lose its last slot to
        a concurrent assignment before the row-locked owner write.  The failed
        reservation must leave both owner and restoration metadata untouched.
        """
        from tasks.models import Item

        owned = Item.objects.create(
            title='unavailable owner must survive a lost target-slot race',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with mock.patch.object(
            WorkflowService,
            '_reserve_wip_slot',
            return_value=False,
        ), self.assertLogs('tasks.tasks', level='WARNING') as logs:
            result = self._run_sweep(
                reason='offline',
                recent_heartbeat=False,
            )

        owned.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)
        self.assertNotIn('auto_failover_restore', owned.metadata or {})
        self.assertTrue(any(
            'lost the atomic WIP slot reservation' in message
            for message in logs.output
        ))

    def test_write_fence_rechecks_activity_after_stale_down_snapshot(self):
        """A heartbeat racing the sweep wins before ownership is mutated."""
        from tasks.models import Item

        owned = Item.objects.create(
            title='new activity wins over stale down snapshot',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        states = iter((False, True))
        with mock.patch(
            'tasks.tasks._soft_unavailable_owner_protected',
            side_effect=lambda _email: next(states, True),
        ) as protected:
            result = self._run_sweep(
                reason='provider_down',
                recent_heartbeat=False,
            )

        owned.refresh_from_db()
        self.assertGreaterEqual(protected.call_count, 2)
        self.assertEqual(result['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)

    def test_roster_capacity_counts_target_identity_not_source_owner(self):
        """PLAT-5032: admission evaluates the destination seat's WIP.

        The item still carries the unavailable source owner's ID when candidate
        capacity is checked.  That ID must not be folded into the destination
        query: with two source-owned ready items and one empty target slot, one
        item moves and the second remains rather than both being falsely held.
        """
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.filter(
            organization_id=1,
            team_name='engineering',
        ).update(
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=1,
        )
        first = Item.objects.create(
            title='first source-owned ready item gets the one target slot',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )
        second = Item.objects.create(
            title='second source-owned ready item waits for capacity',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with mock.patch('tasks.tasks._reclaim_wip_overload', return_value={
            'active_stopped': 0,
            'active_queued': 0,
            'ready_queued': 0,
        }):
            result = self._run_sweep(reason='disabled', recent_heartbeat=True)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(first.assignee_email, SORA)
        self.assertEqual(second.assignee_email, NOVA)

    def test_soft_failover_restores_after_newer_activity_and_is_idempotent(self):
        """Real caller sequence: failover -> newer heartbeat -> failback twice.

        The first top-level sweep moves an offline-classified owner to the only
        live peer and writes the immutable receipt.  A later authenticated Pulse
        heartbeat disproves that soft verdict; the event task restores the
        untouched original owner.  Replaying the task is a no-op.
        """
        from tasks.models import Comment, Item

        fake = _FakeRedis({
            WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
            WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                NOVA: 'offline',
            }),
        })
        owned = Item.objects.create(
            title='false-positive failover must restore original context',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.agent_liveness._redis_client', return_value=fake):
            moved = self._run_sweep(
                reason='offline',
                recent_heartbeat=False,
            )

        owned.refresh_from_db()
        marker = (owned.metadata or {}).get('auto_failover_restore') or {}
        self.assertEqual(moved['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertEqual(marker.get('state'), 'active')
        self.assertEqual(marker.get('from_email'), NOVA)
        self.assertEqual(marker.get('to_email'), SORA)
        self.assertTrue(marker.get('receipt_id'))
        self.assertIn('pulse:agent_failback_pending:nova@shizuha.com', fake.values)

        # This timestamp is written after the receipt transaction commits, just
        # as the authentication boundary writes it before middleware dispatches.
        fake.set('pulse:agent_hb:nova@shizuha.com', repr(time.time()))

        def _available(email):
            return (email or '').strip().lower() == SORA

        patches = (
            mock.patch('tasks.user_service.user_service', _FakeUserService()),
            mock.patch.object(WorkflowService, '_pod_roster', return_value={
                'engineering': [NOVA, SORA],
            }),
            mock.patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                side_effect=_available,
            ),
            mock.patch.object(WorkflowService, '_redis_client', return_value=fake),
            mock.patch('tasks.agent_liveness._redis_client', return_value=fake),
            mock.patch('tasks.tasks.send_item_notification.delay', return_value=None),
            mock.patch('tasks.tasks.notify_comment_mentions.delay', return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            first = restore_false_positive_failovers_for_agent.run(NOVA)
            second = restore_false_positive_failovers_for_agent.run(NOVA)

        owned.refresh_from_db()
        self.assertEqual(first['restored'], 1)
        self.assertEqual(second['restored'], 0)
        self.assertEqual(owned.assignee_email, NOVA)
        self.assertEqual(
            (owned.metadata or {})['auto_failover_restore']['state'],
            'restored',
        )
        self.assertNotIn('pulse:agent_failback_pending:nova@shizuha.com', fake.values)
        self.assertEqual(
            Comment.objects.filter(
                item=owned,
                author_email='system@pulse',
                content__startswith='Auto-failback restored',
            ).count(),
            1,
        )

    def test_failback_does_not_yank_work_after_temporary_owner_comments(self):
        """A target comment adopts the failover; later recovery cannot steal it."""
        from tasks.models import Comment, Item

        fake = _FakeRedis({
            WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
            WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                NOVA: 'offline',
            }),
        })
        owned = Item.objects.create(
            title='temporary owner adoption wins over stale failback',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.agent_liveness._redis_client', return_value=fake):
            self._run_sweep(reason='offline', recent_heartbeat=False)
        owned.refresh_from_db()
        self.assertEqual(owned.assignee_email, SORA)
        Comment.objects.create(
            item=owned,
            author_id=702,
            author_email=SORA,
            content='I started the reassigned work.',
        )
        fake.set('pulse:agent_hb:nova@shizuha.com', repr(time.time()))

        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.agent_liveness._redis_client', return_value=fake), \
             mock.patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 side_effect=lambda e: (e or '').lower() == SORA,
             ):
            result = restore_false_positive_failovers_for_agent.run(NOVA)

        owned.refresh_from_db()
        self.assertEqual(result['restored'], 0)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertNotIn('pulse:agent_failback_pending:nova@shizuha.com', fake.values)

    def test_failback_receipt_rejects_later_assignment_even_back_to_target(self):
        """An intervening owner write wins even if the row resembles the receipt."""
        from tasks.models import Item, ItemActivity

        fake = _FakeRedis({
            WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
            WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                NOVA: 'offline',
            }),
        })
        owned = Item.objects.create(
            title='later assignment boundary supersedes automatic failback',
            mode='task', status='open', priority='urgent',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.agent_liveness._redis_client', return_value=fake):
            self._run_sweep(reason='offline', recent_heartbeat=False)
        owned.refresh_from_db()
        self.assertEqual(owned.assignee_email, SORA)

        # A later writer may explicitly reassign away and back before failback
        # observes the row.  The immutable audit boundary, not final-row equality,
        # makes that later ownership decision authoritative.
        ItemActivity.objects.create(
            item=owned,
            action='assigned',
            field_name='Assignee',
            old_value='701',
            new_value='702',
            user_id=999,
            user_email='admin@shizuha.com',
        )
        fake.set('pulse:agent_hb:nova@shizuha.com', repr(time.time()))

        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.agent_liveness._redis_client', return_value=fake), \
             mock.patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 side_effect=lambda e: (e or '').lower() == SORA,
             ):
            result = restore_false_positive_failovers_for_agent.run(NOVA)

        owned.refresh_from_db()
        self.assertEqual(result['restored'], 0)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertNotIn('pulse:agent_failback_pending:nova@shizuha.com', fake.values)

    def test_durable_self_unavailable_drains_immediately_and_scoped(self):
        """Confirmed bridge exhaustion is hard-off even with a fresh heartbeat.

        Production order: bridge writes AgentAvailability(active=False) ->
        post-commit scoped rebalance -> Redis still says provider_down and the
        authenticated request heartbeat is fresh.  Only the declared owner's
        work moves; unrelated group-queued work is left for its normal pull path.
        """
        from tasks.models import Item

        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='claude-token-pool-exhausted',
            updated_by=f'self:{NOVA}',
        )
        # Simulate the event task landing on a worker whose 5s disabled cache
        # predates the just-committed durable row.  The scoped drain must read
        # the definitive row and not wait for that cache to expire.
        WorkflowService._disabled_emails_cache = (time.time() + 60, set())
        WorkflowService._db_avail_cache = None
        owned = Item.objects.create(
            title='confirmed exhausted owner must drain now',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )
        unrelated_queue = Item.objects.create(
            title='unrelated group queue stays on its normal pull path',
            mode='task', status='open', priority='normal',
            project=self.project,
            assignment_group='engineering', workflow_name='autonomous-dev',
        )

        try:
            result = self._run_sweep(
                reason='provider_down',
                recent_heartbeat=True,
                only_agent_email=NOVA,
            )
        finally:
            WorkflowService._disabled_emails_cache = None
            WorkflowService._db_avail_cache = None

        owned.refresh_from_db()
        unrelated_queue.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(owned.assignee_email, SORA)
        self.assertIsNone(unrelated_queue.assignee_email)

    def test_only_activity_newer_than_self_unavailable_row_proves_recovery(self):
        """Causal ordering prevents both false protection and stale stranding.

        Authentication records the heartbeat before the self-availability view
        commits active=false.  That same request must not cancel its own shutdown;
        the next successful authenticated request is strictly newer and protects
        the recovered owner from a stale soft/provider verdict.
        """
        from tasks.models import Item

        row = AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='provider unavailable',
            updated_by=f'self:{NOVA}',
        )
        fake = _FakeRedis({
            WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
            WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                NOVA: 'provider_down',
            }),
            'pulse:agent_hb:nova@shizuha.com': repr(
                row.updated_at.timestamp() - 1,
            ),
        })
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake):
            self.assertFalse(
                _soft_unavailable_owner_protected(NOVA),
                'the pre-row heartbeat from active=false itself cannot cancel shutdown',
            )
            fake.set(
                'pulse:agent_hb:nova@shizuha.com',
                repr(row.updated_at.timestamp() + 1),
            )
            with mock.patch('time.time', return_value=row.updated_at.timestamp() + 2):
                self.assertTrue(
                    _soft_unavailable_owner_protected(NOVA),
                    'a strictly newer successful request proves recovery',
                )
                owned = Item.objects.create(
                    title='replay: active reviewer survives stale inactive row',
                    mode='task', status='open', priority='urgent',
                    project=self.project,
                    assignment_group='engineering',
                    assignee_id=701,
                    assignee_email=NOVA,
                    workflow_name='autonomous-dev',
                )
                replay = self._run_sweep(
                    reason='provider_down',
                    recent_heartbeat=True,
                )

        owned.refresh_from_db()
        self.assertEqual(replay['moves'], 0)
        self.assertEqual(owned.assignee_email, NOVA)


    def test_hard_down_owner_does_not_drain_blocked_work(self):
        from tasks.models import Comment, Item

        blocked = create_blocked_item(
            title='blocked operator-held work must not fail over',
            mode='task', status='blocked', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
            metadata={'blocked_via': 'admin_ops_escalation'},
        )
        satisfy_blocked_invariant(blocked)
        ready = Item.objects.create(
            title='ready work should still drain from hard-down owner',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='disabled', recent_heartbeat=True)

        blocked.refresh_from_db()
        ready.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(blocked.assignee_email, NOVA)
        self.assertEqual(ready.assignee_email, SORA)
        self.assertEqual(
            Comment.objects.filter(item=blocked, author_email='system@pulse', content__startswith='Auto-failover').count(),
            0,
        )

    def test_linked_blocked_item_clears_dead_owner_without_failover_pls524(self):
        """PLS-524: a `blocked` item with an ACTIVE LINKED blocker (an ordinary
        is_blocked_by chain, NOT admin_ops_escalation) owned by a hard-down
        member must not fail over to another concrete seat. Before the fix only the admin-ops carve-out
        was skipped, so linked-blocked recurring work (TRD-1 / BKSH-3) thrashed
        owners and emitted Auto-failover / Task Assigned notifications every
        sweep for work nobody could advance. Drive the REAL sweep across two
        consecutive cycles and assert the dead owner is cleared once, with zero failover; ready work from the same
        down owner still drains, proving the sweep actually ran."""
        from tasks.models import Comment, Item

        blocked = create_blocked_item(
            title='linked-blocked recurring work must not thrash on failover',
            mode='task', priority='high',
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
            project=self.project,
        )  # NOTE: no blocked_via metadata — a plain linked-blocker chain
        ready = Item.objects.create(
            title='ready work still drains from hard-down owner',
            mode='task', status='open', priority='high',
            assignment_group='engineering', assignee_id=701, assignee_email=NOVA,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        for _ in range(2):
            self._run_sweep(reason='disabled', recent_heartbeat=True)

        blocked.refresh_from_db()
        ready.refresh_from_db()
        self.assertIsNone(
            blocked.assignee_email,
            'linked-blocked item must release its dead concrete owner',
        )
        self.assertIsNone(blocked.assignee_id)
        self.assertEqual(blocked.assignment_group, 'engineering')
        self.assertEqual(
            Comment.objects.filter(
                item=blocked, author_email='system@pulse',
                content__startswith='Auto-failover').count(),
            0, 'no auto-failover on a linked-blocked item across two cycles')
        self.assertEqual(ready.assignee_email, SORA,
                         'ready work still drains (proves the sweep ran)')

    def test_linked_blocked_sole_member_clears_before_no_peer_exit_pls524(self):
        """The no-live-peer exit cannot pin a sole hard-down owner.

        Production order is the real top-level sweep twice.  The first tick
        releases the dead concrete owner without a target; the second is a
        no-op and emits no duplicate repair comment.
        """
        from tasks.models import Comment, TeamRoster

        roster = TeamRoster.objects.get(
            organization_id=1,
            team_name='engineering',
        )
        roster.tiers = [[NOVA]]
        roster.save(update_fields=['tiers', 'updated_at'])
        WorkflowService._roster_cache = None
        blocked = create_blocked_item(
            title='sole hard-down member must release blocked ownership',
            mode='task', priority='high',
            assignment_group='engineering', assignee_id=701,
            assignee_email=NOVA, workflow_name='autonomous-dev',
            project=self.project,
        )

        results = [
            self._run_sweep(reason='disabled', recent_heartbeat=True)
            for _ in range(2)
        ]

        blocked.refresh_from_db()
        self.assertIsNone(blocked.assignee_email)
        self.assertIsNone(blocked.assignee_id)
        self.assertEqual(blocked.assignment_group, 'engineering')
        self.assertEqual(results[0]['moves'], 1)
        self.assertEqual(results[1]['moves'], 0)
        self.assertEqual(
            Comment.objects.filter(
                item=blocked,
                content__contains='Cleared unavailable concrete owner',
            ).count(),
            1,
        )

    def test_linked_blocked_clears_before_all_peers_ineligible_exit_pls524(self):
        """Target eligibility cannot gate a no-target blocked-owner release."""
        from tasks.models import Comment

        blocked = create_blocked_item(
            title='blocked owner release precedes ineligible peer resolution',
            mode='task', priority='high',
            assignment_group='engineering', assignee_id=701,
            assignee_email=NOVA, workflow_name='autonomous-dev',
            project=self.project,
        )
        original_verdict = WorkflowService._seat_routability_verdict
        blocked_candidate_checks = []

        def deny_blocked_candidates(item, team_name, email, **kwargs):
            if item.pk == blocked.pk:
                blocked_candidate_checks.append(email)
                return WorkflowService._RoutabilityVerdict(
                    ok=False,
                    reasons=('seat_not_team_member',),
                )
            return original_verdict(item, team_name, email, **kwargs)

        with mock.patch.object(
            WorkflowService,
            '_seat_routability_verdict',
            side_effect=deny_blocked_candidates,
        ):
            results = [
                self._run_sweep(reason='disabled', recent_heartbeat=True)
                for _ in range(2)
            ]

        blocked.refresh_from_db()
        self.assertIsNone(blocked.assignee_email)
        self.assertIsNone(blocked.assignee_id)
        self.assertEqual(results[0]['moves'], 1)
        self.assertEqual(results[1]['moves'], 0)
        self.assertEqual(
            blocked_candidate_checks,
            [],
            'linked-blocked clear must happen before resolving any peer',
        )
        self.assertEqual(
            Comment.objects.filter(
                item=blocked,
                content__contains='Cleared unavailable concrete owner',
            ).count(),
            1,
        )

    def test_alias_owner_no_reoscillation_through_real_sweep_pls524(self):
        """PLS-524: an item owned by the `@agents.shizuha.io` ALIAS of a
        rostered, AVAILABLE member must be stable through the real top-level
        reconcile→failover sweep sequence. Pre-fix, the roster reconciliation
        read the alias as a non-member (roster stores the `@shizuha.com` form)
        and re-leased it every pass while availability failover bounced it — the
        goro/shizuha oscillation. Run two consecutive real sweeps and assert
        zero identity-equivalent re-lease (PLS-408 comment) and zero failover."""
        from tasks.models import Comment, Item

        alias = 'sora@agents.shizuha.io'  # identity-equivalent to rostered SORA
        item = Item.objects.create(
            title='alias-owned live item must not re-oscillate',
            mode='task', status='open', priority='high',
            assignment_group='engineering', assignee_id=702, assignee_email=alias,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        for _ in range(2):
            self._run_sweep(reason='disabled', recent_heartbeat=True)

        item.refresh_from_db()
        self.assertTrue(
            WorkflowService._emails_same_agent_identity(item.assignee_email, SORA),
            f'alias owner churned off its canonical identity to {item.assignee_email}')
        self.assertEqual(
            Comment.objects.filter(
                item=item, author_email='system@pulse',
                content__contains='PLS-408').count(),
            0, 'a rostered member alias must not be roster-reconciled/re-leased')
        self.assertEqual(
            Comment.objects.filter(
                item=item, author_email='system@pulse',
                content__startswith='Auto-failover').count(),
            0, 'an available member alias must not fail over')

    def test_soft_unavailable_member_is_still_excluded_as_new_target(self):
        from tasks.models import Item

        queued = Item.objects.create(
            title='new group work must avoid provider-down nova',
            mode='task', status='open', priority='high',
            project=self.project,
            assignment_group='engineering', workflow_name='autonomous-dev',
        )

        result = self._run_sweep(reason='provider_down', recent_heartbeat=True)

        queued.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(queued.assignee_email, SORA)
        self.assertEqual(queued.assignee_id, 702)


class SoftUnavailableReasonPublishingTests(SimpleTestCase):
    def test_availability_sweep_publishes_soft_and_hard_reason_sidecar(self):
        metrics = '\n'.join([
            'shizuha_agent_process_up{agent="nova",backend="claude"} 1',
            'shizuha_agent_provider_healthy{agent="nova",backend="claude"} 0',
            'shizuha_agent_process_up{agent="sora",backend="claude"} 1',
            'shizuha_agent_capacity_unavailable{agent="sora",backend="claude"} 1',
            'shizuha_agent_process_up{agent="kei",backend="claude"} 1',
            'shizuha_agent_enabled{agent="kei",backend="claude"} 0',
        ]) + '\n'
        fake = _FakeRedis()
        resp = mock.MagicMock()
        resp.read.return_value = metrics.encode('utf-8')
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        _rget = mock.MagicMock()
        _rget.text = metrics
        _rget.status_code = 200
        _rget.raise_for_status.return_value = None

        with mock.patch('urllib.request.urlopen', return_value=cm), \
             mock.patch('requests.get', return_value=_rget), \
             mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch.dict('os.environ', {
                 'PULSE_HEALTH_EXPORTER_URL': 'http://exporter.test/metrics',
                 'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
                 'PULSE_PROVIDER_DOWN_COOLDOWN_SECONDS': '0',
                 # Instant demotion for this unit test; production defaults gate
                 # provider/runtime bad samples for a few minutes.
                 'PULSE_HEALTH_DEMOTE_AFTER_SEC': '0',
                 'PULSE_HEALTH_RECOVER_AFTER_SEC': '0',
             }):
            result = sweep_agent_availability.run()

        self.assertTrue(result['ok'])
        availability = json.loads(fake.writes['pulse:agent_availability'])
        reasons = json.loads(fake.writes['pulse:agent_unavailability_reason'])
        self.assertFalse(availability[NOVA])
        self.assertFalse(availability[SORA])
        self.assertFalse(availability['kei@shizuha.com'])
        self.assertEqual(reasons[NOVA], 'provider_down')
        self.assertEqual(reasons[SORA], 'capacity_limited')
        self.assertEqual(reasons['kei@shizuha.com'], 'disabled')

    def test_unavailable_since_bounds_the_soft_protection_window(self):
        from tasks.tasks import _agent_unavailable_seconds, _soft_protection_window_exhausted

        now = time.time()
        fake = _FakeRedis({
            'pulse:agent_unavailable_since': json.dumps({NOVA: now - 4200}),
        })
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch.dict('os.environ', {'PULSE_SOFT_UNAVAILABLE_PROTECT_MAX_SEC': '900'}):
            self.assertAlmostEqual(_agent_unavailable_seconds(NOVA, now_ts=now), 4200, delta=2)
            self.assertTrue(_soft_protection_window_exhausted(NOVA, now_ts=now))
            # A short blip is still protected.
            self.assertFalse(_soft_protection_window_exhausted(SORA, now_ts=now))

        # No stamp at all => keep the pre-existing protection, never broaden a drain.
        empty = _FakeRedis({'pulse:agent_unavailable_since': json.dumps({})})
        with mock.patch.object(WorkflowService, '_redis_client', return_value=empty):
            self.assertIsNone(_agent_unavailable_seconds(NOVA, now_ts=now))
            self.assertFalse(_soft_protection_window_exhausted(NOVA, now_ts=now))

    def test_availability_sweep_carries_unavailable_since_across_restarts(self):
        """The stamp must survive the agent's process restarting, and clear on recovery."""
        old_stamp = time.time() - 3600
        metrics = '\n'.join([
            'shizuha_agent_process_up{agent="nova",backend="claude"} 1',
            'shizuha_agent_provider_healthy{agent="nova",backend="claude"} 0',
            'shizuha_agent_process_up{agent="sora",backend="claude"} 1',
            'shizuha_agent_provider_healthy{agent="sora",backend="claude"} 1',
        ]) + '\n'
        fake = _FakeRedis({'pulse:agent_unavailable_since': json.dumps({NOVA: old_stamp, SORA: old_stamp})})
        resp = mock.MagicMock()
        resp.read.return_value = metrics.encode('utf-8')
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        _rget = mock.MagicMock()
        _rget.text = metrics
        _rget.status_code = 200
        _rget.raise_for_status.return_value = None

        with mock.patch('urllib.request.urlopen', return_value=cm), \
             mock.patch('requests.get', return_value=_rget), \
             mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch.dict('os.environ', {
                 'PULSE_HEALTH_EXPORTER_URL': 'http://exporter.test/metrics',
                 'PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED': '0',
                 'PULSE_PROVIDER_DOWN_COOLDOWN_SECONDS': '0',
                 'PULSE_HEALTH_DEMOTE_AFTER_SEC': '0',
                 'PULSE_HEALTH_RECOVER_AFTER_SEC': '0',
             }):
            sweep_agent_availability.run()

        since = json.loads(fake.writes['pulse:agent_unavailable_since'])
        # Still down -> original stamp preserved (a restart must not reset the clock).
        self.assertAlmostEqual(float(since[NOVA]), old_stamp, delta=2)
        # Recovered -> stamp cleared.
        self.assertNotIn(SORA, since)

    def test_reason_and_heartbeat_helpers_alias_fold(self):
        now = int(time.time())
        fake = _FakeRedis({
            'pulse:agent_unavailability_reason': json.dumps({NOVA: 'provider_down'}),
            'pulse:agent_hb:nova@agents.shizuha.io': str(now),
        })
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake):
            self.assertEqual(_agent_unavailability_reason('nova@agents.shizuha.io'), 'provider_down')
            self.assertTrue(_agent_has_recent_pulse_heartbeat(NOVA, now_ts=now + 1, grace_seconds=20))
            self.assertFalse(_agent_has_recent_pulse_heartbeat(NOVA, now_ts=now + 30, grace_seconds=20))

    def test_successful_recovery_clears_only_provider_down_identity_hold(self):
        alias = 'nova@agents.shizuha.io'
        fake = _FakeRedis({
            WorkflowService._PROVIDER_DOWN_COOLDOWN_KEY: json.dumps({
                NOVA: time.time() + 600,
                SORA: time.time() + 600,
            }),
            WorkflowService._AGENT_AVAILABILITY_KEY: json.dumps({
                NOVA: False,
                alias: False,
                SORA: False,
            }),
            WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                NOVA: 'provider_down',
                alias: 'provider_down',
                SORA: 'provider_down',
            }),
            WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
        })

        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake):
            recovered = WorkflowService.mark_agent_provider_recovered(NOVA)

        self.assertTrue(recovered)
        cooldown = json.loads(
            fake.values[WorkflowService._PROVIDER_DOWN_COOLDOWN_KEY]
        )
        availability = json.loads(
            fake.values[WorkflowService._AGENT_AVAILABILITY_KEY]
        )
        reasons = json.loads(
            fake.values[WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY]
        )
        self.assertNotIn(NOVA, cooldown)
        self.assertNotIn(alias, cooldown)
        self.assertIn(SORA, cooldown)
        self.assertTrue(availability[NOVA])
        self.assertTrue(availability[alias])
        self.assertFalse(availability[SORA])
        self.assertNotIn(NOVA, reasons)
        self.assertNotIn(alias, reasons)
        self.assertEqual(reasons[SORA], 'provider_down')

    def test_successful_report_cannot_clear_disabled_or_non_provider_state(self):
        for reason, disabled in (
            ('disabled', []),
            ('offline', []),
            ('provider_down', [NOVA]),
        ):
            with self.subTest(reason=reason, disabled=disabled):
                fake = _FakeRedis({
                    WorkflowService._PROVIDER_DOWN_COOLDOWN_KEY: json.dumps({
                        NOVA: time.time() + 600,
                    }),
                    WorkflowService._AGENT_AVAILABILITY_KEY: json.dumps({
                        NOVA: False,
                    }),
                    WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({
                        NOVA: reason,
                    }),
                    WorkflowService._AGENT_DISABLED_KEY: json.dumps(disabled),
                })

                with mock.patch.object(
                    WorkflowService,
                    '_redis_client',
                    return_value=fake,
                ):
                    recovered = WorkflowService.mark_agent_provider_recovered(NOVA)

                self.assertFalse(recovered)
                self.assertFalse(json.loads(
                    fake.values[WorkflowService._AGENT_AVAILABILITY_KEY]
                )[NOVA])


class AgentSelfAvailabilityFailoverEventTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def tearDown(self):
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None

    def _patch_availability(self, payload):
        request = self.factory.patch(
            '/api/agent-availability/self/',
            payload,
            format='json',
        )
        force_authenticate(
            request,
            user=SimpleNamespace(email=NOVA, is_authenticated=True),
        )
        return AgentSelfAvailabilityView.as_view()(request)

    def test_active_to_inactive_dispatches_scoped_rebalance_after_commit(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=True,
            status='active',
            updated_by=f'self:{NOVA}',
        )

        with mock.patch('tasks.tasks.sweep_rebalance_pods.apply_async') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._patch_availability({
                    'active': False,
                    'reason': 'claude-token-pool-exhausted',
                })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentAvailability.objects.get(email=NOVA).active)
        dispatch.assert_called_once_with(kwargs={'only_agent_email': NOVA})

    def test_repeated_inactive_report_does_not_dispatch_duplicate_reconcile(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='claude-token-pool-exhausted',
            updated_by=f'self:{NOVA}',
        )

        with mock.patch('tasks.tasks.sweep_rebalance_pods.apply_async') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._patch_availability({
                    'active': False,
                    'reason': 'claude-token-pool-exhausted',
                })

        self.assertEqual(response.status_code, 200)
        dispatch.assert_not_called()

    def test_inactive_to_active_dispatches_scoped_group_queue_drain_after_commit(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=False,
            status='inactive',
            reason='provider unavailable',
            updated_by=f'self:{NOVA}',
        )

        with mock.patch.object(
            WorkflowService,
            'mark_agent_provider_recovered',
            return_value=False,
        ) as recover, mock.patch(
            'tasks.tasks.refresh_agent_health_and_assign_group_queue.apply_async',
        ) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._patch_availability({
                    'active': True,
                    'reason': 'worked Pulse heartbeat',
                })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AgentAvailability.objects.get(email=NOVA).active)
        recover.assert_called_once_with(NOVA)
        dispatch.assert_called_once_with(kwargs={'only_agent_email': NOVA})

    def test_repeated_active_report_dispatches_idempotent_scoped_queue_drain(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=True,
            status='active',
            updated_by=f'self:{NOVA}',
        )

        with mock.patch.object(
            WorkflowService,
            'mark_agent_provider_recovered',
            return_value=False,
        ) as recover, mock.patch(
            'tasks.tasks.refresh_agent_health_and_assign_group_queue.apply_async',
        ) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._patch_availability({
                    'active': True,
                    'reason': 'periodic heartbeat',
                })

        self.assertEqual(response.status_code, 200)
        recover.assert_called_once_with(NOVA)
        dispatch.assert_called_once_with(kwargs={'only_agent_email': NOVA})

    def test_repeated_active_report_drains_when_provider_hold_was_cleared(self):
        AgentAvailability.objects.create(
            email=NOVA,
            active=True,
            status='active',
            updated_by=f'self:{NOVA}',
        )

        with mock.patch.object(
            WorkflowService,
            'mark_agent_provider_recovered',
            return_value=True,
        ) as recover, mock.patch(
            'tasks.tasks.refresh_agent_health_and_assign_group_queue.apply_async',
        ) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                response = self._patch_availability({
                    'active': True,
                    'reason': 'completed model work',
                })

        self.assertEqual(response.status_code, 200)
        recover.assert_called_once_with(NOVA)
        dispatch.assert_called_once_with(kwargs={'only_agent_email': NOVA})

    @mock.patch('tasks.tasks.reconcile_group_owned_external_waits.run')
    @mock.patch('tasks.tasks.sweep_assign_group_queued.run')
    @mock.patch('tasks.tasks.restore_false_positive_failovers_for_agent.run')
    @mock.patch('tasks.tasks.sweep_agent_availability.run')
    def test_recovery_refreshes_health_before_scoped_queue_drain(
        self,
        refresh_health,
        failback,
        drain_queue,
        reconcile_external_waits,
    ):
        events = []
        refresh_health.side_effect = lambda: (
            events.append('refresh-health') or {'ok': True}
        )
        failback.side_effect = lambda _email: (
            events.append('failback') or {'restored': 1}
        )
        drain_queue.side_effect = lambda **_kwargs: (
            events.append('drain-queue') or {'assigned': 1}
        )
        reconcile_external_waits.side_effect = lambda **_kwargs: (
            events.append('reconcile-external-waits') or {'assigned': 1}
        )

        result = refresh_agent_health_and_assign_group_queue.run(NOVA)

        # main: failback runs between health refresh and drain; PLS-837 also
        # reconciles group-owned external waits after the drain.
        self.assertEqual(
            events,
            ['refresh-health', 'failback', 'drain-queue', 'reconcile-external-waits'],
        )
        refresh_health.assert_called_once_with()
        failback.assert_called_once_with(NOVA)
        drain_queue.assert_called_once_with(only_agent_email=NOVA)
        reconcile_external_waits.assert_called_once_with(only_agent_email=NOVA)
        self.assertEqual(result['availability'], {'ok': True})
        self.assertEqual(result['failback'], {'restored': 1})
        self.assertEqual(result['assignment'], {'assigned': 1})
        self.assertEqual(result['external_wait_assignment'], {'assigned': 1})


class AgentFailbackMiddlewareTests(SimpleTestCase):
    """The real successful-request boundary emits one throttled recovery event."""

    def _request(self, path='/shizuha-pulse/api/items/', status=200):
        return (
            SimpleNamespace(
                path=path,
                user=SimpleNamespace(
                    email=NOVA,
                    is_authenticated=True,
                ),
            ),
            SimpleNamespace(status_code=status),
        )

    def test_successful_authenticated_request_dispatches_pending_failback_once(self):
        from tasks import agent_liveness
        from tasks.middleware import RequestLocalMiddleware

        fake = _FakeRedis({
            'pulse:agent_failback_pending:nova@shizuha.com': '1',
        })
        request, response = self._request()
        with mock.patch.object(agent_liveness, '_redis_client', return_value=fake), \
             mock.patch(
                 'tasks.tasks.restore_false_positive_failovers_for_agent.apply_async',
             ) as dispatch:
            RequestLocalMiddleware._schedule_agent_failback(request, response)
            RequestLocalMiddleware._schedule_agent_failback(request, response)

        dispatch.assert_called_once_with(
            kwargs={'only_agent_email': NOVA},
            countdown=2,
        )

    def test_failed_request_and_self_shutdown_endpoint_do_not_dispatch(self):
        from tasks import agent_liveness
        from tasks.middleware import RequestLocalMiddleware

        for path, status in (
            ('/shizuha-pulse/api/items/', 500),
            ('/shizuha-pulse/api/agent-availability/self/', 200),
        ):
            with self.subTest(path=path, status=status):
                fake = _FakeRedis({
                    'pulse:agent_failback_pending:nova@shizuha.com': '1',
                })
                request, response = self._request(path=path, status=status)
                with mock.patch.object(
                    agent_liveness,
                    '_redis_client',
                    return_value=fake,
                ), mock.patch(
                    'tasks.tasks.restore_false_positive_failovers_for_agent.apply_async',
                ) as dispatch:
                    RequestLocalMiddleware._schedule_agent_failback(
                        request, response,
                    )
                dispatch.assert_not_called()
