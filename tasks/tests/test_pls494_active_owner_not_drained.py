"""PLS-494: auto-failover must not drain an actively-producing owner.

Regression for the PLAT-4678 live reproduction: ichi posted fresh authenticated
Pulse comments/mutations while advancing an in-progress incident, but the health
sweep classified him `offline` (stale bridge sample), and because the failover-drain
protection (`_soft_unavailable_owner_protected`) only covered `provider_down`/
`capacity_limited`, each rebalance re-drained him to ni. A fresh authenticated Pulse
heartbeat (set on every authenticated request) cannot come from a truly-offline
process, so `offline` + recent heartbeat is stale health, not hard-off — the owner
must keep already-owned work. Genuinely hard-off states still drain: operator
`disabled`, `wedged` (polls but zero work → heartbeat does not refute the wedge),
and any down owner with NO recent heartbeat.
"""
import os
from unittest import mock
from unittest.mock import patch

from django.test import TestCase

from tasks.services.workflow_service import WorkflowService
import tasks.tasks as t
from tasks.tasks import (
    sweep_rebalance_pods,
    _soft_unavailable_owner_protected,
    _SOFT_UNAVAILABILITY_REASONS,
)
from tasks import agent_liveness
from tasks.authentication import ShizuhaJWTAuthentication, _record_agent_heartbeat


class PLS494SoftProtectionUnitTests(TestCase):
    """Unit coverage of the exact drain-protection predicate the fix changes."""

    def _protected(self, reason, hb):
        with patch.object(t, '_agent_unavailability_reason', return_value=reason), \
             patch.object(t, '_agent_has_recent_pulse_heartbeat', return_value=hb):
            return _soft_unavailable_owner_protected('ichi@shizuha.com')

    def test_offline_with_recent_heartbeat_is_protected(self):
        # PLS-494: the exact regression — offline sample + fresh authenticated hb.
        self.assertIn('offline', _SOFT_UNAVAILABILITY_REASONS)
        self.assertTrue(self._protected('offline', hb=True),
                        "an offline-classified owner producing authenticated Pulse "
                        "activity (fresh heartbeat) must be protected from drain")

    def test_offline_without_heartbeat_still_drains(self):
        # Truly hard-off: no recent authenticated request → hb TTL-expired.
        self.assertFalse(self._protected('offline', hb=False),
                         "an offline owner with no recent heartbeat is truly hard-off "
                         "and must still drain")

    def test_provider_down_with_heartbeat_is_protected(self):
        # Existing PLS-204 behaviour is preserved.
        self.assertTrue(self._protected('provider_down', hb=True))

    def test_wedged_with_heartbeat_still_drains(self):
        # A wedged process keeps polling, so its heartbeat stays fresh; the heartbeat
        # does NOT refute the zero-work wedge signal → must still drain.
        self.assertNotIn('wedged', _SOFT_UNAVAILABILITY_REASONS)
        self.assertFalse(self._protected('wedged', hb=True))

    def test_disabled_with_heartbeat_still_drains(self):
        # Operator kill-switch: drain even while heartbeating.
        self.assertFalse(self._protected('disabled', hb=True))


# Disable the Pulse API + health-exporter gates so the sweep proceeds to the
# failover-drain logic under test (see _rebalance_pulse_api_health_gate / the
# PLAT-319 exporter gate in tasks.tasks).
_ENV = {'PULSE_API_HEALTH_URL': 'disabled', 'PULSE_HEALTH_EXPORTER_URL': 'disabled'}


def _org_scoped_drain_fixture(org_seed=590000):
    """Build a `--parallel`-safe real drain fixture: a unique agent identity, an
    ACTIVE DB TeamRoster row for its pod, and the owned in-progress incident in an
    org-scoped Project whose org == the roster's.

    The failover-drain pod loop iterates real ``TeamRoster`` rows (NOT the in-memory
    ``_pod_roster``) and scopes movable work by ``project__org_id``. A fixture that
    only patched ``_pod_roster`` and used a projectless item made the sweep no-op —
    the drain never ran and the assertions were hollow. This builds the row+project
    the production drain actually requires.
    """
    import uuid
    from tasks.models import Item, Project, TeamRoster
    tag = uuid.uuid4().hex[:10]
    owner = f'owner{tag}@shizuha.com'
    peer = f'peer{tag}@shizuha.com'
    base = 700000 + (int(tag, 16) % 200000)
    org_id = org_seed + (int(tag, 16) % 40000)
    users = {owner: (base, owner), peer: (base + 1, peer)}
    roster = {'engineering': [[owner, peer]]}
    TeamRoster.objects.create(
        team_name='engineering', organization_slug=f'torg{org_id}',
        organization_id=org_id, tiers=[[owner, peer]], is_active=True)
    project = Project.objects.create(
        org_id=org_id, name=f'Incident project {org_id}', key=f'PJ{org_id % 1000:03d}')
    task = Item.objects.create(
        title='In-progress incident (actively producing)',
        mode='task', status='in_progress', priority='urgent',
        assignment_group='engineering', project=project,
        assignee_id=base, assignee_email=owner)
    return task, owner, peer, users, roster


