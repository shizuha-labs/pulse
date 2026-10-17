"""PLAT-4732 — verified Hive capability-evidence consumer (HLD PLS-277 v18 §5.2).

Pulse may authorize privileged RUNTIME-SUBSTRATE routing (kubeconfig / host-exec
/ credential-broker / bench-runner) ONLY from a verified, fresh, exact-seat
``HiveCapabilityEvidenceV2`` token — a compact **Ed25519 JWS** minted by Hive.
A plain capability set, ``PULSE_AGENT_CAPABILITIES_JSON``, an unsigned HTTP JSON
body, a team-conferred baseline, or a "signature valid" boolean can NEVER enter
the privileged decision (the PLAT-3412 regression was exactly a team-baseline
seat passing the gate without a signed grant).

Fail-closed is the only safe outcome on any doubt: missing / unreachable /
malformed / stale / wrong-binding / non-current-generation evidence is
``authority_unknown`` — authorize no new owner or transfer, preserve a valid
current owner, and never boot Mio merely to probe.

This module owns the typed request, the canonical request hash, and the strict
JWS verifier. It deliberately holds NO ownership-mutation logic; the caller
(``WorkflowService._seat_satisfies_required_capability``) maps a verdict onto
off / shadow / enforce behavior. The verifier is symmetric with the Hive
producer (HIVE-1153) via one SHA-256-pinned golden vector.

Contract: HLD PLS-277 v18 §5.2 "PLAT-4732 evidence wire and activation pin".
JOSE/Ed25519 per RFC 8037; alg/iss/aud/sub/typ validation per RFC 8725.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import jwt as _jwt
from jwt import algorithms as _jwt_algorithms

logger = logging.getLogger(__name__)

# --- Frozen wire constants (HLD v18 §5.2) --------------------------------------
EVIDENCE_ISSUER = 'shizuha-hive'
EVIDENCE_AUDIENCE = 'shizuha-pulse'
EVIDENCE_TYP = 'hive-capability-evidence+jwt'
EVIDENCE_SCHEMA = 'hive.capability-evidence.v2'
REQUEST_SCHEMA = 'pulse.capability-evidence-request.v1'
MAX_LIFETIME_SECONDS = 5
_EDDSA = 'EdDSA'
# EdDSA ONLY. Never widen; an ``alg`` allow-list is the JWT-BCP defence against
# ``none`` / HS-substitution attacks (RFC 8725).
_ALLOWED_ALGS = (_EDDSA,)
_RESULT_PRESENT = 'PRESENT'
_RESULT_ABSENT = 'ABSENT'

# --- Three-state activation control (the SOLE switch) --------------------------
MODE_OFF = 'off'
MODE_SHADOW = 'shadow'
MODE_ENFORCE = 'enforce'
_VALID_MODES = frozenset({MODE_OFF, MODE_SHADOW, MODE_ENFORCE})
# The retired boolean; its presence alongside the mode is a hard dual-config error.
_RETIRED_BOOLEAN_ENV = 'PULSE_SUBSTRATE_CAP_ENFORCE'
_MODE_ENV = 'PULSE_SUBSTRATE_CAP_MODE'


class SubstrateCapModeError(RuntimeError):
    """Raised at boot for missing-dual / unknown / conflicting mode config."""


def get_substrate_cap_mode(environ=None):
    """Resolve the sole activation switch. Boot-reject unknown / dual config.

    ``off`` preserves legacy ownership behavior while still exporting the mode,
    but it must be selected explicitly. A missing/blank switch is an ambiguous
    deployment, not an implicit opt-out. The retired boolean
    ``PULSE_SUBSTRATE_CAP_ENFORCE`` must not co-exist — a lingering value is a
    deploy error, not a second control, and fails closed by refusing to resolve.
    """
    env = os.environ if environ is None else environ
    if str(env.get(_RETIRED_BOOLEAN_ENV, '')).strip():
        raise SubstrateCapModeError(
            f'{_RETIRED_BOOLEAN_ENV} is retired and must be unset; '
            f'{_MODE_ENV}=off|shadow|enforce is the sole control')
    raw = str(env.get(_MODE_ENV, '')).strip().lower()
    if not raw:
        raise SubstrateCapModeError(
            f'missing {_MODE_ENV}; explicitly set off|shadow|enforce')
    if raw not in _VALID_MODES:
        raise SubstrateCapModeError(
            f'invalid {_MODE_ENV}={raw!r}; expected one of off|shadow|enforce')
    return raw


# --- Typed results -------------------------------------------------------------
class EvidenceAuthorityUnknown(Exception):
    """Verification failed; the seat's substrate authority is UNKNOWN.

    Carries a stable ``reason`` slug for deduplicated metrics/breadcrumbs. This
    is never "seat is incapable"; it is "we could not prove capability", which
    fails closed identically (no new owner/transfer) but is diagnosed distinctly.
    """

    def __init__(self, reason: str, detail: str = ''):
        self.reason = reason
        self.detail = detail
        super().__init__(f'{reason}: {detail}' if detail else reason)


@dataclass(frozen=True)
class VerifiedEvidence:
    """Result of a fully verified ``HiveCapabilityEvidenceV2`` token."""
    subject: str
    organization_id: int
    team_slug: str
    request_sha256: str
    required_capabilities: tuple
    present_capabilities: frozenset
    materialization_generation: str
    producer_generation_id: str
    jti: str
    # HIVE-1153 P1 #3 increment 3: the full producer tuple + absolute expiry, so a
    # selection-time receipt can revalidate the tuple + 5s freshness LOCALLY at the
    # ownership-writing commit fence with NO producer re-fetch under a DB lock.
    producer_generation_digest: str = ''
    expires_at: int = 0
    # HIVE-1153 P1 #2 (HIVE-1174 decision): the signed issued-at, the monotonic
    # ordering key for the durable per-seat materialization high-water (a newer iat
    # advances it; an older response never rewinds; equal iat + different generation
    # latches ambiguity and HOLDs).
    iat: int = 0

    def satisfies(self, required_caps) -> bool:
        """True iff every required capability is PRESENT in the signed result."""
        req = {str(c).strip().lower() for c in (required_caps or ()) if str(c).strip()}
        return req.issubset(self.present_capabilities)


# --- Canonical request ---------------------------------------------------------
def _reject_floats(obj):
    """No floats anywhere (HLD: 'no floats'); reject to keep the hash exact."""
    if isinstance(obj, float):
        raise ValueError('float is not permitted in a capability-evidence request')
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_floats(v)


def canonical_request_bytes(request: dict) -> bytes:
    """Deterministic UTF-8 bytes: recursively sorted keys, compact, no floats."""
    _reject_floats(request)
    return json.dumps(
        request, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')


def request_sha256(request: dict) -> str:
    """``sha256:<lowercase-hex>`` over the canonical request bytes."""
    return 'sha256:' + hashlib.sha256(canonical_request_bytes(request)).hexdigest()


def build_evidence_request(*, email, organization_id, team_slug, requirement_key,
                           required_capabilities, item_id, assignment_generation,
                           request_id):
    """Assemble the exact ``PulseCapabilityEvidenceRequestV1`` body.

    The caller supplies ``request_id`` (a UUID string) so the value is stable
    across the hash, the POST, and the returned-claim equality check.
    """
    return {
        'schema_version': REQUEST_SCHEMA,
        'request_id': str(request_id),
        'email': str(email).strip().lower(),
        'organization_id': int(organization_id),
        'team_slug': str(team_slug),
        'requirement_key': str(requirement_key),
        'required_capabilities': sorted({
            str(c).strip().lower() for c in required_capabilities if str(c).strip()
        }),
        'item_id': int(item_id),
        'assignment_generation': int(assignment_generation),
    }


# --- RequirementKey v2: content-addressed authorizing key (HIVE-1153 P1 #3) ----
# HLD PLS-277 v20 §5.2. The lossy `workflow_name::status` string is replaced by a
# strict typed key binding org + stable workflow/stage UUIDs + a CONTENT-DERIVED
# workflow-policy digest + the closed writer-intent. Hive echoes it verbatim; the
# Pulse consumer rebuilds the identical value to verify. No mutable/bumped column.
REQUIREMENT_KEY_V2_PREFIX = 'rk2'
WORKFLOW_POLICY_ENVELOPE_SCHEMA = 'pulse.workflow-policy.v1'
# The versioned normalizer provenance — bump when the envelope construction logic
# changes so every digest deterministically rotates with the normalizer.
WORKFLOW_POLICY_NORMALIZER_VERSION = 'pulse.workflow-policy.normalizer.v1'
# The versioned requirement CLASSIFIER provenance (ADR-0016 v7 / PLS-459): the logic
# that decides the required abstract capability vector for a stage. HLD v20 §5.2 —
# until an initialized-stage capability-policy RELATION exists, the envelope carries
# a legacy/current-stage-policy sentinel + this classifier version + provenance, so a
# classifier change rotates the policy digest and old evidence cannot authorize.
REQUIREMENT_CLASSIFIER_VERSION = 'pulse.requirement-classifier.adr-0016-v7'
REQUIREMENT_CLASSIFIER_PROVENANCE = (
    'workflow_service._task_required_capabilities+_normalize_capability_slug')
# The CLOSED set of authorizing writer intents the signed key may carry. A value
# outside this set is a bug, never a silently-accepted new authority.
ASSIGNMENT_INTENTS = frozenset({'assign', 'transfer', 'resume', 'retain', 'system'})

_SHA256_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def workflow_policy_version(envelope):
    """``sha256:<lowerhex>`` over the canonical AUTHORIZING workflow-policy
    envelope (HLD PLS-277 v20 §5.2, per aoi's ruling).

    The caller builds the envelope from the Workflow definition carrying ONLY
    authorizing/assignment-relevant fields (stable IDs, initial stage, ordered
    memberships, transition authorization fields, normalizer provenance) — never
    display names/docs/timestamps — so a display-only edit keeps the digest
    stable while any authorizing-field change rotates it. Canonicalization is the
    shared compact-sorted-UTF-8-JSON/no-floats/no-duplicate-keys form, so the
    producer and consumer agree byte-for-byte.
    """
    if not isinstance(envelope, dict) or envelope.get('schema') != WORKFLOW_POLICY_ENVELOPE_SCHEMA:
        raise ValueError(
            f'workflow policy envelope must be a dict carrying schema={WORKFLOW_POLICY_ENVELOPE_SCHEMA!r}')
    return 'sha256:' + hashlib.sha256(canonical_request_bytes(envelope)).hexdigest()


def build_requirement_key_v2(*, organization_id, workflow_id, policy_version,
                             stage_status_id, assignment_intent):
    """The strict typed RequirementKey bound into the signed evidence:

        rk2:org=<id>;wf=<uuid>;pol=sha256:<64hex>;stage=<uuid>;intent=<enum>

    Every component is validated to a delimiter-free shape (int / UUID / pinned
    sha256 / closed enum), so the `;`/`=` framing is unambiguous and no field can
    smuggle a separator. Raises ``ValueError`` on any malformed component — the
    caller fails closed rather than emit an ambiguous authority key.
    """
    org = int(organization_id)
    wf = str(workflow_id).strip().lower()
    pol = str(policy_version).strip().lower()
    stage = str(stage_status_id).strip().lower()
    intent = str(assignment_intent).strip().lower()
    if not _UUID_RE.match(wf):
        raise ValueError(f'invalid workflow_id={wf!r} (expected UUID)')
    if not _SHA256_RE.match(pol):
        raise ValueError(f'invalid policy_version={pol!r} (expected sha256:<64hex>)')
    if not _UUID_RE.match(stage):
        raise ValueError(f'invalid stage_status_id={stage!r} (expected UUID)')
    if intent not in ASSIGNMENT_INTENTS:
        raise ValueError(f'invalid assignment_intent={intent!r}; '
                         f'expected one of {sorted(ASSIGNMENT_INTENTS)}')
    return (f'{REQUIREMENT_KEY_V2_PREFIX}:org={org};wf={wf};pol={pol};'
            f'stage={stage};intent={intent}')


# --- Pinned public JWK set + current producer generation -----------------------
def _load_pinned_jwks(environ=None):
    """kid -> Ed25519 public key, from the declaratively pinned OKP JWK set.

    Only ``OKP`` / ``Ed25519`` keys are accepted; a pinned RSA/EC/oct key is a
    misconfiguration and is dropped (verification then fails unknown_kid rather
    than silently enabling a non-EdDSA path).
    """
    env = os.environ if environ is None else environ
    raw = str(env.get('PULSE_HIVE_EVIDENCE_PUBLIC_JWKS', '')).strip()
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
        keys = doc.get('keys', doc) if isinstance(doc, dict) else doc
        pinned = {}
        seen_kids = set()
        for jwk in keys or []:
            if not isinstance(jwk, dict):
                continue
            kid = jwk.get('kid')
            if kid:
                kid = str(kid)
                if kid in seen_kids:
                    raise EvidenceAuthorityUnknown('duplicate_pinned_kid', kid)
                seen_kids.add(kid)
            if jwk.get('kty') != 'OKP' or jwk.get('crv') != 'Ed25519':
                continue
            if not kid:
                continue
            pinned[kid] = _jwt_algorithms.OKPAlgorithm.from_jwk(json.dumps(jwk))
        return pinned
    except EvidenceAuthorityUnknown:
        raise
    except Exception:
        logger.exception('Invalid PULSE_HIVE_EVIDENCE_PUBLIC_JWKS; pinning no keys (fail closed)')
        return {}


def _pinned_producer_generation(environ=None):
    """The single expected current ``(id, digest, kid)`` tuple, or None."""
    env = os.environ if environ is None else environ
    raw = str(env.get('PULSE_HIVE_PRODUCER_GENERATION', '')).strip()
    if not raw:
        return None
    try:
        doc = json.loads(raw)
        pid = doc.get('producer_generation_id')
        digest = doc.get('producer_generation_digest')
        kid = doc.get('kid')
        if pid and digest and kid:
            return {'producer_generation_id': str(pid),
                    'producer_generation_digest': str(digest),
                    'kid': str(kid)}
    except Exception:
        logger.exception('Invalid PULSE_HIVE_PRODUCER_GENERATION; failing closed')
    return None


# --- The strict verifier -------------------------------------------------------
# HIVE-1153 P2 #1: the ONLY protected-header keys the frozen contract permits.
_ALLOWED_HEADER_KEYS = frozenset({'alg', 'typ', 'kid'})
_ALLOWED_CLAIM_KEYS = frozenset({
    'schema_version', 'iss', 'aud', 'sub', 'organization_id', 'team_slug',
    'request_id', 'request_sha256', 'requirement_key', 'required_capabilities',
    'results', 'materialization_generation', 'producer_generation_id',
    'producer_generation_digest', 'iat', 'nbf', 'exp', 'jti',
})
# The producer materialization digest is `mat-<sha256:hex>` (hive
# fleet/capability_evidence.py:65 -> f'mat-{_sha256(...)}'). It is a DYNAMIC
# per-roster digest, so the verifier binds its exact shape, never a single pinned
# value (that would fail closed every time the roster legitimately re-materializes).
_MATERIALIZATION_GENERATION_RE = re.compile(r'^mat-sha256:[0-9a-f]{64}$')


def _strict_json_segment(segment_b64, dup_reason):
    """Decode a compact-JWS base64url segment as JSON, rejecting DUPLICATE keys.

    PyJWT's default decoder silently keeps the last of any duplicated header or
    payload claim, so a token can carry a benign visible claim and a shadowing
    malicious one. We re-parse the raw segments with a duplicate-detecting hook
    (HIVE-1153 P2 #1) and treat any duplicate as authority_unknown.
    """
    try:
        padded = segment_b64 + '=' * (-len(segment_b64) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('ascii'))
    except Exception:
        raise EvidenceAuthorityUnknown('malformed_jws')

    def _no_duplicate_keys(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                raise EvidenceAuthorityUnknown(dup_reason, str(k))
            seen[k] = v
        return seen

    try:
        return json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except EvidenceAuthorityUnknown:
        raise
    except Exception:
        raise EvidenceAuthorityUnknown('malformed_jws')


def verify_evidence_jws(compact_jws, *, request, expected_email, expected_org_id,
                        expected_team_slug, requirement_key, required_capabilities,
                        pinned_jwks=None, pinned_generation=None, environ=None, now=None):
    """Verify a compact ``HiveCapabilityEvidenceV2`` JWS. Raise on any failure.

    Returns a :class:`VerifiedEvidence` only when EVERY binding holds. Any
    deviation raises :class:`EvidenceAuthorityUnknown` with a stable reason
    slug — the caller treats every raise identically (fail closed) but records
    the reason for shadow diffing.
    """
    if pinned_jwks is None:
        pinned_jwks = _load_pinned_jwks(environ)
    if pinned_generation is None:
        pinned_generation = _pinned_producer_generation(environ)

    if not compact_jws or not isinstance(compact_jws, str):
        raise EvidenceAuthorityUnknown('evidence_missing')
    if not pinned_jwks:
        raise EvidenceAuthorityUnknown('no_pinned_keys')
    if not pinned_generation:
        raise EvidenceAuthorityUnknown('no_pinned_generation')

    # 0) Structural: exactly three segments, and NEITHER the header NOR the
    #    payload may carry duplicate keys (PyJWT would silently keep the last).
    segments = compact_jws.split('.')
    if len(segments) != 3:
        raise EvidenceAuthorityUnknown('malformed_jws')
    header = _strict_json_segment(segments[0], 'duplicate_header_claim')
    raw_claims = _strict_json_segment(segments[1], 'duplicate_payload_claim')

    # 1) Header: EXACT protected-header key set + exact alg/typ/resolvable kid,
    #    BEFORE touching the signature. An extra header key (e.g. `jku`, `x5u`,
    #    `crit`) is rejected outright rather than ignored (HIVE-1153 P2 #1).
    if set(header.keys()) != _ALLOWED_HEADER_KEYS:
        raise EvidenceAuthorityUnknown(
            'unexpected_header_keys', ','.join(sorted(map(str, header.keys()))))
    claim_keys = set(raw_claims.keys())
    extra_claims = claim_keys - _ALLOWED_CLAIM_KEYS
    if extra_claims:
        raise EvidenceAuthorityUnknown(
            'unexpected_claim_keys', ','.join(sorted(map(str, extra_claims))))
    missing_claims = _ALLOWED_CLAIM_KEYS - claim_keys
    if missing_claims:
        raise EvidenceAuthorityUnknown(
            'missing_claim', ','.join(sorted(map(str, missing_claims))))
    if header.get('alg') != _EDDSA:
        raise EvidenceAuthorityUnknown('alg_not_allowed', str(header.get('alg')))
    if header.get('typ') != EVIDENCE_TYP:
        raise EvidenceAuthorityUnknown('typ_mismatch', str(header.get('typ')))
    kid = header.get('kid')
    if not kid:
        raise EvidenceAuthorityUnknown('missing_kid')
    if kid != pinned_generation['kid']:
        raise EvidenceAuthorityUnknown('kid_not_current_generation', str(kid))
    key = pinned_jwks.get(kid)
    if key is None:
        raise EvidenceAuthorityUnknown('unknown_kid', str(kid))

    # 2) Signature + registered-claim validation via the EdDSA allow-list.
    #    Expiry/nbf are validated manually below against an injectable ``now`` so
    #    the SHA-pinned golden vector (frozen timestamps) verifies deterministically
    #    while production uses real wall-clock time.
    try:
        claims = _jwt.decode(
            compact_jws, key=key, algorithms=list(_ALLOWED_ALGS),
            audience=EVIDENCE_AUDIENCE, issuer=EVIDENCE_ISSUER, leeway=0,
            options={
                'require': ['iss', 'aud', 'sub', 'iat', 'nbf', 'exp', 'jti'],
                'verify_signature': True, 'verify_aud': True, 'verify_iss': True,
                'verify_exp': False, 'verify_nbf': False, 'verify_iat': False,
            },
        )
    except _jwt.InvalidSignatureError:
        raise EvidenceAuthorityUnknown('bad_signature')
    except _jwt.MissingRequiredClaimError as exc:
        raise EvidenceAuthorityUnknown('missing_claim', str(exc))
    except _jwt.InvalidTokenError as exc:
        raise EvidenceAuthorityUnknown('invalid_token', str(exc))

    # 3) Freshness: bounded ≤5s lifetime + nbf<=now<exp (RFC 8725 explicit expiry).
    try:
        iat, nbf, exp = int(claims['iat']), int(claims['nbf']), int(claims['exp'])
    except (KeyError, TypeError, ValueError):
        raise EvidenceAuthorityUnknown('bad_time_claims')
    now_ts = int(time.time()) if now is None else int(now)
    # RFC 8725: an issued-in-the-future token is never fresh — reject even when
    # nbf<=now<exp (the exact-head adversarial: iat=now+4, nbf=now, exp=now+5).
    if iat > now_ts:
        raise EvidenceAuthorityUnknown('iat_in_future')
    if exp <= iat:
        raise EvidenceAuthorityUnknown('non_positive_lifetime')
    if nbf < iat:
        raise EvidenceAuthorityUnknown('nbf_before_iat')
    if exp - iat > MAX_LIFETIME_SECONDS:
        raise EvidenceAuthorityUnknown('lifetime_too_long')
    if now_ts >= exp:
        raise EvidenceAuthorityUnknown('expired')
    if now_ts < nbf:
        raise EvidenceAuthorityUnknown('not_yet_valid')

    # 4) Exact-seat / request / policy / generation bindings.
    def _require(claim, expected, reason):
        if claims.get(claim) != expected:
            raise EvidenceAuthorityUnknown(reason, f'{claim}={claims.get(claim)!r}')

    _require('schema_version', EVIDENCE_SCHEMA, 'schema_mismatch')
    _require('sub', str(expected_email).strip().lower(), 'wrong_subject')
    _require('organization_id', int(expected_org_id), 'wrong_org')
    _require('team_slug', str(expected_team_slug), 'wrong_team')
    _require('request_id', request.get('request_id'), 'wrong_request_id')
    _require('request_sha256', request_sha256(request), 'wrong_request_sha256')
    _require('requirement_key', str(requirement_key), 'wrong_requirement_key')
    _require('producer_generation_id',
             pinned_generation['producer_generation_id'], 'producer_generation_not_current')
    _require('producer_generation_digest',
             pinned_generation['producer_generation_digest'], 'producer_generation_not_current')

    req_caps = sorted({
        str(c).strip().lower() for c in required_capabilities if str(c).strip()
    })
    if list(claims.get('required_capabilities') or []) != req_caps:
        raise EvidenceAuthorityUnknown('required_capabilities_mismatch')

    # 5) Results: a PRESENT/ABSENT verdict for EVERY and ONLY requested capability.
    results = claims.get('results')
    if not isinstance(results, dict) or set(results.keys()) != set(req_caps):
        raise EvidenceAuthorityUnknown('results_shape_mismatch')
    present = set()
    for cap, verdict in results.items():
        if verdict == _RESULT_PRESENT:
            present.add(cap)
        elif verdict != _RESULT_ABSENT:
            raise EvidenceAuthorityUnknown('malformed_result', f'{cap}={verdict!r}')

    mat_gen = claims.get('materialization_generation')
    if not isinstance(mat_gen, str) or not _MATERIALIZATION_GENERATION_RE.match(mat_gen):
        raise EvidenceAuthorityUnknown('bad_materialization_generation', repr(mat_gen))

    return VerifiedEvidence(
        subject=claims['sub'],
        organization_id=int(claims['organization_id']),
        team_slug=str(claims['team_slug']),
        request_sha256=str(claims['request_sha256']),
        required_capabilities=tuple(req_caps),
        present_capabilities=frozenset(present),
        materialization_generation=str(claims['materialization_generation']),
        producer_generation_id=str(claims['producer_generation_id']),
        jti=str(claims['jti']),
        producer_generation_digest=str(claims['producer_generation_digest']),
        expires_at=int(exp),
        iat=int(iat),
    )


# --- PLAT-9505 ruling (b): form-based short-circuit -----------------------------
# A seat that cannot possibly canonicalize to a fleet subject (non-@shizuha.com
# form — the FleetAgent canonical is {agent_username}@shizuha.com) would
# deterministically 409 at the producer. Per the identity-decision ruling
# (nagi, HIVE-1153/PLAT-4732 owner, thread comment 828382): short-circuit the
# doomed round-trip with a typed authority_unknown. Narrow and typed: the
# identity FORM is the only predicate — transient hive faults on fleet-form
# seats still round-trip/retry per existing semantics. Fail-closed preserved:
# authority_unknown remains a denial — no evidence minted, no capability
# granted. The strand stays visible through the durable deduped shadow
# breadcrumb (reason non_fleet_form_seat): a seat repeatedly appearing there
# is a routing defect (fleet work assigned to a human seat), worth seeing,
# not hiding.
def _seat_is_fleet_form(seat) -> bool:
    email = str(seat or '').strip().lower()
    if '@' not in email:
        return False
    return email.rsplit('@', 1)[-1] == 'shizuha.com'


# --- Producer fetch ------------------------------------------------------------
def fetch_evidence(request, *, url=None, timeout=2, environ=None):
    """POST the request to Hive's producer and return the compact JWS.

    Fail-closed on any transport doubt: an unset URL, an unreachable endpoint, a
    non-2xx status, or a body without an ``evidence`` string raises
    :class:`EvidenceAuthorityUnknown`. The endpoint is Hive-internal and
    ingress-blocked; Pulse authenticates as a service. Never returns a partial
    or unverified result — the caller must still verify the JWS.
    """
    env = os.environ if environ is None else environ
    endpoint = url or str(env.get('PULSE_HIVE_EVIDENCE_URL', '')).strip()
    if not endpoint:
        raise EvidenceAuthorityUnknown('no_producer_url')
    # The Hive producer authenticates Pulse with a shared internal token; the
    # real view returns 403 without it. Require it fail-closed (HIVE-1153 P1).
    token = str(env.get('PULSE_HIVE_EVIDENCE_TOKEN', '')).strip()
    if not token:
        raise EvidenceAuthorityUnknown('no_producer_token')
    # PLAT-9505 ruling (b): a non-fleet-form seat cannot canonicalize to a
    # fleet subject — the producer would deterministically 409. Short-circuit
    # the doomed round-trip with a typed authority_unknown (see the block
    # comment on _seat_is_fleet_form).
    seat = request.get('email') if isinstance(request, dict) else None
    if not _seat_is_fleet_form(seat):
        logger.warning(
            'Hive capability-evidence request for a non-fleet-form seat (%r); '
            'short-circuiting authority_unknown without the round-trip',
            seat,
        )
        raise EvidenceAuthorityUnknown('non_fleet_form_seat', detail=str(seat or ''))
    try:
        import requests as _requests
        # Send the EXACT canonical bytes Hive re-hashes into request_sha256.
        # `requests`' own `json=` re-serializes with default spacing/ordering,
        # so the view rejects it as HIVE_EVIDENCE_REQUEST_NOT_CANONICAL — post
        # the canonical bytes as the raw body with an explicit JSON content type.
        resp = _requests.post(
            endpoint, data=canonical_request_bytes(request), timeout=timeout,
            headers={
                'Content-Type': 'application/json',
                'X-Internal-Service': 'shizuha-pulse',
                'X-Hive-Internal-Token': token,
            },
        )
    except EvidenceAuthorityUnknown:
        raise
    except Exception:
        logger.warning('Hive capability-evidence producer unreachable; failing closed', exc_info=True)
        raise EvidenceAuthorityUnknown('producer_unreachable')
    if resp.status_code >= 400:
        # PLAT-9505: a typed producer error (the 409 conflict class —
        # SUBJECT_MISMATCH / SUBJECT_NOT_MATERIALIZED / EXACT_SEAT_MISMATCH —
        # plus 403 auth and 503 stale-roster) is a DIAGNOSABLE strand, not
        # unreachability. Surface the API's error code in the reason slug so
        # a persistent conflict condition self-identifies in deduplicated
        # breadcrumbs/metrics instead of masquerading as
        # ``producer_unreachable``. The fail-closed outcome is unchanged.
        code = ''
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                code = str(payload.get('error') or payload.get('code') or '')
        except Exception:
            pass
        reason = f'producer_http_{resp.status_code}'
        if code:
            reason += '_' + re.sub(r'[^a-z0-9_]+', '_', code.lower()).strip('_')
        logger.warning(
            'Hive capability-evidence producer returned HTTP %s (code=%s); failing closed',
            resp.status_code, code or 'unparseable')
        raise EvidenceAuthorityUnknown(reason)
    try:
        body = resp.json()
    except Exception:
        logger.warning('Hive capability-evidence producer returned a non-JSON body; failing closed', exc_info=True)
        raise EvidenceAuthorityUnknown('empty_evidence')
    jws = body.get('evidence') if isinstance(body, dict) else None
    if not jws or not isinstance(jws, str):
        raise EvidenceAuthorityUnknown('empty_evidence')
    return jws


# --- Mode-aware verdict (pure; NO ownership mutation) --------------------------
OUTCOME_OFF = 'off'
OUTCOME_AUTHORIZED = 'authorized'
OUTCOME_DENIED = 'denied'
OUTCOME_AUTHORITY_UNKNOWN = 'authority_unknown'


@dataclass(frozen=True)
class SubstrateVerdict:
    """Mode-aware substrate-capability verdict, free of any ownership effect.

    The caller (``WorkflowService._seat_satisfies_required_capability``) maps
    outcome onto behavior:
      * ``off``               -> ignore this verdict; legacy behavior stands.
      * ``authorized``        -> the seat's signed evidence carries every cap.
      * ``denied``            -> signed evidence proves at least one cap ABSENT.
      * ``authority_unknown`` -> could not prove capability; preserve a valid
                                 current owner, authorize no new owner/transfer,
                                 never boot a seat merely to probe.
    In ``shadow`` the caller emits ``reason`` as a deduplicated breadcrumb and
    still returns the legacy result; in ``enforce`` it applies the verdict at the
    ownership-writing commit fence.
    """
    mode: str
    outcome: str
    reason: str
    present_capabilities: frozenset = field(default_factory=frozenset)
    # HIVE-1153 P1 #3 increment 3: on AUTHORIZED, the verified selection-time proof
    # to carry to the ownership-writing commit fence for local revalidation.
    receipt: object = None

    @property
    def is_off(self):
        return self.outcome == OUTCOME_OFF

    @property
    def authorizes(self):
        """True ONLY on a positive signed proof; never on unknown/denied/off."""
        return self.outcome == OUTCOME_AUTHORIZED


def _high_water_key(evidence):
    return dict(
        producer_generation_id=str(evidence.producer_generation_id),
        organization_id=int(evidence.organization_id),
        subject=str(evidence.subject).strip().lower(),
        team_slug=str(evidence.team_slug or ''),
    )


def advance_materialization_high_water(evidence):
    """HIVE-1153 P1 #2 (HIVE-1174): advance the durable per-exact-seat materialization
    high-water for a just-verified evidence, BEFORE the caller may AUTHORIZE.

    Returns None when the response is the newest-or-consistent for its seat (caller may
    authorize), or a HOLD reason slug when it is superseded/ambiguous (caller must NOT
    authorize):
      * strictly-newer signed ``iat`` advances the row and clears ambiguity;
      * a strictly-older ``iat`` is already superseded -> never rewinds -> HOLD;
      * equal ``iat`` with a different ``materialization_generation`` latches ``ambiguous``
        -> HOLD until a strictly-newer ``iat`` resolves it.
    The row is row-locked so concurrent verifications serialize.
    """
    from django.db import transaction
    from ..models import SubstrateMaterializationHighWater as _HW
    key = _high_water_key(evidence)
    iat = int(getattr(evidence, 'iat', 0) or 0)
    gen = str(evidence.materialization_generation)
    with transaction.atomic():
        row = _HW.objects.select_for_update().filter(**key).first()
        if row is None:
            _HW.objects.create(iat=iat, materialization_generation=gen,
                               ambiguous=False, **key)
            return None
        if iat > row.iat:
            row.iat = iat
            row.materialization_generation = gen
            row.ambiguous = False
            row.save(update_fields=['iat', 'materialization_generation',
                                    'ambiguous', 'updated_at'])
            return None
        if iat < row.iat:
            return 'materialization_superseded'
        # equal iat
        if gen != row.materialization_generation:
            if not row.ambiguous:
                row.ambiguous = True
                row.save(update_fields=['ambiguous', 'updated_at'])
            return 'materialization_ambiguous'
        if row.ambiguous:
            return 'materialization_ambiguous'
        return None


def evaluate_substrate(*, mode, request, expected_email, expected_org_id,
                       expected_team_slug, requirement_key, required_capabilities,
                       fetch=fetch_evidence, pinned_jwks=None, pinned_generation=None,
                       environ=None, now=None):
    """Fetch + verify + classify, per the sole three-state mode. Pure verdict.

    ``off`` short-circuits (no fetch). ``shadow`` and ``enforce`` run the exact
    same production fetch/verify/verdict — the modes differ ONLY in how the
    caller acts on the result, never in what is computed, so a shadow bake proves
    the enforce verdict before it can change ownership.
    """
    if mode == MODE_OFF:
        return SubstrateVerdict(MODE_OFF, OUTCOME_OFF, 'mode_off')
    try:
        jws = fetch(request, environ=environ)
        evidence = verify_evidence_jws(
            jws, request=request, expected_email=expected_email,
            expected_org_id=expected_org_id, expected_team_slug=expected_team_slug,
            requirement_key=requirement_key, required_capabilities=required_capabilities,
            pinned_jwks=pinned_jwks, pinned_generation=pinned_generation, environ=environ, now=now,
        )
    except EvidenceAuthorityUnknown as exc:
        return SubstrateVerdict(mode, OUTCOME_AUTHORITY_UNKNOWN, exc.reason)
    if evidence.satisfies(required_capabilities):
        return SubstrateVerdict(
            mode, OUTCOME_AUTHORIZED, 'ok', evidence.present_capabilities,
            receipt=build_substrate_receipt(evidence, request))
    return SubstrateVerdict(mode, OUTCOME_DENIED, 'seat_incapable_substrate',
                            evidence.present_capabilities)


# --- Selection-time receipt carried to the ownership-writing commit fence -------
@dataclass(frozen=True)
class SubstrateCapabilityReceipt:
    """The verified selection-time proof carried to the ownership-writing commit
    fence (HIVE-1153 P1 #3 increment 3, per the architect ruling). It is
    revalidated LOCALLY under the Item+Workflow row locks — NEVER re-fetched from
    the producer while holding a DB lock — so policy/stage/intent drift (via the
    rk2 key), a superseding writer generation, a rotated producer/materialization
    tuple, or an elapsed 5-second lifetime each fail closed at commit."""
    requirement_key: str
    request_sha256: str
    assignment_generation: int
    organization_id: int
    team_slug: str
    subject: str
    required_capabilities: tuple
    present_capabilities: frozenset
    materialization_generation: str
    producer_generation_id: str
    producer_generation_digest: str
    jti: str
    expires_at: int
    # HIVE-1153 P1 #2 (akira re-review): the exact request/item this proof was
    # issued for. VerifiedEvidence carries neither, so they are captured from the
    # selection-time request here; the commit fence rebuilds the request with the
    # SAME request_id + item_id and the item's CURRENT context, re-hashes it, and
    # compares against request_sha256 — binding the receipt to the exact item (no
    # cross-item replay in the 5 s window) and to every request field.
    item_id: int = 0
    request_id: str = ''
    # HIVE-1153 P1 #2 (HIVE-1174): the signed iat this proof was materialized under —
    # compared against the durable per-seat high-water at commit so a superseded
    # materialization HOLDs.
    iat: int = 0


def build_substrate_receipt(evidence, request):
    """Snapshot a verified evidence + its request into a carry-able commit receipt."""
    return SubstrateCapabilityReceipt(
        requirement_key=str(request.get('requirement_key')),
        request_sha256=str(evidence.request_sha256),
        assignment_generation=int(request.get('assignment_generation') or 0),
        organization_id=int(evidence.organization_id),
        team_slug=str(evidence.team_slug),
        subject=str(evidence.subject),
        required_capabilities=tuple(evidence.required_capabilities),
        present_capabilities=frozenset(evidence.present_capabilities),
        materialization_generation=str(evidence.materialization_generation),
        producer_generation_id=str(evidence.producer_generation_id),
        producer_generation_digest=str(evidence.producer_generation_digest),
        jti=str(evidence.jti),
        expires_at=int(evidence.expires_at),
        item_id=int(request.get('item_id') or 0),
        request_id=str(request.get('request_id') or ''),
        iat=int(getattr(evidence, 'iat', 0) or 0),
    )


def revalidate_substrate_receipt(receipt, *, recomputed_requirement_key,
                                 current_assignment_generation, pinned_generation=None,
                                 expected_subject=None, expected_team_slug=None,
                                 expected_required_capabilities=None,
                                 expected_org_id=None, expected_request_sha256=None,
                                 expected_item_id=None, now=None):
    """Local, NO-NETWORK revalidation of a carried receipt at the commit fence.

    Returns a stable drift-reason slug, or ``None`` when the receipt still holds.
    ANY non-None result MUST cause the caller to HOLD (zero ownership write). The
    caller has already re-resolved the item's workflow/stage under the Item+Workflow
    row locks and recomputed the rk2 key; this compares that fresh view against the
    frozen selection-time proof, entirely offline.

    HIVE-1153 P1 (akira re-review): beyond the rk2 key + generation + producer
    tuple + expiry, the FULL authorizing context is revalidated when the caller
    supplies it — the routed team (`expected_team_slug`), the exact required-
    substrate-capability vector (`expected_required_capabilities`), and the org.
    An assignment-group change or an INSTANCE capability addition (via
    metadata.required_capabilities_by_stage, which is not part of the workflow-
    policy envelope and so does not move the rk2 policy digest) must therefore
    invalidate a stale receipt.
    """
    now_ts = int(time.time()) if now is None else int(now)
    if int(receipt.expires_at) <= 0 or now_ts >= int(receipt.expires_at):
        return 'receipt_expired'
    if str(recomputed_requirement_key) != str(receipt.requirement_key):
        # rk2 encodes org+workflow+policy-digest+stage+intent, so this single
        # comparison catches policy, destination-stage, and assignment-intent drift.
        return 'requirement_key_drift'
    if int(current_assignment_generation) != int(receipt.assignment_generation):
        return 'writer_generation_drift'
    if expected_subject is not None and \
            str(expected_subject).strip().lower() != str(receipt.subject).strip().lower():
        return 'subject_drift'
    if expected_org_id is not None and int(expected_org_id) != int(receipt.organization_id):
        return 'org_drift'
    if expected_team_slug is not None and \
            str(expected_team_slug) != str(receipt.team_slug):
        return 'team_slug_drift'
    if expected_required_capabilities is not None:
        want = sorted({str(c).strip().lower() for c in expected_required_capabilities if str(c).strip()})
        have = sorted({str(c).strip().lower() for c in (receipt.required_capabilities or ()) if str(c).strip()})
        if want != have:
            return 'required_capabilities_drift'
    if pinned_generation:
        if str(pinned_generation.get('producer_generation_id') or '') != str(receipt.producer_generation_id):
            return 'producer_generation_drift'
        if str(pinned_generation.get('producer_generation_digest') or '') != str(receipt.producer_generation_digest):
            return 'producer_generation_drift'
    # HIVE-1153 P1 #2 (akira): bind the receipt to the EXACT item + request. The
    # caller rebuilds the request with the receipt's own request_id + the item's
    # CURRENT pk/context and re-hashes it; a mismatch means the proof was issued for
    # a different item (cross-item replay) or a request field drifted since
    # selection. item_id is compared directly too so a receipt whose request_id was
    # reused across items still fails closed.
    if expected_item_id is not None and int(expected_item_id) != int(receipt.item_id or 0):
        return 'request_item_drift'
    if expected_request_sha256 is not None and \
            str(expected_request_sha256) != str(receipt.request_sha256):
        return 'request_sha256_drift'
    return None
