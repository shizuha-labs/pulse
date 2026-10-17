"""Canonical-identity resolution for roster emails (PLS-1164).

Root cause class (PLS-1154 audit): routing rosters are operator-authored and
have accumulated emails that look like principals but resolve to no shizuha-id
user (11 verified phantom ``@shizuha.com`` rows). The local-part domain-swap
alias helper (``WorkflowService._agent_email_aliases``) only fixes domain
variants of a CORRECT local-part; a wrong-local-part label silently matches
nothing and every consumer treats it as a zero — the "phantom identity" class.

The rule (sharper than hypothesized, from the PLS-1154 audit): a roster email
resolves **only if it equals a principal's canonical ``User.email``** in
shizuha-id. Agent accounts canonicalize to ``@agents.shizuha.io``; the
``@shizuha.com`` spelling is an org-membership *label* that may or may not be
the canonical form (counterexample: ``rui@shizuha.com`` IS canonical — user 99
— so this is not a blanket domain rule).

This module is the single canonicalization point:

- ``canonical_principal_email`` resolves an email to its principal's canonical
  address through ``UserService`` (shizuha-id is the ONLY principal authority —
  ADR-0016 split respected; no second identity source is created here).
- ``canonicalize_roster_members`` maps a roster's member emails to canonical
  forms and classifies every email as already-canonical, canonicalizable
  (domain variant of a real principal), phantom (no principal), or
  resolution-error (shizuha-id unreachable — NEVER treated as phantom).

Fail-loud contract (PLS-277 §6): a roster email that resolves to no principal
is a named, observable reason class (``phantom_roster_identity``), never a
silent zero.
"""

from __future__ import annotations

import logging
import threading
import time

logger = __import__('logging').getLogger(__name__)

# Named reason class per PLS-277 §6 / PLS-1164 acceptance.
REASON_PHANTOM_ROSTER_IDENTITY = 'phantom_roster_identity'

# How long a canonicalized-roster decision is reused before re-resolving.
# Bounded and short: identity edits take effect within a minute, and a
# shizuha-id outage degrades to the pre-PLS-1164 alias behavior after TTL.
_ROSTER_CANON_TTL_SECONDS = 60


def canonical_principal_email(email, *, allow_stale=False):
    """Return the principal's canonical email for ``email``, or None.

    Resolution goes through ``UserService.get_user_by_email`` (shizuha-id
    internal by-email API + alias retry + PLS-472 bounded last-known-good
    cache). The returned address is the principal's OWN canonical ``email``
    field — not the queried spelling — so downstream by-email set membership
    compares canonical principals only.

    ``None`` means: shizuha-id has no principal for this email (or, only when
    ``allow_stale`` is also False and the lookup errored, the authority was
    unreachable). Callers MUST distinguish phantom (verified no principal)
    from resolution-error (authority unreachable) via
    ``classify_roster_email`` — never treat an outage as a phantom.
    """
    if not email or not str(email).strip():
        return None
    try:
        user = _raw_principal_lookup(str(email).strip())
    except Exception:  # defensive: resolver must never raise into routing paths
        logger.warning(
            'canonical_principal_email: shizuha-id unreachable for %s', email,
            exc_info=True,
        )
        return None
    if not user:
        return None
    canonical = (user.get('email') or '').strip().lower()
    return canonical or None


def classify_roster_email(email, *, allow_stale=False):
    """Classify one roster email against the principal authority.

    Returns ``(status, canonical_email)`` where status is one of:
      - ``'canonical'``: the email IS a principal's canonical email.
      - ``'canonicalizable'``: a principal exists but under a different
        canonical email (domain variant or corrected local-part) — the
        returned canonical email is what should be stored/matched instead.
      - ``'phantom'``: shizuha-id has NO principal for this email (or any
        alias form). Named reason class: ``phantom_roster_identity``.
      - ``'resolution_error'``: the authority was unreachable — unknown, NOT
        phantom. Callers must fail open (keep existing behavior), never
        reject a write or flag a seat on an outage.
    """
    norm = (email or '').strip().lower()
    if not norm:
        return 'phantom', None
    cached = _classify_cache.get(norm)
    if cached is not None:
        return cached
    try:
        user = _raw_principal_lookup(norm)
    except Exception:
        logger.warning(
            'classify_roster_email: shizuha-id unreachable for %s — '
            'resolution_error (fail-open)', norm, exc_info=True,
        )
        return 'resolution_error', None
    if not user:
        result = ('phantom', None)
    else:
        canonical = (user.get('email') or '').strip().lower()
        if not canonical:
            # A principal row without an email is unusable for by-email matching.
            result = ('phantom', None)
        elif canonical == norm:
            result = ('canonical', canonical)
        else:
            result = ('canonicalizable', canonical)
    _classify_cache.set(norm, result)
    return result


