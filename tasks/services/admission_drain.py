"""PLAT-8195 §8.2 drain state machine — pure, dependency-free, unit-testable.

States: STARTING → SERVING → DRAINING → QUIESCED → TERMINATING, plus
FAILED_CLOSED. All I/O (broadcasts, epoch checks) is injected as callables so
the invariants are testable without a cluster:

- SERVING→DRAINING is ONE atomic generation change: admission closes and the
  CLOSE_ADMISSION(generation) broadcast fires in the same step — no
  open-admission window.
- Drain completes only when every current-generation participant has ACKed
  and both accepted sets (REST + MCP) are at zero.
- Stale nonces cannot ACK or decrement a newer generation's leases; accepted
  counts never go negative.
- A crash during drain records ``aborted_active`` (never silently
  decremented) and blocks the drain until reconciled.
- Boot-epoch fail-closed: no in-place restart to SERVING; a replaced pod with
  a NEW epoch may serve.
- The participant registry freezes at the generation change.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Set

STATE_STARTING = "STARTING"
STATE_SERVING = "SERVING"
STATE_DRAINING = "DRAINING"
STATE_QUIESCED = "QUIESCED"
STATE_TERMINATING = "TERMINATING"
STATE_FAILED_CLOSED = "FAILED_CLOSED"

CRASH_RECONCILED = "reconciled"
CRASH_ABORTED_ACTIVE = "aborted_active"


class DrainError(Exception):
    """Base error for drain state-machine violations."""


class InvalidTransition(DrainError):
    pass


class StaleNonce(DrainError):
    pass


class CrashUnreconciled(DrainError):
    pass


class BootEpochViolation(DrainError):
    pass


class AdmissionDrainStateMachine:
    """Pure §8.2 drain machine.

    ``broadcast_close_admission`` is called exactly once per DRAINING entry
    with the new generation (injected — the real gateway owns the UDS wire).
    ``epoch_matches`` is injected and must return True only while the boot
    epoch recorded at STARTING still matches the live epoch file.
    """

    def __init__(
        self,
        broadcast_close_admission: Callable[[int], None],
        epoch_matches: Callable[[], bool],
    ) -> None:
        self._broadcast = broadcast_close_admission
        self._epoch_matches = epoch_matches
        self.state: str = STATE_STARTING
        self.generation: int = 0
        # participant nonce -> generation it registered under
        self.participants: Dict[str, int] = {}
        self.registry_frozen: bool = False
        self.accepted_rest: int = 0
        self.accepted_mcp: int = 0
        self.acked: Set[str] = set()
        self.crash_record: Optional[str] = None

    # --- lifecycle ---------------------------------------------------------

    def serve(self, epoch_ok: bool) -> None:
        if self.state != STATE_STARTING:
            raise InvalidTransition(f"serve from {self.state}")
        if not epoch_ok:
            self.state = STATE_FAILED_CLOSED
            raise BootEpochViolation("boot epoch missing/mismatched at STARTING")
        self.state = STATE_SERVING

    def begin_drain(self) -> int:
        """Atomically SERVING → DRAINING: close admission + broadcast in one
        step. Returns the new generation."""
        if self.state != STATE_SERVING:
            raise InvalidTransition(f"begin_drain from {self.state}")
        if not self._epoch_matches():
            self.state = STATE_FAILED_CLOSED
            raise BootEpochViolation("boot epoch changed before drain")
        self.generation += 1
        self.registry_frozen = True  # registry freezes at the generation change
        # Participants registered while SERVING are the drain participants:
        # carry them into the new generation (they are exactly who must ACK).
        # Stale nonces are unknown/non-current nonces only.
        self.participants = {n: self.generation for n in self.participants}
        self.state = STATE_DRAINING
        # Same-step broadcast: no code path may admit between the close and
        # the broadcast — both happen inside this atomic method.
        self._broadcast(self.generation)
        return self.generation

    # --- participant surface ----------------------------------------------

    def register(self, nonce: str) -> None:
        if self.registry_frozen:
            raise InvalidTransition("registry frozen at generation change")
        self.participants[nonce] = self.generation

    def ack(self, nonce: str) -> None:
        self._require_current(nonce)
        self.acked.add(nonce)

    def decrement_accepted(self, nonce: str, set_name: str) -> None:
        """Participant finished an accepted op (set_name: 'rest' | 'mcp')."""
        self._require_current(nonce)
        attr = "accepted_rest" if set_name == "rest" else "accepted_mcp"
        value = getattr(self, attr)
        if value <= 0:
            raise ValueError(f"{attr} would go negative")
        setattr(self, attr, value - 1)

    def admit(self, set_name: str) -> None:
        if self.state != STATE_SERVING:
            raise InvalidTransition("admission closed")
        attr = "accepted_rest" if set_name == "rest" else "accepted_mcp"
        setattr(self, attr, getattr(self, attr) + 1)

    # --- crash / completion ------------------------------------------------

    def record_crash(self, nonce: str) -> None:
        """A participant died mid-drain: record aborted_active. Its lease is
        NEVER silently decremented — the drain stays blocked until an
        operator reconciles."""
        self._require_current(nonce)
        self.crash_record = CRASH_ABORTED_ACTIVE

    def reconcile_crash(self) -> None:
        if self.crash_record != CRASH_ABORTED_ACTIVE:
            raise DrainError("no aborted_active crash to reconcile")
        self.crash_record = CRASH_RECONCILED

    def maybe_complete(self) -> str:
        if self.state != STATE_DRAINING:
            return self.state
        if self.crash_record == CRASH_ABORTED_ACTIVE:
            raise CrashUnreconciled("drain blocked: aborted_active crash unreconciled")
        current = {n for n, g in self.participants.items() if g == self.generation}
        if not current.issubset(self.acked):
            return self.state
        if self.accepted_rest != 0 or self.accepted_mcp != 0:
            return self.state
        self.state = STATE_QUIESCED
        return self.state

    def terminate(self) -> None:
        if self.state != STATE_QUIESCED:
            raise InvalidTransition(f"terminate from {self.state}")
        if not self._epoch_matches():
            self.state = STATE_FAILED_CLOSED
            raise BootEpochViolation("boot epoch changed before TERMINATING")
        self.state = STATE_TERMINATING

    def fail_closed(self, reason: str) -> None:
        self.state = STATE_FAILED_CLOSED
        self.crash_record = self.crash_record or reason

    # --- helpers -----------------------------------------------------------

    def _require_current(self, nonce: str) -> None:
        gen = self.participants.get(nonce)
        if gen is None:
            raise StaleNonce(f"unknown participant {nonce!r}")
        if gen != self.generation:
            raise StaleNonce(
                f"participant {nonce!r} registered at generation {gen}, "
                f"current is {self.generation} — stale nonces cannot ACK/decrement"
            )