class PLS494ProductionOrderDrainTests(TestCase):
    """Production-order: fresh owner activity → rebalance sweep ×2 → owner stable;
    a hard-disabled owner still drains (aoi's specified regression shape).

    Uses the REAL org-scoped drain fixture (DB TeamRoster row + org-scoped project),
    so the sweep genuinely executes the failover drain instead of no-opping. The
    unavailability reason / heartbeat are still injected via the predicate mocks so
    this stays a focused production-order coverage of the drain decision itself."""

    def _run_sweeps(self, *, reason, hb, sweeps=2):
        task, owner, peer, users, roster = _org_scoped_drain_fixture()

        def fake_by_email(email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def fake_by_id(uid):
            for u, e in users.values():
                if int(uid) == u:
                    return {'id': u, 'email': e}
            return None

        class FakeUS:
            get_user_by_email = staticmethod(fake_by_email)
            get_user_by_id = staticmethod(fake_by_id)

        # The owner reads as NOT available-for-assignment (health sweep marked it
        # down); the peer is available — so if the drain fires, the task moves to
        # the peer.
        available = {peer.lower()}
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', FakeUS()), \
             patch.object(WorkflowService, '_pod_roster', return_value=roster), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in available), \
             patch.object(t, '_agent_unavailability_reason', return_value=reason), \
             patch.object(t, '_agent_has_recent_pulse_heartbeat', return_value=hb):
            for _ in range(sweeps):
                sweep_rebalance_pods.run()
        task.refresh_from_db()
        return task, owner, peer

    def test_active_offline_owner_survives_two_sweeps(self):
        # PLS-494 core: owner is offline-classified but producing (fresh hb). Two
        # consecutive rebalance sweeps (the exact production-order repro where the
        # peer restored ownership and the next sweep re-drained) must leave the
        # owner in place.
        task, owner, peer = self._run_sweeps(reason='offline', hb=True, sweeps=2)
        self.assertEqual(task.assignee_email, owner,
                         "an actively-producing (offline+heartbeat) owner must keep "
                         "already-owned work across repeated rebalance sweeps")

    def test_hard_disabled_owner_still_drains(self):
        # Negative: operator-disabled (not heartbeat-refutable) drains to the live peer.
        task, owner, peer = self._run_sweeps(reason='disabled', hb=True, sweeps=1)
        self.assertEqual(task.assignee_email, peer,
                         "a hard-disabled owner must still drain to the available peer")

    def test_offline_no_heartbeat_owner_drains(self):
        # Negative: truly hard-off (offline, no recent heartbeat) drains.
        task, owner, peer = self._run_sweeps(reason='offline', hb=False, sweeps=1)
        self.assertEqual(task.assignee_email, peer,
                         "an offline owner with no recent heartbeat must drain")


class PLS494RealAuthBoundaryIntegrationTests(TestCase):
    """aoi Architecture contract (PLS-494): exercise the REAL authenticated
    request -> liveness marker -> two-sweep sequence against real Redis, with NO
    mocking of the marker read (`_agent_has_recent_pulse_heartbeat`) or the
    unavailability reason (`_agent_unavailability_reason`).

    rei's P1 on PR #7: the earlier suite mocked both, so it could not detect the
    split-brain aoi identified — a committed authenticated agent mutation can
    exist while the marker is absent (best-effort, is_agent-claim-gated, error-
    swallowing write). These tests drive the actual `get_user` boundary write and
    let the real sweep read real Redis. Each test namespaces its own agent
    identity so the shared Redis is safe under `--parallel`.
    """

    def _redis(self):
        return WorkflowService._redis_client()

    def _fresh_identity(self):
        import uuid
        tag = uuid.uuid4().hex[:10]
        owner = f'owner{tag}@shizuha.com'
        peer = f'peer{tag}@shizuha.com'
        owner_alias = f'owner{tag}@agents.shizuha.io'
        # deterministic uids from the tag so the fake user_service resolves both
        base = 700000 + (int(tag, 16) % 200000)
        # Unique org per identity → the DB TeamRoster row and its org-scoped
        # project/items are isolated per test (and per `--parallel` worker).
        org_id = 610000 + (int(tag, 16) % 40000)
        users = {owner: (base, owner), peer: (base + 1, peer)}
        roster = {'engineering': [[owner, peer]]}
        return owner, peer, owner_alias, org_id, users, roster

    def _clear(self, *emails):
        # Parallel-safe: the marker keys are per-identity (unique tag), and the
        # shared reason map is edited entry-scoped — never SET/DELETE wholesale —
        # so sibling `--parallel` workers don't clobber each other's Redis state.
        import json
        rc = self._redis()
        owned = set()
        for e in emails:
            owned.add(e)
            for alias in agent_liveness.agent_email_aliases(e):
                owned.add(alias)
                rc.delete(f'pulse:agent_hb:{alias}')
        raw = rc.get('pulse:agent_unavailability_reason')
        if raw:
            data = json.loads(raw)
            if any(k in data for k in owned):
                for k in owned:
                    data.pop(k, None)
                rc.set('pulse:agent_unavailability_reason', json.dumps(data))

    def _publish_reason(self, mapping):
        import json
        rc = self._redis()
        raw = rc.get('pulse:agent_unavailability_reason')
        data = json.loads(raw) if raw else {}
        data.update(mapping)
        rc.set('pulse:agent_unavailability_reason', json.dumps(data))

    def _authenticate(self, email, *, is_agent):
        # REAL auth boundary: get_user refreshes the canonical marker for the
        # resolved actor BEFORE the is_agent branch. NO exception swallowing
        # (revi's fixture requirement): a get_user failure must surface — the
        # tests here assert on the marker/drain outcome that depends on it.
        import uuid
        token = {
            'user_id': str(uuid.uuid4()), 'email': email,
            'username': email.split('@')[0], 'is_agent': is_agent,
        }
        ShizuhaJWTAuthentication().get_user(token)

    def _install_roster(self, owner, peer, org_id):
        # The failover-drain pod loop iterates ACTIVE DB TeamRoster rows (not the
        # in-memory pod roster); the drain only fires for a pod that has a real row.
        from tasks.models import TeamRoster
        return TeamRoster.objects.create(
            team_name='engineering', organization_slug=f'torg{org_id}',
            organization_id=org_id, tiers=[[owner, peer]], is_active=True,
        )

    def _make_task(self, owner_email, owner_uid, org_id):
        # The drain scopes movable work by ``project__org_id`` == the roster's org,
        # so the owned incident must live in an org-real project (a projectless item
        # is invisible to the org-scoped failover sweep).
        from tasks.models import Item, Project
        project = Project.objects.create(
            org_id=org_id, name=f'Incident project {org_id}',
            key=f'PJ{org_id % 1000:03d}')
        return Item.objects.create(
            title='In-progress incident (actively producing)',
            mode='task', status='in_progress', priority='urgent',
            assignment_group='engineering', project=project,
            assignee_id=owner_uid, assignee_email=owner_email,
        )

    def _run_sweeps(self, users, roster, available_emails, sweeps=2):
        def fake_by_email(email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def fake_by_id(uid):
            for u, e in users.values():
                if int(uid) == u:
                    return {'id': u, 'email': e}
            return None

        class FakeUS:
            get_user_by_email = staticmethod(fake_by_email)
            get_user_by_id = staticmethod(fake_by_id)

        avail = {e.lower() for e in available_emails}
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', FakeUS()), \
             patch.object(WorkflowService, '_pod_roster', return_value=roster), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in avail):
            for _ in range(sweeps):
                sweep_rebalance_pods.run()

    def test_authenticated_request_records_marker_even_without_is_agent_claim(self):
        # THE split-brain proof: a genuinely authenticated agent mutation whose
        # is_agent claim is absent/false must STILL refresh the canonical marker.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        self.assertFalse(t._agent_has_recent_pulse_heartbeat(owner))
        self._authenticate(owner, is_agent=False)
        self.assertTrue(
            t._agent_has_recent_pulse_heartbeat(owner),
            'the auth boundary must record the liveness marker regardless of the '
            'best-effort is_agent claim (the evidence PLS-204 was missing)')

    def test_active_offline_owner_survives_two_sweeps_unmocked(self):
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        self._authenticate(owner, is_agent=True)
        self._publish_reason({owner: 'offline'})
        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, users[owner][0], org_id)
        self._run_sweeps(users, roster, {peer}, sweeps=2)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, owner,
                         'a real authenticated (offline+marker) owner must keep '
                         'already-owned work across two real sweeps')

    def test_disabled_owner_drains_unmocked(self):
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        self._authenticate(owner, is_agent=True)
        self._publish_reason({owner: 'disabled'})
        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, users[owner][0], org_id)
        self._run_sweeps(users, roster, {peer}, sweeps=1)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, peer,
                         'operator-disabled drains even with a fresh marker')

    def test_no_marker_offline_owner_drains_unmocked(self):
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)  # no authenticated request → no marker
        self._publish_reason({owner: 'offline'})
        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, users[owner][0], org_id)
        self._run_sweeps(users, roster, {peer}, sweeps=1)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, peer,
                         'offline with no recent marker is truly hard-off → drains')

    def test_alias_form_parity_marker_protects_canonical_owner(self):
        # Marker refreshed under the @agents.shizuha.io alias; owner recorded under
        # the canonical @shizuha.com form. The shared fold must find it.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        self._authenticate(owner_alias, is_agent=True)
        self._publish_reason({owner: 'offline'})
        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, users[owner][0], org_id)
        self._run_sweeps(users, roster, {peer}, sweeps=2)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, owner,
                         'a marker written under the agents-alias must protect the '
                         'canonical-form owner (shared alias fold)')

    def _isolated_metrics_file(self):
        # The failure counter is a DURABLE, file-backed JSON at the module globals
        # ``_HB_METRICS_PATH`` / ``_HB_METRICS_LOCK_PATH`` — bound at import time, so
        # patching the env var would not redirect it. Patch the module attributes to
        # a fresh per-test file (initialized to zero) so the shared cross-worker
        # counter cannot leak between tests / `--parallel` workers.
        import json, os as _os, shutil, tempfile
        d = tempfile.mkdtemp(prefix='pls494metrics_')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = _os.path.join(d, 'agent-liveness-metrics.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'write_failures': 0, 'last_failure_ts': 0}, f)
        return path

    def test_marker_storage_failure_is_observable_and_fails_safe(self):
        # aoi: on marker-write failure, emit diagnosable telemetry AND preserve
        # hard-off drain semantics (never broaden the override on a swallowed error).
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        metrics_path = self._isolated_metrics_file()
        with patch.object(agent_liveness, '_HB_METRICS_PATH', metrics_path), \
             patch.object(agent_liveness, '_HB_METRICS_LOCK_PATH', metrics_path + '.lock'):
            before = agent_liveness.hb_write_failure_count()
            with patch.object(agent_liveness, '_redis_client',
                              side_effect=RuntimeError('redis down')), \
                 self.assertLogs('tasks.agent_liveness', level='WARNING') as logs:
                ok = _record_agent_heartbeat(owner)
            self.assertFalse(ok, 'a failed marker write must report failure')
            # Durable, file-backed counter — assert monotonic increase (the shared
            # counter is not exactly +1 under `--parallel`, so never assert equality).
            self.assertGreaterEqual(
                agent_liveness.hb_write_failure_count(), before + 1,
                'marker-write failures must be counted in the durable file counter')
        self.assertTrue(any('pulse_agent_hb_write_failed' in m for m in logs.output),
                        'marker-write failure must emit a diagnosable WARNING')
        # Fail-safe: with no marker persisted, an offline owner still drains.
        self._publish_reason({owner: 'offline'})
        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, users[owner][0], org_id)
        self._run_sweeps(users, roster, {peer}, sweeps=1)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, peer,
                         'a swallowed-but-observed marker failure must not protect '
                         'the owner — it degrades to hard-off drain')

    def test_marker_write_failure_surfaces_on_metrics_endpoint(self):
        # revi P1 #3: the durable /metrics surface must reflect a marker-write
        # failure. Force the write to fail, then assert
        # ``render_agent_liveness_metrics()`` exposes
        # ``pulse_agent_hb_write_failures_total`` with a value >= 1. Uses an isolated
        # metrics file so the assertion is deterministic regardless of sibling tests.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        metrics_path = self._isolated_metrics_file()
        with patch.object(agent_liveness, '_HB_METRICS_PATH', metrics_path), \
             patch.object(agent_liveness, '_HB_METRICS_LOCK_PATH', metrics_path + '.lock'):
            with patch.object(agent_liveness, '_redis_client',
                              side_effect=RuntimeError('redis down')):
                self.assertFalse(_record_agent_heartbeat(owner),
                                 'the forced marker write must report failure')
            text = agent_liveness.render_agent_liveness_metrics()
        self.assertIn('pulse_agent_hb_write_failures_total', text,
                      'the /metrics surface must expose the failure counter series')
        value = None
        for line in text.splitlines():
            if line.startswith('pulse_agent_hb_write_failures_total '):
                value = float(line.split()[-1])
        self.assertIsNotNone(value, 'the counter sample line must be present')
        self.assertGreaterEqual(
            value, 1.0,
            'the durable /metrics counter must reflect the marker-write failure')

    def test_marker_write_retries_once_before_counting_failure(self):
        # Live incident class: a single Redis blip (timeout / brief disconnect)
        # must not page PLS-494 or drop a LIVE owner's marker. One short retry
        # recovers; only exhausted attempts bump the durable counter.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        metrics_path = self._isolated_metrics_file()
        pipe = mock.Mock()
        pipe.setex = mock.Mock()
        pipe.execute = mock.Mock(return_value=[True, True])
        rc_fail = mock.Mock()
        rc_fail.pipeline = mock.Mock(side_effect=TimeoutError('redis blip'))
        rc_ok = mock.Mock()
        rc_ok.pipeline = mock.Mock(return_value=pipe)
        with patch.object(agent_liveness, '_HB_METRICS_PATH', metrics_path), \
             patch.object(agent_liveness, '_HB_METRICS_LOCK_PATH', metrics_path + '.lock'), \
             patch.object(agent_liveness, '_HB_WRITE_RETRY_SLEEP_S', 0), \
             patch.object(agent_liveness, '_redis_client',
                          side_effect=[rc_fail, rc_ok]):
            before = agent_liveness.hb_write_failure_count()
            ok = _record_agent_heartbeat(owner)
            after = agent_liveness.hb_write_failure_count()
        self.assertTrue(ok, 'a single blip must recover on the retry attempt')
        self.assertEqual(
            after, before,
            'recovered marker writes must NOT bump the durable failure counter')
        pipe.execute.assert_called_once()