def _raw_principal_lookup(email):
    """Raw shizuha-id by-email lookup with alias retry.

    Uses ``UserService._get_id_user_by_email`` — the RAW internal call whose
    contract distinguishes the two cases ``get_user_by_email`` collapses:
    a 404 returns None (verified: no principal), while a transport/5xx error
    RAISES (authority unreachable). ``get_user_by_email`` swallows the error
    and returns None for both (PLS-472 serves last-known-good only when one
    exists), which would misread an identity outage as a phantom — the exact
    fail-open violation PLS-1164 forbids.
    """
    from ..user_service import UserService
    service = UserService()
    norm = (email or '').strip().lower()
    found = service._get_id_user_by_email(norm)
    if found:
        return found
    # Alias retry: the paired agent spelling (same local-part, other domain).
    alias = None
    if norm.endswith('@shizuha.com'):
        alias = norm[:-len('@shizuha.com')] + '@agents.shizuha.io'
    elif norm.endswith('@agents.shizuha.io'):
        alias = norm[:-len('@agents.shizuha.io')] + '@shizuha.com'
    if alias and alias != norm:
        return service._get_id_user_by_email(alias)
    return None


def roster_label_for_principal(canonical_email):
    """The roster STORAGE form for a principal (PLAT-4220 convention).

    PLAT-4220 (operator 2026-07-11, enforced in ``TeamRoster.save``): the
    ``*.shizuha.io`` namespace is NOT owned — roster labels for agent
    principals use the ``@shizuha.com`` spelling, and ``@agents.shizuha.io``
    labels are rejected at every write. Principal truth (canonical
    ``User.email``) lives in shizuha-id and is ``@agents.shizuha.io`` for
    agents (PLS-1154 audit); resolution-time matching maps the stored label to
    the principal (see ``_is_team_member`` / ``roster_member_canonical_map``).

    So: an agent principal whose canonical email is ``x@agents.shizuha.io`` is
    stored as ``x@shizuha.com``; any other principal (humans, e.g.
    ``rui@shizuha.com``) is stored as its canonical email verbatim.
    """
    norm = (canonical_email or '').strip().lower()
    if norm.endswith('@agents.shizuha.io'):
        return norm[:-len('@agents.shizuha.io')] + '@shizuha.com'
    return norm


def canonicalize_roster_members(tiers, junior_emails=None, *, allow_stale=False):
    """Canonicalize every member email in a roster payload to STORAGE form.

    Storage form = the PLAT-4220 roster label convention (``roster_label_for_
    principal``), NOT the principal's raw canonical email — writing the raw
    canonical ``@agents.shizuha.io`` address would be rejected by the model's
    owned-domain rule. Every stored email is verified resolvable to a real
    shizuha-id principal first.

    Returns a dict:
      ``tiers`` / ``junior_emails``: payloads with every member email rewritten
        to storage form (structure preserved).
      ``canonicalized``: {old_email: stored_email} rewrites applied.
      ``phantoms``: emails with NO principal under any spelling (reject-worthy,
        named reason class ``phantom_roster_identity``).
      ``resolution_errors``: emails unclassified because the authority was
        unreachable (fail-open: caller keeps the original email for these).
    """
    tiers = tiers or []
    junior_emails = junior_emails if junior_emails is not None else []
    canonicalized = {}
    phantoms = []
    resolution_errors = []

    def _canon_one(email):
        norm = (email or '').strip().lower()
        status, canonical = classify_roster_email(norm, allow_stale=allow_stale)
        if status == 'canonical':
            stored = roster_label_for_principal(canonical)
            if stored != norm:
                canonicalized[norm] = stored
            return stored
        if status == 'canonicalizable':
            stored = roster_label_for_principal(canonical)
            if stored != norm:
                canonicalized[norm] = stored
            return stored
        if status == 'phantom':
            phantoms.append(norm)
            return norm
        resolution_errors.append(norm)
        return norm

    new_tiers = [
        [_canon_one(m) for m in (tier or [])]
        for tier in tiers
    ]
    new_juniors = [_canon_one(m) for m in junior_emails]
    return {
        'tiers': new_tiers,
        'junior_emails': new_juniors,
        'canonicalized': canonicalized,
        'phantoms': phantoms,
        'resolution_errors': resolution_errors,
    }


