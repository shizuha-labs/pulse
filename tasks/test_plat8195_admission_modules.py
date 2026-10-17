"""PLAT-8195 §5/§8.1–8.2 admission module invariants — unit tests.

Pure modules; no Django DB. Covers: reference-constants lint (§10), the §8.2
drain state machine, §5.2 deadline propagation (never-widen), and the §5.3–5.4
mutation attempt fence + phase ladder.
"""

import unittest

from tasks.services.admission_constants import (
    ASYNC_REJECT_THRESHOLD_S,
    LOCAL_MUTATION_COOPERATIVE_S,
    LOCAL_MUTATION_WIRE_CUTOFF_S,
    PHASE_COOPERATIVE_CANCEL_S,
    PHASE_KILL_AND_DB_FENCE_S,
    PHASE_PROOF_WINDOW_S,
    PHASE_TERM_FENCE_S,
    PHASE_WIRE_CUTOFF_S,
    validate_reference_constants,
)
from tasks.services.admission_deadline import (
    BUDGET_LOCAL_MUTATION,
    BUDGET_MEMBERSHIP_PROVIDER,
    BUDGET_ORDINARY_READ,
    Deadline,
    DeadlineExceeded,
    SyncWaitProhibited,
)
from tasks.services.admission_drain import (
    CRASH_ABORTED_ACTIVE,
    STATE_DRAINING,
    STATE_FAILED_CLOSED,
    STATE_QUIESCED,
    STATE_SERVING,
    STATE_TERMINATING,
    AdmissionDrainStateMachine,
    BootEpochViolation,
    CrashUnreconciled,
    DrainError,
    InvalidTransition,
    StaleNonce,
)
from tasks.services.admission_mutation_fence import (
    ATTEMPT_ACTIVE,
    ATTEMPT_FENCING_UNPROVEN,
    DoubleExecution,
    MutationAttemptFence,
    ProofMiss,
    TerminalBodyUnproven,
    ladder_phase,
    reservation_key,
)


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def make_machine(epoch_ok=True):
    broadcasts = []
    epoch = {"ok": epoch_ok}
    m = AdmissionDrainStateMachine(
        broadcast_close_admission=broadcasts.append,
        epoch_matches=lambda: epoch["ok"],
    )
    return m, broadcasts, epoch


class ReferenceConstantsLint(unittest.TestCase):
    def test_lint_passes_on_shipped_constants(self):
        validate_reference_constants()  # must not raise

    def test_ladder_is_strictly_increasing(self):
        bounds = [
            PHASE_COOPERATIVE_CANCEL_S,
            PHASE_TERM_FENCE_S,
            PHASE_KILL_AND_DB_FENCE_S,
            PHASE_PROOF_WINDOW_S,
            PHASE_WIRE_CUTOFF_S,
        ]
        for earlier, later in zip(bounds, bounds[1:]):
            self.assertLess(earlier, later)

    def test_wire_cutoffs_exceed_cooperative_budgets(self):
        self.assertGreater(LOCAL_MUTATION_WIRE_CUTOFF_S, LOCAL_MUTATION_COOPERATIVE_S)
        self.assertEqual(ASYNC_REJECT_THRESHOLD_S, LOCAL_MUTATION_WIRE_CUTOFF_S)


