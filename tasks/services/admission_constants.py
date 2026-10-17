"""PLAT-8195 §5.1 budget classes / wire cutoff — single source of record.

Every admission budget and phase-ladder boundary lives here. The §10 gate
binds this surface: ANY widening of the 10 s / 15 s / 20 s / 30 s / 150 s
constants requires Architecture re-review (reference-constants lint). The
``validate_reference_constants`` helper is imported by the test suite so a
widening that breaks the structural invariants fails CI even before review.
"""

from __future__ import annotations

# --- §5.1 budget classes (cooperative budget, wire cutoff) -----------------
# Wire cutoff = the last instant the pod may hold the client transport. A
# typed 503 is emitted only after the provider unwinds; no success-shaped or
# retryable terminal body may be written without commit/rollback proof.
MEMBERSHIP_PROVIDER_COOPERATIVE_S = 0.350
MEMBERSHIP_PROVIDER_WIRE_CUTOFF_S = 0.500

ORDINARY_READ_COOPERATIVE_S = 4.5
ORDINARY_READ_WIRE_CUTOFF_S = 5.0

LOCAL_MUTATION_COOPERATIVE_S = 8.0
LOCAL_MUTATION_WIRE_CUTOFF_S = 10.0

# Work that cannot finish inside the local-mutation wire cutoff is rejected
# to 202 + durable operation ID at admission time.
ASYNC_REJECT_THRESHOLD_S = LOCAL_MUTATION_WIRE_CUTOFF_S

# --- §5.4 mutation phase ladder boundaries (seconds since dispatch) --------
# RUNNING → COOPERATIVE_CANCEL → TERM_FENCE → KILL_AND_DB_FENCE →
# PROOF_WINDOW → WIRE_CUTOFF. Strictly increasing; pinned by tests.
PHASE_COOPERATIVE_CANCEL_S = 8.0
PHASE_TERM_FENCE_S = 8.25
PHASE_KILL_AND_DB_FENCE_S = 8.75
PHASE_PROOF_WINDOW_S = 9.75
PHASE_WIRE_CUTOFF_S = 10.0

# --- §10-gated reference constants (widening requires Architecture re-review)
REFERENCE_DRAIN_ACK_BUDGET_S = 15.0
REFERENCE_QUIESCE_BUDGET_S = 20.0
REFERENCE_TERMINATE_BUDGET_S = 30.0
REFERENCE_OBSERVER_RELIST_BUDGET_S = 150.0

# §5.2 unwind reserve: downstream PostgreSQL timeouts sit strictly below the
# remaining budget by at least this much so the handler can still unwind and
# write a typed terminal body before the wire cutoff.
PG_UNWIND_RESERVE_MS = 50.0

_LADDER = (
    PHASE_COOPERATIVE_CANCEL_S,
    PHASE_TERM_FENCE_S,
    PHASE_KILL_AND_DB_FENCE_S,
    PHASE_PROOF_WINDOW_S,
    PHASE_WIRE_CUTOFF_S,
)

_BUDGET_CLASSES = (
    (MEMBERSHIP_PROVIDER_COOPERATIVE_S, MEMBERSHIP_PROVIDER_WIRE_CUTOFF_S),
    (ORDINARY_READ_COOPERATIVE_S, ORDINARY_READ_WIRE_CUTOFF_S),
    (LOCAL_MUTATION_COOPERATIVE_S, LOCAL_MUTATION_WIRE_CUTOFF_S),
)

_REFERENCE_CONSTANTS = (
    REFERENCE_DRAIN_ACK_BUDGET_S,
    REFERENCE_QUIESCE_BUDGET_S,
    REFERENCE_TERMINATE_BUDGET_S,
    REFERENCE_OBSERVER_RELIST_BUDGET_S,
)


def validate_reference_constants() -> None:
    """Structural §10 lint: raise on any invariant-breaking constant edit.

    - the phase ladder is strictly increasing and ends at the mutation wire
      cutoff;
    - every budget class has cooperative < wire cutoff;
    - the mutation ladder's cooperative cancel equals the local-mutation
      cooperative budget and its wire cutoff equals the local-mutation wire
      cutoff (one source of truth, not two).
    """
    for earlier, later in zip(_LADDER, _LADDER[1:]):
        if not later > earlier:
            raise ValueError(
                f"phase ladder must be strictly increasing: {earlier} !< {later}"
            )
    if _LADDER[-1] != LOCAL_MUTATION_WIRE_CUTOFF_S:
        raise ValueError("ladder WIRE_CUTOFF must equal the local-mutation wire cutoff")
    if _LADDER[0] != LOCAL_MUTATION_COOPERATIVE_S:
        raise ValueError(
            "ladder COOPERATIVE_CANCEL must equal the local-mutation cooperative budget"
        )
    for coop, wire in _BUDGET_CLASSES:
        if not wire > coop > 0:
            raise ValueError(f"budget class invalid: cooperative={coop} wire={wire}")
    for value in _REFERENCE_CONSTANTS:
        if value <= 0:
            raise ValueError(f"reference constant must be positive: {value}")
