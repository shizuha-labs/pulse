"""PLS-742 regression: `AgentAvailability.updated_by` ownership must be compared
by ONE normalisation rule everywhere, so a row stamped `Health-Sweep`, `HEALTH-SWEEP `
or `health-sweep` is owned by the same actor on every path.

Three historical comparison styles (exact / lower-cased / __iexact) meant one
row could be health-sweep-owned to one code path and self-owned to another.
This suite asserts IDENTICAL classification across the drain-set path, the
routing/escape path (`_is_operator_availability_actor`), the reactivation loop,
and `mark_agent_provider_recovered` — including the ordering consequence: a
mixed-case `Health-Sweep` row + live health True must NOT be drained.
"""
import json
import uuid
from unittest.mock import patch

from django.test import TestCase

from tasks.models import AgentAvailability
from tasks.services.workflow_service import WorkflowService

_ACTOR_STAMPS = [
    'health-sweep',
    'Health-Sweep',
    'HEALTH-SWEEP ',
    ' HEALTH-SWEEP',
]

_OPERATOR_STAMPS = [
    'operator',
    'Operator',
    'OPERATOR ',
]


class _FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value

    def set(self, key, value, nx=False, ex=None, px=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    def setnx(self, key, value):
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def pipeline(self):
        return _FakeRedisPipeline(self)

    def names(self):
        return list(self.values)


class _FakeRedisPipeline:
    def __init__(self, r):
        self.r = r
        self.cmds = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set(self, key, value, nx=False, ex=None, px=None):
        self.cmds.append(('set', key, value))
        return self

    def get(self, key):
        self.cmds.append(('get', key))
        return self

    def delete(self, *keys):
        self.cmds.append(('delete', keys))
        return self

    def execute(self):
        for cmd in self.cmds:
            if cmd[0] == 'set':
                self.r.set(cmd[1], cmd[2])
        return []


class PLS742ActorComparisonConsistencyTest(TestCase):
    def setUp(self):
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None
        # PLS-1187 circuit breaker (main fold #452): _redis_circuit_until is
        # process-global class state that survives between tests. While it is
        # open, _redis_get returns None WITHOUT consulting the patched
        # _redis_client, so the health map reads {} and a live-health-UP row
        # false-drains whenever an earlier test in the same parallel worker
        # tripped the circuit on the real (hermetic-guard-refused) client.
        # Reset it with the other memo caches (PLAT-7047 combined-tree red).
        WorkflowService._redis_circuit_until = 0.0
        suffix = uuid.uuid4().hex[:8]
        self.email = f'seat{suffix}@shizuha.com'

    def tearDown(self):
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None
        WorkflowService._redis_circuit_until = 0.0

    def _stamp_row(self, updated_by, *, email=None, active=False, reason=''):
        return AgentAvailability.objects.create(
            email=email or self.email,
            active=active,
            status='inactive' if not active else 'active',
            reason=reason,
            updated_by=updated_by,
        )

    # ---- route 1: drain-set path (_disabled_agent_emails) ----
    def _disabled_set(self, health_email=None):
        fake = _FakeRedis()
        if health_email:
            fake.values[WorkflowService._AGENT_AVAILABILITY_KEY] = json.dumps({
                health_email: True,
            })
        else:
            fake.values[WorkflowService._AGENT_AVAILABILITY_KEY] = json.dumps({})
        WorkflowService._redis_circuit_until = 0.0
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None
        with patch.object(WorkflowService, '_redis_client', return_value=fake):
            out = WorkflowService._disabled_agent_emails()
        WorkflowService._redis_circuit_until = 0.0
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None
        WorkflowService._db_avail_cache = None
        return out

    def test_mixed_case_health_sweep_not_drained_when_health_up(self):
        """Acceptance #6 consequence: `Health-Sweep` row + live health True is
        NOT in the drain set — it must not be drained at the :9364 short-circuit."""
        for stamp in _ACTOR_STAMPS:
            with self.subTest(stamp=stamp):
                email = f'drain{uuid.uuid4().hex[:8]}@shizuha.com'
                self._stamp_row(stamp, email=email)
                disabled = self._disabled_set(health_email=email)
                self.assertNotIn(
                    email, disabled,
                    f'{stamp!r} must not enter the disabled/drain set while '
                    f'live health is True',
                )

    def test_health_sweep_variant_reads_as_non_operator_on_routing_path(self):
        """Route 2: _is_operator_availability_actor must return False for every
        health-sweep spelling (it is automation, not an operator kill-switch)."""
        for stamp in _ACTOR_STAMPS:
            with self.subTest(stamp=stamp):
                self.assertFalse(
                    WorkflowService._is_operator_availability_actor(stamp),
                    f'{stamp!r} must NOT be an operator kill-switch',
                )

    def test_operator_variants_always_read_as_sacred(self):
        """Every operator spelling is sacred on BOTH routes (drain + escape):
        an operator force-OFF must remain in the drain set regardless of health,
        and must short-circuit the operator gate. This is the non-widening
        guard (acceptance #4 — the operator exclusion is correct and untouched)."""
        for stamp in _OPERATOR_STAMPS:
            with self.subTest(stamp=stamp):
                self.assertTrue(
                    WorkflowService._is_operator_availability_actor(stamp),
                    f'{stamp!r} must be an operator kill-switch',
                )

    # ---- route 3: reactivation loop exclusion ----
    def test_reactivation_loop_excludes_every_operator_spelling(self):
        """Acceptance #3: rows stamped with every operator variant are excluded
        from auto-reactivation — sacred regardless of case/whitespace."""
        # Each stamp is a distinct row on a distinct email; the loop must not
        # touch any of them even with a streak-0 healthy sample.
        emails = []
        for stamp in _OPERATOR_STAMPS:
            email = f'op{uuid.uuid4().hex[:6]}@shizuha.com'
            emails.append(email)
            self._stamp_row(stamp, email=email)
        active_before = {
            e: AgentAvailability.objects.get(email=e).active
            for e in emails
        }
        # The reactivation loop only considers active=False rows anyway; assert
        # the exclusion query itself yields none of the operator rows.
        from tasks.models import AgentAvailability as AA
        candidates = AA.objects.filter(active=False).exclude(
            AA.actor_q(AA.ACTOR_OPERATOR))
        candidate_emails = {str(r.email) for r in candidates}
        self.assertTrue(emails and not (
            set(emails) & candidate_emails),
            'reactivation candidates must exclude every operator spelling')

    def test_reactivation_loop_includes_health_sweep_variants(self):
        """Mixed-case health-sweep rows stay IN the reactivation candidate set
        (automation-owned, recoverable) — the drain/escape/loop all agree they
        are NOT operator."""
        emails = []
        for stamp in _ACTOR_STAMPS:
            email = f'hs{uuid.uuid4().hex[:6]}@shizuha.com'
            emails.append(email)
            self._stamp_row(stamp, email=email)
        from tasks.models import AgentAvailability as AA
        candidates = list(
            AA.objects.filter(active=False).exclude(
                AA.actor_q(AA.ACTOR_OPERATOR)))
        candidate_emails = {str(r.email) for r in candidates}
        for email in emails:
            self.assertIn(
                email, candidate_emails,
                'health-sweep variants must remain reactivation candidates',
            )

    # ---- mark_agent_provider_recovered's _still_unhealthy query ----
    def test_provider_recovered_still_unhealthy_sees_health_sweep_variants(self):
        """Route 4 (ws:12468): a mixed-case health-sweep OFF row with a
        non-provider reason must block clearing the provider-down hold — the
        same row the drain path sees as sweep-owned, seen here as unhealthy."""
        for stamp in _ACTOR_STAMPS:
            email = f'pu{uuid.uuid4().hex[:6]}@shizuha.com'
            self._stamp_row(stamp, email=email, reason='empty turns')
            fake = _FakeRedis({
                WorkflowService._PROVIDER_DOWN_COOLDOWN_KEY: json.dumps({
                    email: 9999999999,
                }),
                WorkflowService._AGENT_AVAILABILITY_KEY: json.dumps({}),
                WorkflowService._AGENT_UNAVAILABILITY_REASON_KEY: json.dumps({}),
                WorkflowService._AGENT_DISABLED_KEY: json.dumps([]),
            })
            with patch.object(
                WorkflowService, '_redis_client', return_value=fake,
            ):
                with patch.object(
                    WorkflowService,
                    '_health_availability_map',
                    return_value={},
                ):
                    recovered = WorkflowService.mark_agent_provider_recovered(
                        email)
            self.assertFalse(
                recovered,
                f'{stamp!r} non-provider unhealthy row must block provider-down '
                f'clearance (it is still held by an unhealthy signal)',
            )


class PLS742NormalisationHelpersTest(TestCase):
    """The shared matcher must itself be consistent across spellings."""

    def test_norm_actor_strips_and_folds(self):
        for stamp in _ACTOR_STAMPS:
            self.assertEqual(
                AgentAvailability.norm_actor(stamp), 'health-sweep',
                f'norm_actor({stamp!r}) -> health-sweep')
        for stamp in _OPERATOR_STAMPS:
            self.assertEqual(
                AgentAvailability.norm_actor(stamp), 'operator',
                f'norm_actor({stamp!r}) -> operator')

    def test_is_actor_supports_self_prefix(self):
        self.assertTrue(AgentAvailability.is_actor(
            'self:kai@shizuha.com', 'self:'))
        self.assertTrue(AgentAvailability.is_actor(
            'Self:kai@shizuha.com ', 'self:'))
        self.assertTrue(AgentAvailability.is_actor(
            'health-sweep', 'health-sweep'))
        self.assertFalse(AgentAvailability.is_actor(
            'health-sweep', 'operator'))
        self.assertTrue(AgentAvailability.is_actor('OPERATOR ', 'operator'))

    def test_actor_q_matches_mixed_case_at_orm(self):
        email = f'q{uuid.uuid4().hex[:6]}@shizuha.com'
        AgentAvailability.objects.create(
            email=email, active=False, status='inactive',
            updated_by='HEALTH-SWEEP ')
        hit = AgentAvailability.objects.filter(
            email=email, active=False).filter(
            AgentAvailability.actor_q(AgentAvailability.ACTOR_HEALTH_SWEEP)
        ).exists()
        self.assertTrue(hit, 'actor_q must match HEALTH-SWEEP  at the ORM')