class DrainStateMachineTests(unittest.TestCase):
    def test_serve_then_drain_is_one_atomic_generation_change(self):
        m, broadcasts, _ = make_machine()
        m.serve(epoch_ok=True)
        m.register("p1")
        gen = m.begin_drain()
        self.assertEqual(gen, 1)
        self.assertEqual(m.state, STATE_DRAINING)
        self.assertTrue(m.registry_frozen)
        # close + broadcast happened in the same step, exactly once
        self.assertEqual(broadcasts, [1])

    def test_admission_closed_after_drain_begins(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.begin_drain()
        with self.assertRaises(InvalidTransition):
            m.admit("rest")

    def test_drain_completes_only_with_all_acks_and_zero_accepted(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.register("p1")
        m.register("p2")
        m.admit("rest")
        m.admit("mcp")
        m.begin_drain()
        self.assertEqual(m.maybe_complete(), STATE_DRAINING)  # nothing ACKed
        m.ack("p1")
        self.assertEqual(m.maybe_complete(), STATE_DRAINING)
        m.ack("p2")
        self.assertEqual(m.maybe_complete(), STATE_DRAINING)  # accepted sets nonzero
        m.decrement_accepted("p1", "rest")
        m.decrement_accepted("p2", "mcp")
        self.assertEqual(m.maybe_complete(), STATE_QUIESCED)

    def test_stale_nonce_cannot_ack_or_decrement(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.register("current")
        m.begin_drain()  # current participants carry into generation 1
        # A nonce left over from an older generation (e.g. a pre-carry registry
        # entry or a previous pod epoch) must not ACK/decrement.
        m.participants["ghost"] = 0
        with self.assertRaises(StaleNonce):
            m.ack("ghost")
        with self.assertRaises(StaleNonce):
            m.decrement_accepted("ghost", "rest")
        with self.assertRaises(StaleNonce):
            m.ack("never-registered")

    def test_accepted_never_goes_negative(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.register("p1")
        m.admit("rest")
        m.begin_drain()
        m.decrement_accepted("p1", "rest")
        with self.assertRaises(ValueError):
            m.decrement_accepted("p1", "rest")

    def test_crash_records_aborted_active_and_blocks_drain(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.register("p1")
        m.begin_drain()
        m.ack("p1")
        m.record_crash("p1")
        self.assertEqual(m.crash_record, CRASH_ABORTED_ACTIVE)
        with self.assertRaises(CrashUnreconciled):
            m.maybe_complete()
        m.reconcile_crash()
        self.assertEqual(m.maybe_complete(), STATE_QUIESCED)

    def test_boot_epoch_fail_closed_no_in_place_restart(self):
        m, _, epoch = make_machine(epoch_ok=False)
        with self.assertRaises(BootEpochViolation):
            m.serve(epoch_ok=False)
        self.assertEqual(m.state, STATE_FAILED_CLOSED)

    def test_epoch_change_mid_serving_fails_closed(self):
        m, _, epoch = make_machine()
        m.serve(epoch_ok=True)
        epoch["ok"] = False
        with self.assertRaises(BootEpochViolation):
            m.begin_drain()
        self.assertEqual(m.state, STATE_FAILED_CLOSED)

    def test_registry_frozen_at_generation_change(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        m.begin_drain()
        with self.assertRaises(InvalidTransition):
            m.register("late")

    def test_terminate_requires_quiesced(self):
        m, _, _ = make_machine()
        m.serve(epoch_ok=True)
        with self.assertRaises(InvalidTransition):
            m.terminate()
        m.register("p1")
        m.begin_drain()
        m.ack("p1")
        m.maybe_complete()
        m.terminate()
        self.assertEqual(m.state, STATE_TERMINATING)


class DeadlinePropagationTests(unittest.TestCase):
    def test_mint_produces_deadline_and_request_id(self):
        d = Deadline.mint(BUDGET_ORDINARY_READ)
        self.assertTrue(d.request_id)
        self.assertEqual(d.wire_cutoff_s, 5.0)

    def test_pg_timeouts_strictly_below_remaining_with_unwind_reserve(self):
        clock = FakeClock()
        d = Deadline.mint(BUDGET_LOCAL_MUTATION, clock=clock)
        stmt_ms, lock_ms = d.pg_timeouts_ms(clock=clock)
        self.assertLess(stmt_ms, LOCAL_MUTATION_WIRE_CUTOFF_S * 1000)
        self.assertLessEqual(stmt_ms, LOCAL_MUTATION_WIRE_CUTOFF_S * 1000 - 50)
        self.assertLess(lock_ms, stmt_ms)

    def test_http_timeout_never_widens_receiver_cap(self):
        clock = FakeClock()
        d = Deadline.mint(BUDGET_LOCAL_MUTATION, clock=clock)
        self.assertEqual(d.http_timeout_s(3.0, clock=clock), 3.0)  # cap wins
        clock.advance(9.5)  # 0.5 s remaining
        self.assertAlmostEqual(d.http_timeout_s(3.0, clock=clock), 0.5)

    def test_redis_lock_ttl_bounded_below_remaining(self):
        clock = FakeClock()
        d = Deadline.mint(BUDGET_ORDINARY_READ, clock=clock)
        self.assertLess(d.redis_lock_ttl_s(clock=clock), 5.0)

    def test_celery_wait_is_rejected_not_clamped(self):
        d = Deadline.mint(BUDGET_LOCAL_MUTATION)
        with self.assertRaises(SyncWaitProhibited):
            d.validate_wait_s(1.0)

    def test_remaining_or_abort_raises_past_zero(self):
        clock = FakeClock()
        d = Deadline.mint(BUDGET_MEMBERSHIP_PROVIDER, clock=clock)
        clock.advance(0.6)  # past the 500 ms wire cutoff
        with self.assertRaises(DeadlineExceeded):
            d.remaining_or_abort(clock=clock)

    def test_membership_budget_is_the_tightest_class(self):
        d = Deadline.mint(BUDGET_MEMBERSHIP_PROVIDER)
        self.assertEqual(d.wire_cutoff_s, 0.5)


class MutationFenceTests(unittest.TestCase):
    def test_ladder_phase_pinned_at_every_boundary(self):
        self.assertEqual(ladder_phase(0.0), "RUNNING")
        self.assertEqual(ladder_phase(PHASE_COOPERATIVE_CANCEL_S - 0.01), "RUNNING")
        self.assertEqual(ladder_phase(PHASE_COOPERATIVE_CANCEL_S), "COOPERATIVE_CANCEL")
        self.assertEqual(ladder_phase(PHASE_TERM_FENCE_S), "TERM_FENCE")
        self.assertEqual(ladder_phase(PHASE_KILL_AND_DB_FENCE_S), "KILL_AND_DB_FENCE")
        self.assertEqual(ladder_phase(PHASE_PROOF_WINDOW_S), "PROOF_WINDOW")
        self.assertEqual(ladder_phase(PHASE_WIRE_CUTOFF_S), "WIRE_CUTOFF")
        self.assertEqual(ladder_phase(99.0), "WIRE_CUTOFF")

    def test_reservation_key_shape(self):
        key = reservation_key("agent:sato", "PATCH /items/", "idem-1", "digest-1")
        self.assertEqual(key, "agent:sato:PATCH /items/:idem-1:digest-1")

    def test_active_never_authorizes_second_execution(self):
        fence = MutationAttemptFence(reservation_key("p", "op", "i", "d"), "nonce-1")
        with self.assertRaises(DoubleExecution):
            fence.authorize_second_execution()

    def test_fencing_unproven_still_blocks(self):
        fence = MutationAttemptFence(reservation_key("p", "op", "i", "d"), "nonce-1")
        fence.begin_abort()
        with self.assertRaises(ProofMiss):
            fence.prove_abort(
                worker_nonce_dead=True, backend_absent=True,
                receipt_effect_agrees=False,
            )
        self.assertEqual(fence.status, ATTEMPT_FENCING_UNPROVEN)
        with self.assertRaises(DoubleExecution):
            fence.authorize_second_execution()

    def test_aborted_proven_requires_all_three_proof_elements(self):
        fence = MutationAttemptFence(reservation_key("p", "op", "i", "d"), "nonce-1")
        fence.begin_abort()
        status = fence.prove_abort(
            worker_nonce_dead=True, backend_absent=True, receipt_effect_agrees=True,
        )
        self.assertEqual(status, "ABORTED_PROVEN")
        # after ABORTED_PROVEN a same-key retry is eligible
        fence.authorize_second_execution()

    def test_terminal_body_at_wire_cutoff_requires_proof(self):
        fence = MutationAttemptFence(reservation_key("p", "op", "i", "d"), "nonce-1")
        with self.assertRaises(TerminalBodyUnproven):
            fence.terminal_body(at_or_after_wire_cutoff=True,
                                commit_or_rollback_proof=False)
        fence.mark_rollback()
        self.assertEqual(
            fence.terminal_body(at_or_after_wire_cutoff=True,
                                commit_or_rollback_proof=True),
            "ROLLED_BACK",
        )

    def test_terminal_body_before_cutoff_unblocked(self):
        fence = MutationAttemptFence(reservation_key("p", "op", "i", "d"), "nonce-1")
        self.assertEqual(fence.terminal_body(at_or_after_wire_cutoff=False,
                                             commit_or_rollback_proof=False),
                         ATTEMPT_ACTIVE)


if __name__ == "__main__":
    unittest.main()
