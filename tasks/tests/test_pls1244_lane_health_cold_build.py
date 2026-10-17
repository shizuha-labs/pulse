"""PLS-1244: lane-health cold-snapshot build must never block the request path.

PLS-1187 fixed the warm path (stale-while-revalidate snapshot cache) but left
the cold regime unbounded: with no fresh AND no stale snapshot (post-deploy,
post-flush, or after ``_invalidate_all_lane_snapshots``), ``lane_health()``
built synchronously in the request path — measured ~90-120s across 23 lanes in
production (PLS-1242), so every client/probe with a <=60s timeout saw a dead
endpoint. The cold path now spawns the background build and serves a bounded
``build_status='building'`` placeholder (the view escalates to 503 +
Retry-After). These tests pin that contract; the blocked-builder fixture
DETERMINISTICALLY fails on the old synchronous-build code (the request would
never return while the builder is blocked).
"""
import threading
import time
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tasks.models import TeamRoster
from tasks.services import lane_health as lh
from tasks.services import workflow_service as ws
from tasks.services.lane_health import (
    _LANE_SNAPSHOT_FRESH_KEY,
    _LANE_SNAPSHOT_REFRESH_LOCK_KEY,
    all_lanes_health,
    lane_health,
)

ROSTER = {
    'engineering': [['nagi@shizuha.com'], ['aoi@shizuha.com']],
    'review': [['revi@shizuha.com']],
}

LANES = ('engineering', 'review')