class PLS494RealApiStackIntegrationTests(TestCase):
    """revi BLOCKING P1 (PLS-494): the 'real auth boundary' regression must execute
    the PRODUCTION sequence, not a synthetic ``get_user`` poke.

    revi's finding on ``PLS494RealAuthBoundaryIntegrationTests`` was that it called
    ``ShizuhaJWTAuthentication.get_user()`` directly with a synthetic token dict and
    swallowed every exception, so the marker (written inside ``get_user`` before any
    view/durable mutation) was recorded even if the request later failed — proving
    neither a successful authenticated API mutation nor the direct-handoff boundary,
    and substituting 'no marker' for the required stale-marker probe.

    This class instead:
      1. mints a REAL agent JWT (HS256, signed with the app's JWT signing key) and
         drives a REAL authenticated Pulse mutation (POST /comments/) through the
         API/request stack — asserting 2xx AND the row committed AND the canonical
         ``pulse:agent_hb:<alias>`` marker now exists. NO try/except swallows a
         failed request/auth: a non-2xx fails the test.
      2. performs the direct owner handoff, publishes the soft-offline reason to the
         real Redis sidecar, and runs two top-level sweeps with NO mocking of
         ``_agent_has_recent_pulse_heartbeat`` / ``_agent_unavailability_reason`` —
         asserting the actively-producing owner SURVIVES.
      3. writes the marker with a REAL EXPIRED timestamp (older than the drain
         grace) and asserts the offline owner DRAINS (stale marker != live activity).

    Each test namespaces its own agent identity + Redis keys so the shared Redis is
    ``--parallel``-safe; the shared reason map is edited entry-scoped only.
    """

    def setUp(self):
        # SCLI-376: get_validated_token now consults is_token_revoked on EVERY
        # token (fail closed). The id service is not reachable in unit tests, so
        # the revocation lookup would fail closed and 401 every real authenticated
        # mutation with 'Token has been revoked' — breaking the real-auth-boundary
        # assertions below. This class tests the liveness-marker / ownership-
        # transition boundary, NOT revocation, so mock the lookup to report the
        # minted token as active. Matches the pattern in test_jwks_canonical_verifier.py.
        from tasks import token_revocation as trev
        self._revocation_cache = dict(trev._REVOCATION_CACHE)
        trev._REVOCATION_CACHE.clear()
        self._resp = mock.MagicMock()
        self._resp.status_code = 200
        self._resp.json.return_value = {"active": True}
        self._post_patcher = patch(
            "tasks.token_revocation.requests.post", return_value=self._resp)
        self._mock_post = self._post_patcher.start()
        self.addCleanup(self._post_patcher.stop)

    def tearDown(self):
        from tasks import token_revocation as trev
        trev._REVOCATION_CACHE.clear()
        trev._REVOCATION_CACHE.update(self._revocation_cache)

    def _redis(self):
        return WorkflowService._redis_client()

    def _fresh_identity(self):
        import uuid
        tag = uuid.uuid4().hex[:10]
        owner = f'owner{tag}@shizuha.com'
        peer = f'peer{tag}@shizuha.com'
        owner_alias = f'owner{tag}@agents.shizuha.io'
        base = 700000 + (int(tag, 16) % 200000)
        # A unique org per identity keeps the DB-backed TeamRoster row and its
        # org-scoped project/items isolated from any other row and ``--parallel``
        # worker (the failover-drain pod loop iterates real TeamRoster rows and
        # scopes movable work by project org, so the fixture must be org-real).
        org_id = 550000 + (int(tag, 16) % 40000)
        users = {owner: (base, owner), peer: (base + 1, peer)}
        roster = {'engineering': [[owner, peer]]}
        return owner, peer, owner_alias, org_id, users, roster

    def _clear(self, *emails):
        import json
        rc = self._redis()
        owned = set()
        for e in emails:
            owned.add(e)
            for alias in agent_liveness.agent_email_aliases(e):
                owned.add(alias)
                rc.delete(f'pulse:agent_hb:{alias}')
        raw = rc.get('pulse:agent_unavailability_reason')
        if raw:
            data = json.loads(raw)
            if any(k in data for k in owned):
                for k in owned:
                    data.pop(k, None)
                rc.set('pulse:agent_unavailability_reason', json.dumps(data))

    def _publish_reason(self, mapping):
        import json
        rc = self._redis()
        raw = rc.get('pulse:agent_unavailability_reason')
        data = json.loads(raw) if raw else {}
        data.update(mapping)
        rc.set('pulse:agent_unavailability_reason', json.dumps(data))

    def _install_roster(self, owner, peer, org_id):
        # The failover-drain pod loop iterates ACTIVE DB TeamRoster rows (not the
        # in-memory pod roster), so the drain only fires for a pod that has a real
        # row. A single flat tier ``[[owner, peer]]`` is a normal load-balanced
        # team. A non-reserved org slug keeps org_id as-provided (the slug→id guard
        # only fires for the four reserved slugs).
        from tasks.models import TeamRoster
        return TeamRoster.objects.create(
            team_name='engineering', organization_slug=f'torg{org_id}',
            organization_id=org_id, tiers=[[owner, peer]], is_active=True,
        )

    def _make_task(self, owner_email, owner_uid, org_id):
        # The drain scopes movable work by ``project__org_id`` == the roster's org,
        # so the owned incident must live in an org-real project (a projectless item
        # is invisible to the org-scoped failover sweep).
        from tasks.models import Item, Project
        project = Project.objects.create(
            org_id=org_id, name=f'Incident project {org_id}',
            key=f'PJ{org_id % 1000:03d}',
        )
        return Item.objects.create(
            title='In-progress incident (actively producing)',
            mode='task', status='in_progress', priority='urgent',
            assignment_group='engineering', project=project,
            assignee_id=owner_uid, assignee_email=owner_email,
        )

    def _run_sweeps(self, users, roster, available_emails, sweeps=2):
        # NO mocking of `_agent_unavailability_reason` / `_agent_has_recent_pulse_
        # heartbeat`: the sweep reads BOTH from the real Redis this test populated.
        def fake_by_email(email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def fake_by_id(uid):
            for u, e in users.values():
                if int(uid) == u:
                    return {'id': u, 'email': e}
            return None

        class FakeUS:
            get_user_by_email = staticmethod(fake_by_email)
            get_user_by_id = staticmethod(fake_by_id)

        avail = {e.lower() for e in available_emails}
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', FakeUS()), \
             patch.object(WorkflowService, '_pod_roster', return_value=roster), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e.lower() in avail):
            for _ in range(sweeps):
                sweep_rebalance_pods.run()

    # ── the REAL authenticated API mutation ─────────────────────────────────
    def _bearer(self, email, uid, *, is_agent):
        # A REAL HS256 access token signed with the app's JWT signing key
        # (== SECRET_KEY in CI). Presented over the wire it authenticates through
        # ShizuhaJWTAuthentication.get_user, which is where the marker is written.
        from rest_framework_simplejwt.tokens import AccessToken
        token = AccessToken()
        token['user_id'] = uid          # integer id → Comment.author_id (IntegerField)
        token['email'] = email          # canonical agent identity drives the marker
        token['username'] = email.split('@')[0]
        token['is_agent'] = is_agent
        token['is_superuser'] = True    # grants comment access (can_comment_on_item)
        return str(token)

    def _authenticated_api_comment(self, email, uid, *, is_agent):
        """Drive a REAL authenticated comment mutation through the API/request stack.

        Returns (response, item, content). Intentionally NO try/except: a failed
        request or a failed authentication surfaces as a non-2xx status the caller
        asserts on, so it fails the test (revi's explicit requirement).
        """
        import uuid
        from rest_framework.test import APIClient
        from tasks.models import Item
        target = Item.objects.create(
            title='Owned work touched by an authenticated agent mutation',
            mode='task', status='in_progress', priority='high',
            assignment_group='engineering',
            assignee_id=uid, assignee_email=email,
        )
        content = f'authenticated progress note {uuid.uuid4().hex[:8]}'
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {self._bearer(email, uid, is_agent=is_agent)}')
        resp = client.post(
            '/shizuha-pulse/api/comments/',
            {'item': target.id, 'content': content}, format='json')
        return resp, target, content

    # ── the REAL direct-handoff / ownership-transition boundary ─────────────
    def _make_unassigned_incident(self, org_id):
        """Create the org-scoped in-progress incident in the UNASSIGNED state —
        NOT pre-assigned to the target agent-owner. The real /assign handoff below
        performs the actual ownership transition to the target owner, so the
        fixture exercises the PLAT-4678 production-order ownership-transition/reset
        boundary instead of substituting a pre-assigned task for it. (The guarded
        Item.save org-Team fence only validates a CONCRETE assignee, so a member
        of the org's engineering roster cannot be simulated here without an actual
        assignment — which is exactly what the /assign endpoint below drives.)
        """
        from tasks.models import Item, Project
        project = Project.objects.create(
            org_id=org_id, name=f'Incident project {org_id}',
            key=f'PJ{org_id % 1000:03d}')
        return Item.objects.create(
            title='In-progress incident (handed off to the active owner)',
            mode='task', status='in_progress', priority='urgent',
            assignment_group='engineering', project=project,
            assignee_id=None, assignee_email=None,
        )

    def _assign_incident_to_owner(self, item, owner_email, users):
        """Drive the REAL production assignment endpoint to hand the incident to
        the target owner, then return (response, resolved_email).

        POST /shizuha-pulse/api/items/<id>/assign/ is the exact production caller
        the MCP ``pulse_assign_task`` / the API team-route ``assignment_group``
        update routes to (``ItemViewSet.assign`` in tasks/views.py). Routing to the
        ``engineering`` team executes the real ownership-transition boundary:
        ``_resolve_team_assignee`` (org-scoped roster resolution + availability +
        the PLS-509 slot reservation) → ``Item.assign`` → the guarded ``Item.save``
        org/team fence → the atomic ``record_direct_handoff`` audit — a genuine
        assignment committed to the DB, NOT a simulated pre-assignment.

        A team route (not a direct+reason override) is used deliberately: it lands
        a CONCRETE owner while retaining the ``engineering`` group the org-scoped
        failover drain keys on (``drain_all_items`` filters
        ``assignment_group__iexact=pod``) and, unlike a direct decision handoff,
        writes NO PLS-233 decision pin — so the drain decision under test is driven
        ONLY by the heartbeat/unavailability reason, never a pin. Only the owner is
        available during this call, so the team route deterministically resolves to
        the owner. NO try/except swallows a non-2xx: the caller asserts success.
        """
        import uuid
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        def fake_by_email(email):
            rec = users.get((email or '').strip().lower())
            return {'id': rec[0], 'email': rec[1]} if rec else None

        def fake_by_id(uid):
            for u, e in users.values():
                if int(uid) == u:
                    return {'id': u, 'email': e}
            return None

        def fake_resolve(user_id=None, email=None):
            if email:
                return fake_by_email(email)
            if user_id is not None:
                return fake_by_id(user_id)
            return None

        class FakeUS:
            get_user_by_email = staticmethod(fake_by_email)
            get_user_by_id = staticmethod(fake_by_id)
            resolve_user_info = staticmethod(fake_resolve)

        User = get_user_model()
        # A distinct staff actor performs the handoff, so the marker under test is
        # the owner's committed comment mutation, never a side effect of the assign
        # call's own authentication.
        actor = User.objects.create_user(
            username=f'handoff{uuid.uuid4().hex[:10]}',
            email=f'handoff{uuid.uuid4().hex[:8]}@shizuha.com',
            password='x', is_staff=True, is_superuser=True)
        org_id = item.project.org_id
        client = APIClient()
        client.force_authenticate(actor)
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', FakeUS()), \
             patch('tasks.services.admin_service.admin_service.get_organization',
                   return_value={'id': org_id, 'slug': f'torg{org_id}'}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: (e or '').strip().lower() == owner_email.lower()):
            resp = client.post(
                f'/shizuha-pulse/api/items/{item.id}/assign/',
                {'assignment_group': 'engineering'}, format='json')
        return resp

    def test_real_api_mutation_authenticates_commits_and_writes_marker(self):
        # (1) A real authenticated Pulse mutation over the API stack: it drives
        # ShizuhaJWTAuthentication.get_user (marker write) AND durably commits a row.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        uid = users[owner][0]
        self.assertFalse(t._agent_has_recent_pulse_heartbeat(owner),
                         'precondition: the namespaced identity has no marker yet')

        resp, target, content = self._authenticated_api_comment(owner, uid, is_agent=True)

        # (a) the request authenticated and returned success through the real stack
        self.assertIn(resp.status_code, (200, 201),
                      f'authenticated mutation must succeed: {getattr(resp, "content", resp)!r}')
        # (b) the mutation durably committed
        from tasks.models import Comment
        self.assertTrue(
            Comment.objects.filter(item=target, content=content, author_email=owner).exists(),
            'a successful authenticated API mutation must persist the comment row')
        # (c) get_user wrote the canonical liveness marker (unmocked real-Redis read)
        self.assertTrue(
            t._agent_has_recent_pulse_heartbeat(owner),
            'a committed authenticated API mutation must refresh the pulse:agent_hb marker')

    def test_active_owner_survives_two_sweeps_via_real_api_mutation(self):
        # (2) The full PLAT-4678 production-order sequence, no shortcut:
        #   1. incident created under a DIFFERENT prior owner (not pre-assigned);
        #   2. the target owner commits a REAL authenticated Pulse mutation
        #      (POST /comments/) that writes the live marker;
        #   3. the REAL /assign endpoint (pulse_assign_task's production caller)
        #      performs the team-route ownership transition TO the owner;
        #   4. the soft-offline reason is published to the real Redis sidecar;
        #   5. two top-level rebalance sweeps run;
        #   6. the actively-producing owner SURVIVES (no mocking of the marker
        #      read or the unavailability reason).
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        uid = users[owner][0]

        # (1) unassigned incident; (2) committed authenticated mutation
        self._install_roster(owner, peer, org_id)
        task = self._make_unassigned_incident(org_id)
        self.assertFalse(task.assignee_email,
                         'precondition: the incident is UNASSIGNED, not pre-assigned '
                         'to the target agent-owner')
        resp, _target, _content = self._authenticated_api_comment(owner, uid, is_agent=True)
        self.assertIn(resp.status_code, (200, 201),
                      f'the marker-writing mutation must succeed: {getattr(resp, "content", resp)!r}')
        self.assertTrue(t._agent_has_recent_pulse_heartbeat(owner),
                        'the real mutation must have written the marker before the handoff')

        # (3) REAL ownership transition to the owner via the /assign endpoint
        handoff = self._assign_incident_to_owner(task, owner, users)
        self.assertIn(handoff.status_code, (200, 201),
                      f'the real /assign handoff must succeed: {getattr(handoff, "content", handoff)!r}')
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, owner,
                         'the real assignment endpoint must have transferred ownership '
                         'to the target owner')
        self.assertEqual((task.assignment_group or '').lower(), 'engineering',
                         'the handoff must retain the team the org-scoped '
                         'failover drain keys on')

        # (4) publish the soft-offline unavailability reason to the real Redis sidecar
        self._publish_reason({owner: 'offline'})
        # sanity: the real sweep sees the live marker → the owner is protected
        self.assertTrue(t._soft_unavailable_owner_protected(owner),
                        'offline + a fresh authenticated marker must be protected')
        # (5) two top-level rebalance sweeps (the production re-drain order)
        self._run_sweeps(users, roster, {peer}, sweeps=2)
        # (6) the owner survives
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, owner,
                         'an actively-producing owner (real authenticated marker + '
                         'offline sample), handed the incident through the REAL '
                         'assignment endpoint, must keep already-owned work across '
                         'two real rebalance sweeps')

    def test_hard_disabled_owner_drains_via_real_api_handoff(self):
        # Negative mirror of the survive case in the SAME production order: an
        # incident is created under a prior owner, the owner commits a real
        # authenticated mutation, the REAL /assign endpoint transfers ownership to
        # the owner, then the owner is published HARD-disabled. The fresh marker
        # does NOT refute an operator kill-switch, so two sweeps still drain the
        # incident to the available peer. Same real handoff boundary as the
        # positive case — no synthetic pre-assignment.
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        uid = users[owner][0]

        self._install_roster(owner, peer, org_id)
        task = self._make_unassigned_incident(org_id)
        resp, _target, _content = self._authenticated_api_comment(owner, uid, is_agent=True)
        self.assertIn(resp.status_code, (200, 201),
                      f'the marker-writing mutation must succeed: {getattr(resp, "content", resp)!r}')
        self.assertTrue(t._agent_has_recent_pulse_heartbeat(owner),
                        'the real mutation must have written the marker before the handoff')

        handoff = self._assign_incident_to_owner(task, owner, users)
        self.assertIn(handoff.status_code, (200, 201),
                      f'the real /assign handoff must succeed: {getattr(handoff, "content", handoff)!r}')
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, owner,
                         'the real assignment endpoint must have transferred ownership '
                         'to the target owner before the drain')
        self.assertEqual((task.assignment_group or '').lower(), 'engineering',
                         'the handoff must retain the team the drain keys on')

        # publish the HARD-off (operator-disabled) reason; the fresh marker is present
        self._publish_reason({owner: 'disabled'})
        self.assertFalse(t._soft_unavailable_owner_protected(owner),
                         'operator-disabled is hard-off — a fresh marker must NOT '
                         'protect it from drain')
        self._run_sweeps(users, roster, {peer}, sweeps=2)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, peer,
                         'a hard-disabled owner (even with a fresh authenticated '
                         'marker), handed the incident through the REAL assignment '
                         'endpoint, must still drain to the available peer')

    def test_stale_marker_offline_owner_drains(self):
        # (3) Stale-marker NEGATIVE: a REAL expired timestamp (far older than the
        # PULSE_FAILOVER_DRAIN_GRACE_SEC=1200 grace) is NOT live activity, so an
        # offline owner must still drain to the peer.
        import time
        owner, peer, owner_alias, org_id, users, roster = self._fresh_identity()
        self.addCleanup(self._clear, owner)
        self._clear(owner)
        uid = users[owner][0]

        canonical = agent_liveness.canonical_agent_email(owner)
        ttl = agent_liveness.heartbeat_ttl_seconds()
        # marker present in Redis, but its recorded activity time is long expired
        self._redis().set(
            f'pulse:agent_hb:{canonical}', repr(time.time() - 100000), ex=ttl)
        self.assertFalse(
            t._agent_has_recent_pulse_heartbeat(owner),
            'a marker whose timestamp is older than the drain grace is not recent')

        self._install_roster(owner, peer, org_id)
        task = self._make_task(owner, uid, org_id)
        self._publish_reason({owner: 'offline'})
        # sanity: a stale marker + offline is NOT protected (the drain must fire)
        self.assertFalse(t._soft_unavailable_owner_protected(owner),
                         'a stale marker must not protect an offline owner')
        self._run_sweeps(users, roster, {peer}, sweeps=1)
        task.refresh_from_db()
        self.assertEqual(task.assignee_email, peer,
                         'a stale (expired-timestamp) marker is not live activity → '
                         'the offline owner must drain to the available peer')
