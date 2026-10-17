"""PLAT-8195 §5.2 deadline propagation helpers — pure, dependency-free.

``Deadline.mint(budget_class)`` produces the monotonic absolute deadline and
the request ID at the gateway. Every downstream timeout derives from the
deadline with the NEVER-WIDEN invariant held absolutely:

- ``pg_timeouts_ms()`` — statement_timeout / lock_timeout strictly below the
  remaining budget (50 ms unwind reserve), rollback-only on cancel;
- ``http_timeout_s(cap)`` — min(remaining, cap): a receiver's own cap is
  never widened by a generous upstream budget;
- ``redis_lock_ttl_s()`` — bounded below the remaining budget;
- ``validate_wait_s()`` — enforces the Celery-wait PROHIBITION in sync
  handlers by REJECTING (never clamping — a clamped wait would silently do
  partial work);
- ``remaining_or_abort()`` — raises at/past zero so a handler never starts
  work it cannot finish inside the wire cutoff.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from .admission_constants import (
    ASYNC_REJECT_THRESHOLD_S,
    LOCAL_MUTATION_WIRE_CUTOFF_S,
    MEMBERSHIP_PROVIDER_WIRE_CUTOFF_S,
    ORDINARY_READ_WIRE_CUTOFF_S,
    PG_UNWIND_RESERVE_MS,
)

BUDGET_MEMBERSHIP_PROVIDER = "membership_provider"
BUDGET_ORDINARY_READ = "ordinary_read"
BUDGET_LOCAL_MUTATION = "local_mutation"

_WIRE_CUTOFFS = {
    BUDGET_MEMBERSHIP_PROVIDER: MEMBERSHIP_PROVIDER_WIRE_CUTOFF_S,
    BUDGET_ORDINARY_READ: ORDINARY_READ_WIRE_CUTOFF_S,
    BUDGET_LOCAL_MUTATION: LOCAL_MUTATION_WIRE_CUTOFF_S,
}


class DeadlineExceeded(Exception):
    """Raised at/past zero remaining — the handler must not start work it
    cannot finish inside the wire cutoff."""


class SyncWaitProhibited(Exception):
    """Celery waits are PROHIBITED in sync handlers (§5.2). Rejected, not
    clamped."""


class Deadline:
    """Monotonic absolute deadline minted at the gateway."""

    __slots__ = ("request_id", "budget_class", "wire_cutoff_s", "_monotonic_deadline")

    def __init__(self, request_id: str, budget_class: str, wire_cutoff_s: float,
                 monotonic_deadline: float) -> None:
        self.request_id = request_id
        self.budget_class = budget_class
        self.wire_cutoff_s = wire_cutoff_s
        self._monotonic_deadline = monotonic_deadline

    @classmethod
    def mint(cls, budget_class: str, *, clock: Optional[Callable[[], float]] = None) -> "Deadline":
        now = (clock or time.monotonic)()
        wire = _WIRE_CUTOFFS[budget_class]
        return cls(
            request_id=uuid.uuid4().hex,
            budget_class=budget_class,
            wire_cutoff_s=wire,
            monotonic_deadline=now + wire,
        )

    def remaining_s(self, *, clock: Optional[Callable[[], float]] = None) -> float:
        now = (clock or time.monotonic)()
        return self._monotonic_deadline - now

    # --- derived timeouts (never widen) ------------------------------------

    def pg_timeouts_ms(self, *, clock: Optional[Callable[[], float]] = None) -> tuple:
        """(statement_timeout_ms, lock_timeout_ms) strictly below remaining,
        minus the unwind reserve. Rollback-only on cancel."""
        remaining_ms = max(self.remaining_s(clock=clock) * 1000.0 - PG_UNWIND_RESERVE_MS, 0.0)
        # lock timeout is a fraction of statement timeout so a lock wait fails
        # before the statement burns the whole budget.
        lock_ms = max(remaining_ms / 2.0, 0.0)
        return int(remaining_ms), int(lock_ms)

    def http_timeout_s(self, cap_s: float, *, clock: Optional[Callable[[], float]] = None) -> float:
        """min(remaining, cap): never widen a receiver's own cap."""
        return min(self.remaining_s(clock=clock), cap_s)

    def redis_lock_ttl_s(self, *, clock: Optional[Callable[[], float]] = None) -> float:
        """Bounded strictly below the remaining budget (half, floor 0.05 s)."""
        return max(self.remaining_s(clock=clock) / 2.0, 0.05)

    def validate_wait_s(self, requested_s: float, *, clock: Optional[Callable[[], float]] = None) -> float:
        """Celery-wait PROHIBITION: any wait in a sync handler is rejected —
        never clamped (a clamped wait would silently do partial work)."""
        raise SyncWaitProhibited(
            f"Celery/broker wait of {requested_s}s prohibited in sync handler "
            f"(request {self.request_id}); use 202 + durable operation ID "
            f"(threshold {ASYNC_REJECT_THRESHOLD_S}s)"
        )

    def remaining_or_abort(self, *, clock: Optional[Callable[[], float]] = None) -> float:
        remaining = self.remaining_s(clock=clock)
        if remaining <= 0:
            raise DeadlineExceeded(
                f"request {self.request_id} past wire cutoff ({self.wire_cutoff_s}s)"
            )
        return remaining

    def exceeds_async_threshold(self) -> bool:
        return self.wire_cutoff_s > ASYNC_REJECT_THRESHOLD_S
