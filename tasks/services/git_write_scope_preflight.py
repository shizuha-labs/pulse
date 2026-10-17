"""PLAT-10642 — dispatch-time git write-scope preflight.

Before execution is assigned, compare the proposed seat's proven write scope
to the delivery target (base repo vs fork). Route to a reachable identity that
already has that write. If none is reachable, park with an explicit routing
note. Never discover the miss as a 403 after assignment.

Seat catalog entries (caller-supplied; this module does not mint tokens):

    {
        "email": "jun@shizuha.com",
        "reachable": true,
        "base_write": ["shizuha-labs/deploy"],
        "fork_write": ["jun/deploy"],
    }

``base_write`` means proven push + pull-request + branch update on that
repository. ``fork_write`` means proven fork-owner write on that fork
(``owner/repo``). Read-scoped tokens are omitted from both lists.

Target:

    {"repo": "shizuha-labs/deploy", "surface": "base"}
    {"repo": "shizuha-labs/deploy", "surface": "fork", "fork": "jun/deploy"}
"""
from __future__ import annotations


class GitWritePreflightError(Exception):
    """Fail-closed park. ``note`` is the routing record."""

    def __init__(self, note: str, *, reason: str):
        super().__init__(note)
        self.note = note
        self.reason = reason


def _norm_repo(value) -> str:
    return str(value or "").strip().lower().strip("/")


def _seat_email(seat) -> str:
    return str((seat or {}).get("email") or "").strip().lower()


def _reachable(seat) -> bool:
    return bool(seat) and seat.get("reachable", True) is not False and bool(_seat_email(seat))


def seat_can_deliver(seat, target) -> bool:
    """True only when this seat has proven write on the declared surface."""
    if not _reachable(seat) or not isinstance(target, dict):
        return False
    surface = str(target.get("surface") or "base").strip().lower()
    base = _norm_repo(target.get("repo"))
    fork = _norm_repo(target.get("fork"))
    base_write = {_norm_repo(r) for r in (seat.get("base_write") or [])}
    fork_write = {_norm_repo(r) for r in (seat.get("fork_write") or [])}
    if surface == "base":
        return bool(base) and base in base_write
    if surface == "fork":
        return bool(fork) and fork in fork_write
    return False


def preflight_git_write_scope(target, catalog, proposed_email):
    """Return the email to assign, or raise GitWritePreflightError to park.

    A proposed seat that can deliver is kept. Otherwise the first reachable
    catalog seat that can deliver is the direct route. No match parks.
    """
    if not isinstance(target, dict) or not _norm_repo(target.get("repo")):
        raise GitWritePreflightError(
            "git-write preflight parked: delivery target has no repo",
            reason="missing_target",
        )
    surface = str(target.get("surface") or "base").strip().lower()
    if surface not in ("base", "fork"):
        raise GitWritePreflightError(
            f"git-write preflight parked: unknown surface {surface!r}",
            reason="unknown_surface",
        )
    if surface == "fork" and not _norm_repo(target.get("fork")):
        raise GitWritePreflightError(
            "git-write preflight parked: fork delivery has no fork owner/repo",
            reason="missing_fork",
        )
    seats = [s for s in (catalog or []) if isinstance(s, dict)]
    proposed = str(proposed_email or "").strip().lower()
    for seat in seats:
        if _seat_email(seat) == proposed and seat_can_deliver(seat, target):
            return proposed
    for seat in seats:
        if seat_can_deliver(seat, target):
            return _seat_email(seat)
    checked = ", ".join(_seat_email(s) or "?" for s in seats) or "(none)"
    where = _norm_repo(target.get("fork")) if surface == "fork" else _norm_repo(target.get("repo"))
    raise GitWritePreflightError(
        "git-write preflight parked: no reachable identity has proven "
        f"{surface} write on {where}. checked: {checked}. "
        "Do not assign a read-scoped seat and discover the 403 after dispatch.",
        reason="no_proven_writer",
    )
