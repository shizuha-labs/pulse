"""PLS-1187: lane-health snapshot cache (stale-while-revalidate) regression.

The authenticated all-lanes probe timed out in production (HTTP 000 at 15-30s
client ceilings) because ``all_lanes_health()`` serially rebuilt every active
lane's snapshot per request — measured ~3.7s per lane x ~23 lanes = ~85s on
shizuha-tasks-e9504fb with the pulse#452/#475 degraded-cache fixes already live
and the cache Redis healthy. The snapshot cache serves each lane from cache and
refreshes expired lanes out-of-band, so a warm request path performs ZERO
per-lane rebuilds — that is the SLA mechanism these tests pin.
"""
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from tasks.models import TeamRoster
from tasks.services import lane_health as lh
from tasks.services import workflow_service as ws
from tasks.services.lane_health import (
    _LANE_SNAPSHOT_FRESH_KEY,
    _LANE_SNAPSHOT_REFRESH_LOCK_KEY,
    _LANE_SNAPSHOT_STALE_KEY,
    all_lanes_health,
    lane_health,
)

ROSTER = {
    'engineering': [['nagi@shizuha.com'], ['aoi@shizuha.com']],
    'review': [['revi@shizuha.com']],
    'merge': [['kai2574@shizuha.com']],
}

LANES = ('engineering', 'review', 'merge')


