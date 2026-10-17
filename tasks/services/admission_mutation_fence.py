"""PLAT-8195 §5.3–5.4 durable attempt fence + phase ladder — pure state machine.

The Django ``MutationAttempt`` model (Phase C wiring) persists reservations
and calls into this machine. Contracts pinned here:

- Reservation is keyed principal+operation+idempotency+request digest BEFORE
  dispatch.
- ``ACTIVE`` / ``FENCING_UNPROVEN`` never authorize a second same-key
  execution.
- ``ABORTED_PROVEN`` requires ALL THREE proof elements: worker-nonce death,
  backend/transaction absence, receipt/effect agreement. A proof miss moves
  the attempt to ``FENCING_UNPROVEN`` (still blocking) and raises ProofMiss —
  the correctness incident.
- Terminal bodies at/after WIRE_CUTOFF require commit/rollback proof.
- ``ladder_phase(elapsed)`` is pinned at every §5.4 boundary.
"""

from __future__ import annotations

from typing import Optional

from .admission_constants import (
    PHASE_COOPERATIVE_CANCEL_S,
    PHASE_KILL_AND_DB_FENCE_S,
    PHASE_PROOF_WINDOW_S,
    PHASE_TERM_FENCE_S,
    PHASE_WIRE_CUTOFF_S,
)

PHASE_RUNNING = "RUNNING"
PHASE_COOPERATIVE_CANCEL = "COOPERATIVE_CANCEL"
PHASE_TERM_FENCE = "TERM_FENCE"
PHASE_KILL_AND_DB_FENCE = "KILL_AND_DB_FENCE"
PHASE_PROOF_WINDOW = "PROOF_WINDOW"
PHASE_WIRE_CUTOFF = "WIRE_CUTOFF"

ATTEMPT_ACTIVE = "ACTIVE"
ATTEMPT_COOPERATIVE_CANCEL = "COOPERATIVE_CANCEL"
ATTEMPT_FENCING = "FENCING"
ATTEMPT_FENCING_UNPROVEN = "FENCING_UNPROVEN"
ATTEMPT_ABORTED_PROVEN = "ABORTED_PROVEN"
ATTEMPT_COMMITTED = "COMMITTED"
ATTEMPT_ROLLED_BACK = "ROLLED_BACK"


class FenceError(Exception):
    pass


class DoubleExecution(FenceError):
    """A second same-key execution was attempted while the first is
    ACTIVE/FENCING_UNPROVEN — never authorized."""


class ProofMiss(FenceError):
    """Correctness incident: the fence could not prove the aborted attempt's
    effect absence. Pages; the attempt stays FENCING_UNPROVEN (blocking)."""


class TerminalBodyUnproven(FenceError):
    """A success-shaped or retryable terminal body was attempted at/after the
    wire cutoff without commit/rollback proof."""


def ladder_phase(elapsed_s: float) -> str:
    """Pin the §5.4 phase ladder at every boundary (strictly increasing)."""
    if elapsed_s < PHASE_COOPERATIVE_CANCEL_S:
        return PHASE_RUNNING
    if elapsed_s < PHASE_TERM_FENCE_S:
        return PHASE_COOPERATIVE_CANCEL
    if elapsed_s < PHASE_KILL_AND_DB_FENCE_S:
        return PHASE_TERM_FENCE
    if elapsed_s < PHASE_PROOF_WINDOW_S:
        return PHASE_KILL_AND_DB_FENCE
    if elapsed_s < PHASE_WIRE_CUTOFF_S:
        return PHASE_PROOF_WINDOW
    return PHASE_WIRE_CUTOFF


def reservation_key(principal: str, operation: str, idempotency_key: str,
                    request_digest: str) -> str:
    """The §5.3 reservation key — minted BEFORE dispatch."""
    return f"{principal}:{operation}:{idempotency_key}:{request_digest}"


class MutationAttemptFence:
    """In-memory state machine for one reservation key. The durable model
    persists the same transitions."""

    def __init__(self, key: str, worker_nonce: str) -> None:
        self.key = key
        self.worker_nonce = worker_nonce
        self.status: str = ATTEMPT_ACTIVE
        self.proof: Optional[dict] = None

    # --- authorization -----------------------------------------------------

    def authorize_second_execution(self) -> None:
        """A concurrent same-key retry asks this. ACTIVE/FENCING_UNPROVEN
        never authorize a second execution."""
        if self.status in (ATTEMPT_ACTIVE, ATTEMPT_FENCING_UNPROVEN):
            raise DoubleExecution(
                f"attempt {self.key} is {self.status}; concurrent same-key "
                "execution is never authorized (§5.3)"
            )
        return None

    # --- abort / proof -----------------------------------------------------

    def begin_abort(self) -> str:
        if self.status != ATTEMPT_ACTIVE:
            raise FenceError(f"begin_abort from {self.status}")
        self.status = ATTEMPT_FENCING
        return PHASE_KILL_AND_DB_FENCE

    def prove_abort(
        self,
        *,
        worker_nonce_dead: bool,
        backend_absent: bool,
        receipt_effect_agrees: bool,
    ) -> str:
        """ABORTED_PROVEN requires ALL THREE proof elements. Any miss moves to
        FENCING_UNPROVEN (still blocking) and raises ProofMiss."""
        if self.status != ATTEMPT_FENCING:
            raise FenceError(f"prove_abort from {self.status}")
        self.proof = {
            "worker_nonce_dead": worker_nonce_dead,
            "backend_absent": backend_absent,
            "receipt_effect_agrees": receipt_effect_agrees,
        }
        if not (worker_nonce_dead and backend_absent and receipt_effect_agrees):
            self.status = ATTEMPT_FENCING_UNPROVEN
            raise ProofMiss(
                f"attempt {self.key} proof incomplete: {self.proof} — "
                "correctness incident, attempt stays FENCING_UNPROVEN (blocking)"
            )
        self.status = ATTEMPT_ABORTED_PROVEN
        return self.status

    # --- terminal bodies ---------------------------------------------------

    def terminal_body(self, *, at_or_after_wire_cutoff: bool,
                      commit_or_rollback_proof: bool) -> str:
        """At/after WIRE_CUTOFF a terminal body requires commit/rollback
        proof; no success-shaped or retryable body without it."""
        if at_or_after_wire_cutoff and not commit_or_rollback_proof:
            raise TerminalBodyUnproven(
                f"attempt {self.key}: terminal body at/after WIRE_CUTOFF "
                "requires commit/rollback proof (§5.4)"
            )
        if not at_or_after_wire_cutoff:
            return self.status
        if self.status == ATTEMPT_ACTIVE:
            raise FenceError("wire cutoff reached with attempt still ACTIVE")
        return self.status

    def mark_commit(self) -> None:
        if self.status not in (ATTEMPT_ACTIVE, ATTEMPT_FENCING):
            raise FenceError(f"mark_commit from {self.status}")
        self.status = ATTEMPT_COMMITTED

    def mark_rollback(self) -> None:
        if self.status not in (ATTEMPT_ACTIVE, ATTEMPT_FENCING):
            raise FenceError(f"mark_rollback from {self.status}")
        self.status = ATTEMPT_ROLLED_BACK