class LaneHealthColdBuildTests(TestCase):
    """Opted-in cache behaviour (hermetic settings default the cache OFF)."""

    def _seed_lanes(self):
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

    def _blocked_builder(self):
        """Builder fixture that blocks until released — the old synchronous
        cold build would hang the request path on this forever."""
        real = lh._lane_health_uncached
        release = threading.Event()
        entered = threading.Event()
        calls = []

        def blocking(team_name, *, org_slug='shizuha'):
            calls.append(team_name)
            if release.is_set():
                return real(team_name, org_slug=org_slug)
            entered.set()
            # Block until the test releases. If the request path ever calls
            # this synchronously (old cold-build code), the request never
            # returns and the elapsed assertion below fails.
            release.wait(timeout=15)
            return real(team_name, org_slug=org_slug)

        return (
            mock.patch.object(lh, '_lane_health_uncached', side_effect=blocking),
            release, entered, calls,
        )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_cold_request_returns_placeholder_without_blocking_on_builder(self):
        """THE hang regression: a cold lane must not build in the request path.

        The builder blocks on an unreleased event. Old code built
        synchronously -> the request never returned (test fails/hangs). New
        code must return a building placeholder immediately while the builder
        is still blocked, then serve the real snapshot once the background
        build publishes.
        """
        self._seed_lanes()
        patcher, release, entered, calls = self._blocked_builder()
        with self._roster_mocks(), patcher:
            started = time.monotonic()
            snapshot = lane_health('engineering')
            elapsed = time.monotonic() - started

            self.assertLess(
                elapsed, 2.0,
                'cold request must return promptly, not block on the build',
            )
            self.assertEqual(snapshot['team'], 'engineering')
            self.assertEqual(snapshot['build_status'], 'building')
            self.assertTrue(snapshot['degraded'])
            self.assertGreater(snapshot['retry_after_seconds'], 0)
            self.assertEqual(snapshot['seats'], [])
            self.assertEqual(
                snapshot['stall'],
                {'is_stalled': False, 'reason': 'snapshot_building'},
                'a building placeholder must never carry a real stall verdict',
            )
            # The builder may already be running in the background thread, but
            # the REQUEST PATH returned without waiting for it.
            self.assertIsNotNone(
                cache.get(_LANE_SNAPSHOT_REFRESH_LOCK_KEY.format(team='engineering')),
                'cold serve must hold the refresh mutex while building',
            )

            # Wait for the background builder to reach the blocked fixture,
            # then release it; it must publish the real snapshot.
            self.assertTrue(
                entered.wait(timeout=5),
                'background build must have been scheduled and started',
            )
            release.set()
            deadline = time.time() + 10
            fresh = None
            while time.time() < deadline:
                fresh = cache.get(
                    _LANE_SNAPSHOT_FRESH_KEY.format(team='engineering'))
                if fresh is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(
                fresh, 'background build must publish the fresh snapshot')
            self.assertNotIn('build_status', fresh)
            self.assertGreaterEqual(len(fresh['seats']), 2)

            # Next read serves the real snapshot, not the placeholder.
            served = lane_health('engineering')
            self.assertNotIn('build_status', served)
            self.assertGreaterEqual(len(served['seats']), 2)

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_cold_all_lanes_marks_every_lane_building_without_rebuilds(self):
        """Cold all-lanes aggregation: placeholders only, zero request-path
        rebuilds (deterministic — spawner mocked, no threads)."""
        self._seed_lanes()
        calls = []
        patcher = mock.patch.object(
            lh, '_lane_health_uncached',
            side_effect=lambda team_name, **kw: calls.append(team_name),
        )
        spawned = []
        with self._roster_mocks(), patcher, \
                mock.patch.object(
                    lh, '_spawn_lane_snapshot_refresh',
                    side_effect=lambda team, **kw: spawned.append(team) or True,
                ):
            lanes = all_lanes_health()
        self.assertTrue(set(LANES) <= {l['team'] for l in lanes})
        for lane in lanes:
            self.assertEqual(
                lane.get('build_status'), 'building',
                'cold lane must serve a building placeholder',
            )
        self.assertEqual(
            calls, [],
            'cold all-lanes request must perform zero in-path rebuilds',
        )
        # Set-based (the DB may legitimately contain migration-seeded default
        # rosters beyond the seeds): every seeded lane must have its build
        # scheduled.
        self.assertTrue(
            set(LANES) <= set(spawned),
            'every cold lane must have its background build scheduled',
        )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_cold_single_lane_endpoint_returns_503_with_retry_after(self):
        """View contract: cold window fast-fails with 503 + Retry-After."""
        self._seed_lanes()
        patcher, release, _entered, _calls = self._blocked_builder()
        user = self._make_user('cold@shizuha.com')
        client = APIClient()
        client.force_authenticate(user=user)
        with self._roster_mocks(), patcher:
            resp = client.get('/shizuha-pulse/api/lane-health/?team=engineering')
            self.assertEqual(
                resp.status_code, 503,
                'cold lane must fast-fail with 503, not hang',
            )
            self.assertIn('Retry-After', resp)
            self.assertGreater(int(resp['Retry-After']), 0)
            self.assertEqual(resp.json()['build_status'], 'building')
            release.set()

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_cold_all_lanes_endpoint_returns_503_with_retry_after(self):
        self._seed_lanes()
        user = self._make_user('coldall@shizuha.com')
        client = APIClient()
        client.force_authenticate(user=user)
        with self._roster_mocks(), \
                mock.patch.object(
                    lh, '_spawn_lane_snapshot_refresh',
                    side_effect=lambda team, **kw: True,
                ):
            resp = client.get('/shizuha-pulse/api/lane-health/')
            self.assertEqual(resp.status_code, 503)
            self.assertIn('Retry-After', resp)
            body = resp.json()
            self.assertTrue(
                any(l.get('build_status') == 'building'
                    for l in body['lanes']),
            )

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_warm_endpoint_returns_200(self):
        """Warm path is untouched: real snapshot, 200, no Retry-After."""
        self._seed_lanes()
        user = self._make_user('warm@shizuha.com')
        client = APIClient()
        client.force_authenticate(user=user)
        with self._roster_mocks():
            lh._build_and_store_lane_snapshot('engineering', org_slug='shizuha')
            resp = client.get('/shizuha-pulse/api/lane-health/?team=engineering')
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn('Retry-After', resp)
            self.assertNotIn('build_status', resp.json())

    @override_settings(PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED=True)
    def test_degraded_cache_reports_refresh_unschedulable(self):
        """If the cache backend is too degraded to schedule the background
        refresh (add raises), the spawner must report False so the cold path
        fails open to the legacy synchronous build instead of serving
        placeholders forever."""
        self._seed_lanes()
        with mock.patch.object(lh.cache, 'add',
                               side_effect=ConnectionError('degraded')):
            self.assertFalse(
                lh._spawn_lane_snapshot_refresh('engineering',
                                                org_slug='shizuha'),
                'degraded cache must report the refresh as unschedulable',
            )

    @staticmethod
    def _make_user(email):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.create_user(
            username=email.split('@')[0], email=email, password='x',
        )