class _RosterCanonCache:
    """Process-local TTL cache: (org_id, team_name) -> canonical member map.

    Bounded (maxsize, oldest-evicted) and TTL'd so roster edits propagate
    within a minute and a shizuha-id outage cannot pin stale decisions forever.
    Values are ``{'by_canonical': {canonical: original}, 'phantoms': [...],
    'errors': [...]}`` or ``None`` (meaning: resolution errored — callers fall
    back to legacy alias behavior).
    """

    def __init__(self, maxsize=256, ttl=_ROSTER_CANON_TTL_SECONDS):
        self._lock = threading.Lock()
        self._data = {}
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            value, at = entry
            if now - at > self._ttl:
                del self._data[key]
                return None
            return value

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self._maxsize:
                oldest = min(self._data, key=lambda k: self._data[k][1])
                del self._data[oldest]
            self._data[key] = (value, time.monotonic())

    def clear(self):
        with self._lock:
            self._data.clear()


_roster_canon_cache = _RosterCanonCache()
_classify_cache = _RosterCanonCache()


def roster_member_canonical_map(team_name, organization_id=None, *, item=None,
                                allow_stale=False):
    """Canonical-principal map for a team's current roster members.

    Returns ``{'by_canonical': {canonical_email: original_roster_email},
    'phantoms': [original_emails], 'errors': [original_emails]}``, or ``None``
    when resolution could not be completed (authority unreachable) — callers
    fall back to the legacy alias-swap behavior in that case.

    Cached per (org, team) with a short TTL; the roster row's ``updated_at``
    is part of the cache key so an edit invalidates immediately.
    """
    from ..models import TeamRoster
    try:
        qs = TeamRoster.objects.filter(is_active=True)
        if organization_id is not None:
            # Callers pass either the numeric org id or the org slug (the
            # item-scoped roster entry helper returns the slug) — accept both.
            if isinstance(organization_id, int):
                qs = qs.filter(organization_id=organization_id)
            else:
                qs = qs.filter(organization_slug=str(organization_id).strip().lower())
        rows = list(qs)
    except Exception:
        logger.warning('roster_member_canonical_map: roster read failed', exc_info=True)
        return None
    norm_team = TeamRoster._CANONICAL_ALIASES.get(
        (team_name or '').strip().lower(), (team_name or '').strip().lower(),
    )
    row = None
    for r in rows:
        if TeamRoster._CANONICAL_ALIASES.get(
                (r.team_name or '').strip().lower(),
                (r.team_name or '').strip().lower()) == norm_team:
            row = r
            break
    if row is None:
        # No DB authority row for this team — the caller's tiers come from
        # another source (e.g. the code-level pod roster). Return None so the
        # caller falls back to its own matching; an absent row must NOT read
        # as an authoritative empty roster.
        return None
    stamp = str(getattr(row, 'updated_at', '') or '')
    key = (getattr(row, 'organization_id', None), norm_team, stamp)
    cached = _roster_canon_cache.get(key)
    if cached is not None:
        return cached

    members = TeamRoster._flat_member_emails(row.tiers, row.junior_emails)
    by_canonical = {}
    phantoms = []
    errors = []
    for m in sorted(members):
        status, canonical = classify_roster_email(m, allow_stale=allow_stale)
        if status == 'resolution_error':
            errors.append(m)
        elif status == 'phantom':
            phantoms.append(m)
        else:
            by_canonical[canonical] = m
    result = {'by_canonical': by_canonical, 'phantoms': phantoms, 'errors': errors}
    if not errors:
        # Only cache fully-resolved maps; error results re-resolve next call.
        _roster_canon_cache.set(key, result)
    return result
