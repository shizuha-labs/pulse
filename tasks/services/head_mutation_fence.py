"""PLAT-9838 — lifetime head-mutation fencing (pair for PLAT-9837).

Composes with PLAT-9160/#641 (mint-time provenance fence): #641 proves the
mint's merge commit IS the live head at mint time; this module keeps that
invariant true for the LIFETIME of the mint. A correlation-key mint
(approval mint, merge-authority receipt, verification-requirement manifest)
pins the head it was minted against. If that head is later mutated —
force-push, re-anchor, successor PR — honoring the outstanding mint
silently would settle it against a superseded head: the CTX-650
drift-catch pattern, generalized to every mint.

Contract (mirrors the security-ci PLAT-8988 discipline and #641):
- equality only: citations and mutations are literal, complete values —
  no ranges, no heuristics, no caller-pinned "any";
- deny-default: a fenced or never-cited mint never settles silently;
- every fence is loud: each invalidated mint yields a typed
  ``mint_fenced_stale[event]`` with old/new heads plus an audit warning;
- surgical: fencing is scoped to the PR the mutation happened on — no
  cross-PR fencing;
- ADR-0090 posture: the fence RECORDS the event (taxonomy, old/new heads)
  and stops there. Re-scope semantics (re-anchor or typed-reject) belong
  to the ADR-0090 owner; the fence never invents re-scope itself. A
  re-frozen mint lands as a NEW registration.

Non-duplication: the registry is in-process. Persistence of mint
references rides the existing requirement-manifest records — this module
adds no second store.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Closed taxonomy of head-mutation events. An open set would let an
# unknown event shape fence nothing and be honored silently — unknown
# names deny.
HEAD_MUTATION_EVENTS = frozenset({"force_push", "re_anchor", "successor_pr"})

#: Typed code for a mint whose cited head was superseded by a mutation.
MINT_FENCED_STALE = "mint_fenced_stale"
#: Typed code for a mint that settled without ever citing a live head.
MINT_UNREGISTERED = "mint_unregistered"


class MintFencingError(Exception):
    """Typed stale-mint denial (PLS-1018 deny-default).

    ``code`` names the dimension — ``mint_fenced_stale`` (a head-mutation
    event fenced this mint's cited head) or ``mint_unregistered`` (no
    live-head citation was ever minted). ``detail`` is load-bearing: exact
    event, repo, PR, old/new heads — the caller's retry path reads it.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


# In-process citation registry: mint_id -> record. A record is
# {"repo", "pr", "head", "fenced", "fence"}; fenced entries keep their
# fence record so the settlement deny can name the mutation.
_MINTS: Dict[str, Dict[str, Any]] = {}


def register_mint_reference(
    mint_id: Any, repo: str, pr: int, head: str,
) -> Dict[str, Any]:
    """Cite the live head at mint time (equality surface).

    - equality: repo/PR/head must be literal, complete values — no
      heuristic ranges, no partial pins;
    - idempotent per (mint_id, citation): re-citing the identical citation
      returns the same record unchanged;
    - a DIFFERENT citation for the same mint_id supersedes: a re-frozen
      mint lands as a NEW citation (and clears any prior fence). Per
      ADR-0090, anchoring is the caller's decision — never the fence's.
    """
    repo_s = str(repo or "").strip()
    head_s = str(head or "").strip()
    try:
        pr_i = int(pr)
    except (TypeError, ValueError):
        raise ValueError("mint citation requires an integer PR number")
    if not repo_s or not head_s or pr_i < 1:
        raise ValueError(
            "mint citation requires literal repo, PR number and head SHA",
        )
    key = str(mint_id)
    existing = _MINTS.get(key)
    if (
        existing is not None
        and existing["repo"] == repo_s
        and existing["pr"] == pr_i
        and existing["head"] == head_s
    ):
        return existing  # exact re-cite: idempotent no-op
    record: Dict[str, Any] = {
        "repo": repo_s,
        "pr": pr_i,
        "head": head_s,
        "fenced": False,
        "fence": None,
    }
    _MINTS[key] = record
    return record


def fence_mints_on_head_mutation(
    repo: str, pr: int, new_head: str, event: str,
) -> List[Dict[str, Any]]:
    """Fence outstanding mints citing the superseded head. Never silent.

    Consumes one head-mutation event and invalidates every outstanding
    mint citing the superseded head on THIS PR. Returns the list of
    fenced records (each carrying the typed ``mint_fenced_stale`` fence
    with old/new heads); emits an audit warning per fenced mint (the
    every-drop-is-loud contract). Unknown event names raise — an unclosed
    taxonomy would honor silence. Already-fenced entries are idempotent:
    no duplicate fence, no duplicate audit line.
    """
    event_s = str(event or "").strip()
    if event_s not in HEAD_MUTATION_EVENTS:
        raise ValueError(
            "unknown head-mutation event %r (expected one of %s)"
            % (event_s, sorted(HEAD_MUTATION_EVENTS)),
        )
    repo_s = str(repo or "").strip()
    try:
        pr_i = int(pr)
    except (TypeError, ValueError):
        raise ValueError("head-mutation event requires an integer PR number")
    new_s = str(new_head or "").strip()
    fenced: List[Dict[str, Any]] = []
    for key in sorted(_MINTS):
        record = _MINTS[key]
        if record["fenced"]:
            continue  # idempotent: already fenced
        if record["repo"] != repo_s or record["pr"] != pr_i:
            continue  # scoped to this PR only
        if record["head"] == new_s:
            continue  # mutation is a no-op for this mint (head == cited)
        record["fenced"] = True
        record["fence"] = {
            "code": MINT_FENCED_STALE,
            "event": event_s,
            "old_head": record["head"],
            "new_head": new_s,
        }
        fenced.append(record)
        logger.warning(
            "PLAT-9838 %s[%s]: mint %s cites superseded head %s… "
            "(repo=%s pr=%s; live head is now %s…)",
            MINT_FENCED_STALE, event_s, key, record["head"][:12],
            repo_s, pr_i, new_s[:12],
        )
    return fenced


def cite_live_head(mint_id: Any, repo: str, pr: int, head: str, *, previous_head: str = "") -> Dict[str, Any]:
    """Mint-path entry: fence a superseded citation, then cite the live head.

    Called when a correlation-key mint is created or refreshed (pulse_link_pr
    head refresh). A changed previous head is a re-anchor, not a silent
    overwrite. The new citation is a new registration and clears the fence.
    """
    previous = str(previous_head or "").strip()
    live = str(head or "").strip()
    if previous and previous != live:
        fence_mints_on_head_mutation(repo, pr, live, "re_anchor")
    return register_mint_reference(mint_id, repo, pr, live)


def assert_mint_live(mint_id: Any) -> Dict[str, Any]:
    """Settlement-side liveness check (deny-default).

    - fenced mint → MintFencingError(MINT_FENCED_STALE, ...) with the
      recorded event and old/new heads;
    - never-cited mint → MintFencingError(MINT_UNREGISTERED, ...);
    - live cited mint → returns its record. Never silent honor.
    """
    record = _MINTS.get(str(mint_id))
    if record is None:
        raise MintFencingError(
            MINT_UNREGISTERED,
            "mint %s settled without a live-head citation; re-mint citing "
            "the backend-verified head" % (mint_id,),
        )
    if record["fenced"]:
        fence = record["fence"]
        raise MintFencingError(
            MINT_FENCED_STALE,
            "mint %s cites head %s… superseded by %s… (event=%s); "
            "re-anchor to the live head to settle"
            % (
                mint_id, fence["old_head"][:12],
                fence["new_head"][:12], fence["event"],
            ),
        )
    return record