class LaneHealthSnapshotCacheTests(TestCase):
    """Opted-in cache behaviour (hermetic settings default the cache OFF)."""

    def _seed_lanes(self):
        # Self-contained cache state: LocMemCache persists across tests in the
        # process, and stale keys from an earlier test would turn the warm-path
        # assertions below into cache hits. Clear first, then seed.
        cache.clear()
        for team in LANES:
            TeamRoster.objects.get_or_create(
                team_name=team, organization_slug='shizuha',
                defaults={'is_active': True, 'version': 1},
            )

    def _roster_mocks(self):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, '_pod_roster', return_value=ROSTER))
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, 'assignable_teams', return_value=ROSTER))
        stack.enter_context(mock.patch.object(
            ws.WorkflowService, '_agent_email_aliases',
            side_effect=lambda e: {str(e).strip().lower()}))
        return stack

    def _counting_builder(self):
        """Patch the live builder with a counting pass-through."""
        real = lh._lane_health_uncached
        calls = []

        def counting(team_name, *, org_slug='shizuha'):
            calls.append(team_name)
            return real(team_name, org_slug=org_slug)

        return mock.patch.object(
            lh, '_lane_health_uncached', side_effect=counting,
        ), calls

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_warm_all_lanes_request_performs_zero_rebuilds(self):
        """SLA regression: once warm, the all-lanes request rebuilds nothing.

        Old code re-ran the per-lane builder for every lane on EVERY request
        (~85s at production lane count). The snapshot cache must serve the
        request entirely from cache. (PLS-1244: the cold request no longer
        builds in the request path at all — it spawns background refreshes and
        serves building placeholders; the warm-up here uses the same
        synchronous publish path the background refresher uses.)
        """
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            # Warm EVERY lane the request will enumerate — all_lanes_health()
            # walks all active TeamRoster rows for the org, and the migrated
            # test DB carries seeded rows (security, devops, ...) beyond LANES.
            # Leaving those cold made the zero-rebuild assertion below
            # nondeterministic (a cold lane's background refresh may not have
            # landed by the second request — worker-grouping dependent).
            all_teams = set(LANES) | set(
                TeamRoster.objects.filter(
                    organization_slug='shizuha', is_active=True,
                ).values_list('team_name', flat=True)
            )
            for team in sorted(all_teams):
                lh._build_and_store_lane_snapshot(team, org_slug='shizuha')
            calls.clear()

            first = all_lanes_health()
            # The test DB may also contain migration-seeded default rosters —
            # the SLA invariant is about the REBUILD count, not the lane set.
            first_teams = {l['team'] for l in first}
            self.assertTrue(set(LANES) <= first_teams,
                            'every seeded lane must be returned')
            self.assertGreaterEqual(
                sum(len(l['seats']) for l in first), 4,
                'seat verdicts must still be produced',
            )

            calls.clear()
            second = all_lanes_health()
            self.assertEqual(
                calls, [],
                'warm request must perform zero per-lane rebuilds '
                '(this is the sub-second SLA mechanism)',
            )
            self.assertEqual(
                {l['team'] for l in second}, first_teams,
            )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_expired_lane_serves_stale_without_rebuilding_request_path(self):
        """Fresh-expiry serves the stale snapshot; the rebuild is only scheduled.

        The request path must never block on a rebuild — it serves the
        last-known snapshot and hands the refresh to the background spawner.
        The spawner is mocked here so the request-path assertion is
        deterministic (the real thread machinery is covered by
        test_background_refresh_repopulates_fresh_key_and_releases_lock).
        """
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            built = lh._build_and_store_lane_snapshot('engineering', org_slug='shizuha')
            self.assertEqual(calls, ['engineering'])

            # Simulate fresh-TTL expiry (stale horizon still holds the copy).
            cache.delete(_LANE_SNAPSHOT_FRESH_KEY.format(team='engineering'))

            spawned = []
            calls.clear()
            with mock.patch.object(
                lh, '_spawn_lane_snapshot_refresh',
                side_effect=lambda team, **kw: spawned.append((team, kw)),
            ):
                served = lane_health('engineering')
            self.assertEqual(
                served, built,
                'expired lane must serve the stale snapshot, not rebuild '
                'in the request path',
            )
            self.assertEqual(
                calls, [],
                'stale serve must not rebuild in the request path',
            )
            self.assertEqual(
                spawned, [('engineering', {'org_slug': 'shizuha'})],
                'stale serve must schedule exactly one background refresh',
            )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_background_refresh_repopulates_fresh_key_and_releases_lock(self):
        """The real background refresher rebuilds once, publishes fresh, and
        releases its mutex."""
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            lh._build_and_store_lane_snapshot('engineering', org_slug='shizuha')  # warm
            cache.delete(_LANE_SNAPSHOT_FRESH_KEY.format(team='engineering'))

            calls.clear()
            lh._spawn_lane_snapshot_refresh('engineering', org_slug='shizuha')

            import time
            deadline = time.time() + 5
            refreshed = False
            while time.time() < deadline:
                if cache.get(_LANE_SNAPSHOT_FRESH_KEY.format(team='engineering')):
                    refreshed = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                refreshed,
                'background refresher must repopulate the fresh key',
            )
            self.assertEqual(
                calls, ['engineering'],
                'background refresher must perform exactly one rebuild',
            )
            # The refresher publishes the fresh key and releases its mutex in
            # two separate cache operations; polling the fresh key can observe
            # the publish a scheduling instant before the delete lands. Wait
            # (bounded) for the release instead of asserting immediately.
            import time
            deadline = time.time() + 5
            while time.time() < deadline:
                if cache.get(_LANE_SNAPSHOT_REFRESH_LOCK_KEY.format(team='engineering')) is None:
                    break
                time.sleep(0.05)
            self.assertIsNone(
                cache.get(_LANE_SNAPSHOT_REFRESH_LOCK_KEY.format(team='engineering')),
                'refresh mutex must be released after the rebuild',
            )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_fail_open_when_cache_errors(self):
        """A degraded cache backend must never break the read path."""
        self._seed_lanes()
        patcher, _calls = self._counting_builder()
        with self._roster_mocks(), patcher, \
             mock.patch.object(lh.cache, 'get', side_effect=ConnectionError('degraded')), \
             mock.patch.object(lh.cache, 'set', side_effect=ConnectionError('degraded')), \
             mock.patch.object(lh.cache, 'add', side_effect=ConnectionError('degraded')):
            snapshot = lane_health('engineering')
        self.assertEqual(snapshot['team'], 'engineering')
        self.assertGreaterEqual(len(snapshot['seats']), 1,
                                'live build must still produce seat verdicts')

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_hold_override_write_invalidates_snapshot(self):
        """A recorded hold-override event must surface on the next rebuild.

        Contract: the write invalidates the fresh key; the immediate next read
        serves the (pre-event) stale snapshot and schedules a background
        refresh; the REBUILT snapshot surfaces the event.
        """
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            lh._build_and_store_lane_snapshot('merge', org_slug='shizuha')
            self.assertEqual(calls, ['merge'])

            # hold_override_events() rebuilds each event with only
            # lane/pr/merger/held_by/head_sha/approved_sha/at — item_key-style
            # fields are dropped by contract, so distinguish via `pr`.
            lh.record_hold_override_event('merge', {
                'pr': 'PR-499', 'note': 'hold-override probe',
            })

            # Immediate next read: stale served, refresh scheduled, no
            # in-request rebuild (spawner mocked for determinism).
            spawned = []
            calls.clear()
            with mock.patch.object(
                lh, '_spawn_lane_snapshot_refresh',
                side_effect=lambda team, **kw: spawned.append((team, kw)),
            ):
                served = lane_health('merge')
            self.assertEqual(
                calls, [],
                'invalidated lane must not rebuild in the request path',
            )
            self.assertEqual(
                spawned, [('merge', {'org_slug': 'shizuha'})],
                'invalidated lane must schedule a background refresh',
            )

            # The rebuild (background thread in production) surfaces the event.
            rebuilt = lh._build_and_store_lane_snapshot(
                'merge', org_slug='shizuha')
            hold = (rebuilt.get('stall') or {}).get('hold_override') or {}
            events = hold.get('events') or []
            self.assertTrue(
                hold.get('flagged') and events,
                'rebuilt snapshot must flag the recorded hold-override event',
            )
            self.assertTrue(
                any(e.get('pr') == 'PR-499' for e in events),
                'rebuilt snapshot must include the recorded hold-override event',
            )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_fleet_digest_write_invalidates_all_lane_snapshots(self):
        """A fleet digest change re-classes every lane — all fresh keys drop."""
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            # PLS-1244: warm every lane synchronously (the request path no
            # longer builds on cold cache), then assert the digest write drops
            # every fresh key it created.
            for row in TeamRoster.objects.filter(
                organization_slug='shizuha', is_active=True,
            ).order_by('team_name'):
                lh._build_and_store_lane_snapshot(row.team_name, org_slug='shizuha')
            # Whatever lane set the DB yields (seeds + any migration-seeded
            # defaults), the digest write must drop EVERY fresh key it created.
            built_teams = sorted(set(calls))
            self.assertGreaterEqual(len(built_teams), 1)

            lh.record_fleet_runtime_digest('sha256:deadbeef')

            keys_left = [
                team for team in built_teams
                if cache.get(_LANE_SNAPSHOT_FRESH_KEY.format(team=team))
            ]
            self.assertEqual(
                keys_left, [],
                'fleet digest change must invalidate every lane snapshot',
            )

    def test_snapshot_cache_disabled_under_hermetic_settings(self):
        """Isolation contract: the hermetic suite runs the live builder path.

        Existing lane-health/circuit tests assert on real per-request builder
        and Redis-client behaviour; the snapshot cache must stay OFF there and
        is exercised only by the override_settings tests above.
        """
        self.assertFalse(
            getattr(settings, 'PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED', True),
            'test_settings.py must disable the lane-health snapshot cache',
        )
        self._seed_lanes()
        patcher, calls = self._counting_builder()
        with self._roster_mocks(), patcher:
            all_lanes_health()
            all_lanes_health()
            n = len(calls)
            self.assertGreaterEqual(n, 2)
            self.assertEqual(
                sorted(calls[: n // 2]), sorted(calls[n // 2 :]),
                'cache disabled -> every request rebuilds the same lane set '
                '(pre-cache behaviour the existing suite relies on)',
            )
