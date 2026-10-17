"""PLAT-5343 slice 1 — internal subject-assertion trust boundary (Pulse hop).

The v2 membership-projection route is fail-closed from its first release: it
inherits neither the tokenless ClusterIP compatibility mode nor the shared
internal service token. Every request must carry

    X-Internal-Service: <bound principal, e.g. shizuha-admin>
    X-Internal-Service-Token: <dedicated credential>
    X-Shizuha-Subject-Assertion: <compact signed assertion>

and the assertion must validate against the HLD v12 §4.1 trust contract:

- issuer / audience / scope bound to exactly this route;
- decimal positive-integer ``sub`` (the identity), canonical ``email`` when
  known (a bound attribute, never the identity);
- ``iat`` / ``exp`` with ``exp`` no more than five seconds after ``iat``;
- a non-empty ``jti``;
- signature under a dedicated internal-signing key, verified only with an
  algorithm from the fixed allowlist (Security condition S1: EdDSA or ES256 —
  ``alg: none``, unsigned/missing-``alg`` objects, and any RS/HS-style
  confusion attempt are validation failures, never a fallback or retry with a
  second algorithm);
- for the Admin→Pulse hop, a ``parent_assertion_sha256`` binding the derivative
  to the exact parent ID assertion Admin validated.

Stated semantics for the two Security conditions that demanded explicit text
(S3, akira 2026-09-09):

- **Clock skew:** validation tolerates a stated ``CLOCK_SKEW_TOLERANCE_SECONDS``
  (2 s) on both ``iat`` (not further in the future than skew) and ``exp`` (not
  expired by more than skew). Zero stated skew re-introduces the
  intermittent-failure-class auth flaps on the mint path (the exact PLAT-5343
  failure class); the skew is deliberately bounded far below the 5 s assertion
  lifetime so it cannot widen the replay window meaningfully.
- **jti replay:** single-use denylist with exp-bound retention. The first
  successful validation records ``jti`` in the shared cache for
  ``max(exp - now, 0) + 2 * skew`` seconds; any replay inside that window is
  rejected with ``replayed_jti``. Loss/eviction of the cache backend degrades
  to exp-bound-only replay protection, which stays safe because the assertion
  is read-only, carries no capability beyond this route, and lives ≤5 s.

Header↔credential binding (Security condition S4): the plaintext credential is
configured per bound principal; a credential is accepted only when the
``X-Internal-Service`` header names exactly the principal it is bound to. A
credential presented under a different service name is ``credential_mismatch``,
never an authentication success.

Nothing from the request body, query string, or any tenant token can supply or
override ``sub``/``email`` — the assertion is the only identity carrier, and
identity-bearing body keys are rejected outright (matrix row 11).
"""
import hashlib
import logging

import jwt
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('tasks.subject_assertion')

# Security condition S1 (akira 2026-09-09): fixed algorithm allowlist. Both
# hops' assertions are signed only with EdDSA or ES256. Validation never
# falls back or retries with a second algorithm.
ALLOWED_ALGORITHMS = ('EdDSA', 'ES256')

# HLD §4.1: exp is no more than five seconds after iat. Receiver-enforced so an
# over-long assertion is a validation failure, not an issuer-side courtesy.
ASSERTION_MAX_LIFETIME_SECONDS = 5

# Security condition S3 (akira 2026-09-09): the stated clock-skew tolerance.
# Applies symmetrically to iat (future) and exp (past) validation.
CLOCK_SKEW_TOLERANCE_SECONDS = 2

# Fixed rejection-reason enum. Values are stable identifiers safe for logs,
# metrics, and tests; they never carry assertion contents or PII.
REASON_MISSING_HEADERS = 'missing_headers'
REASON_UNKNOWN_CALLER = 'unknown_caller'
REASON_CREDENTIAL_MISMATCH = 'credential_mismatch'
REASON_ASSERTION_MISSING = 'assertion_missing'
REASON_ASSERTION_MALFORMED = 'assertion_malformed'
REASON_ALGORITHM_NOT_ALLOWED = 'algorithm_not_allowed'
REASON_BAD_SIGNATURE = 'bad_signature'
REASON_WRONG_ISSUER = 'wrong_issuer'
REASON_WRONG_AUDIENCE = 'wrong_audience'
REASON_WRONG_SCOPE = 'wrong_scope'
REASON_EXPIRED = 'expired'
REASON_FUTURE_IAT = 'future_iat'
REASON_LIFETIME_EXCEEDED = 'lifetime_exceeded'
REASON_INVALID_SUBJECT = 'invalid_subject'
REASON_INVALID_EMAIL = 'invalid_email'
REASON_MISSING_JTI = 'missing_jti'
REASON_REPLAYED_JTI = 'replayed_jti'
REASON_MISSING_PARENT_HASH = 'missing_parent_assertion_hash'
REASON_CALLER_SUPPLIED_IDENTITY = 'caller_supplied_identity'
REASON_NOT_CONFIGURED = 'not_configured'

ALL_REASONS = (
    REASON_MISSING_HEADERS,
    REASON_UNKNOWN_CALLER,
    REASON_CREDENTIAL_MISMATCH,
    REASON_ASSERTION_MISSING,
    REASON_ASSERTION_MALFORMED,
    REASON_ALGORITHM_NOT_ALLOWED,
    REASON_BAD_SIGNATURE,
    REASON_WRONG_ISSUER,
    REASON_WRONG_AUDIENCE,
    REASON_WRONG_SCOPE,
    REASON_EXPIRED,
    REASON_FUTURE_IAT,
    REASON_LIFETIME_EXCEEDED,
    REASON_INVALID_SUBJECT,
    REASON_INVALID_EMAIL,
    REASON_MISSING_JTI,
    REASON_REPLAYED_JTI,
    REASON_MISSING_PARENT_HASH,
    REASON_CALLER_SUPPLIED_IDENTITY,
    REASON_NOT_CONFIGURED,
)

SERVICE_HEADER = 'HTTP_X_INTERNAL_SERVICE'
CREDENTIAL_HEADER = 'HTTP_X_INTERNAL_SERVICE_TOKEN'
ASSERTION_HEADER = 'HTTP_X_SHIZUHA_SUBJECT_ASSERTION'

# Body keys that would smuggle identity into the resolution. The assertion is
# the only identity carrier (matrix row 11); a body carrying any of these is
# rejected before any lookup.
IDENTITY_BODY_KEYS = frozenset({
    'sub', 'subject', 'subject_id', 'user_id', 'email', 'user_email',
    'aliases', 'email_aliases', 'principal',
})

_JTI_CACHE_PREFIX = 'shizuha:v2:subject-assertion:jti:'


class SubjectAssertionRejected(Exception):
    """A v2 internal request failed authentication; carry the fixed reason."""

    def __init__(self, reason: str):
        if reason not in ALL_REASONS:
            raise ValueError(f'unknown subject-assertion rejection reason {reason!r}')
        self.reason = reason
        super().__init__(reason)


def _credential_map():
    """Bound-principal → dedicated credential, from settings (env-provisioned).

    Each entry binds one plaintext credential to exactly one caller principal
    (Security condition S4). Empty/missing configuration is fail-closed: the
    route rejects every request with ``not_configured`` rather than serving
    tokenless.
    """
    configured = getattr(settings, 'PULSE_V2_INTERNAL_SERVICE_CREDENTIALS', None) or {}
    if not isinstance(configured, dict):
        return {}
    return {
        str(principal): str(credential)
        for principal, credential in configured.items()
        if str(principal or '').strip() and str(credential or '').strip()
    }


def _verification_keys():
    """Accepted internal-signing public keys (PEM), current first.

    Key rotation keeps the previous key accepted for a migration window; a
    signature must verify under ONE of the accepted keys. Algorithm fallback
    remains impossible regardless of key choice (S1 allowlist is enforced on
    the token's own ``alg`` header before any key is tried).
    """
    keys = getattr(settings, 'PULSE_V2_ASSERTION_SIGNING_PUBLIC_KEYS', None) or []
    if isinstance(keys, str):
        keys = [keys]
    return [str(key) for key in keys if str(key or '').strip()]


def _constant_time_equal(a: str, b: str) -> bool:
    from django.utils.crypto import constant_time_compare
    return bool(a) and bool(b) and constant_time_compare(a, b)


def _reject_if_body_carries_identity(body) -> None:
    if not isinstance(body, dict):
        return
    for key in body:
        if str(key).strip().lower() in IDENTITY_BODY_KEYS:
            raise SubjectAssertionRejected(REASON_CALLER_SUPPLIED_IDENTITY)


def _parse_and_check_header(assertion: str) -> dict:
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.exceptions.PyJWTError:
        raise SubjectAssertionRejected(REASON_ASSERTION_MALFORMED)
    if not isinstance(header, dict) or not header.get('alg'):
        # Unsigned/missing-alg objects are validation failures (S1), never a
        # fallback to a default algorithm.
        raise SubjectAssertionRejected(REASON_ALGORITHM_NOT_ALLOWED)
    if header.get('alg') not in ALLOWED_ALGORITHMS:
        # Covers alg:none and every RS/HS-style confusion attempt.
        raise SubjectAssertionRejected(REASON_ALGORITHM_NOT_ALLOWED)
    return header


def _verify_signature(assertion: str, keys):
    for key in keys:
        try:
            return jwt.decode(
                assertion,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                options={
                    # exp/iat/aud/iss are validated below with typed reasons
                    # and the stated skew; PyJWT must only prove the signature.
                    # sub is validated here as a decimal positive integer —
                    # PyJWT's RFC7519 string-only sub check is disabled so a
                    # decimal-string sub (the wire format) parses locally.
                    'verify_exp': False,
                    'verify_iat': False,
                    'verify_aud': False,
                    'verify_iss': False,
                    'verify_sub': False,
                },
            )
        except jwt.exceptions.InvalidSignatureError:
            continue
        except jwt.exceptions.PyJWTError:
            # Malformed payload/key-format problems are not signature results;
            # treat them as malformed assertions.
            raise SubjectAssertionRejected(REASON_ASSERTION_MALFORMED)
    raise SubjectAssertionRejected(REASON_BAD_SIGNATURE)


def _validate_claims(claims, *, expected_issuer, expected_audience, expected_scope,
                     require_parent_hash, now) -> dict:
    if not isinstance(claims, dict):
        raise SubjectAssertionRejected(REASON_ASSERTION_MALFORMED)

    if claims.get('iss') != expected_issuer:
        raise SubjectAssertionRejected(REASON_WRONG_ISSUER)
    aud = claims.get('aud')
    aud_values = aud if isinstance(aud, list) else [aud]
    if expected_audience not in aud_values:
        raise SubjectAssertionRejected(REASON_WRONG_AUDIENCE)
    if claims.get('scope') != expected_scope:
        raise SubjectAssertionRejected(REASON_WRONG_SCOPE)

    skew = CLOCK_SKEW_TOLERANCE_SECONDS
    exp = claims.get('exp')
    iat = claims.get('iat')
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise SubjectAssertionRejected(REASON_ASSERTION_MALFORMED)
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        raise SubjectAssertionRejected(REASON_ASSERTION_MALFORMED)
    if exp > iat + ASSERTION_MAX_LIFETIME_SECONDS:
        # Receiver-enforced lifetime cap (HLD §4.1): exp ≤ iat + 5s.
        raise SubjectAssertionRejected(REASON_LIFETIME_EXCEEDED)
    if exp < now - skew:
        raise SubjectAssertionRejected(REASON_EXPIRED)
    if iat > now + skew:
        raise SubjectAssertionRejected(REASON_FUTURE_IAT)

    sub = claims.get('sub')
    if isinstance(sub, bool):
        raise SubjectAssertionRejected(REASON_INVALID_SUBJECT)
    if isinstance(sub, int):
        subject = sub
    elif isinstance(sub, str) and sub.strip().isdigit():
        subject = int(sub.strip())
    else:
        raise SubjectAssertionRejected(REASON_INVALID_SUBJECT)
    if subject <= 0:
        raise SubjectAssertionRejected(REASON_INVALID_SUBJECT)

    email = claims.get('email')
    if email is not None:
        email = str(email).strip().lower()
        if not email or '@' not in email:
            raise SubjectAssertionRejected(REASON_INVALID_EMAIL)
        claims['email'] = email

    jti = claims.get('jti')
    if not isinstance(jti, str) or not jti.strip():
        raise SubjectAssertionRejected(REASON_MISSING_JTI)

    if require_parent_hash:
        parent = claims.get('parent_assertion_sha256')
        if not isinstance(parent, str) or len(parent) != 64:
            raise SubjectAssertionRejected(REASON_MISSING_PARENT_HASH)
        try:
            int(parent, 16)
        except ValueError:
            raise SubjectAssertionRejected(REASON_MISSING_PARENT_HASH)

    claims['sub'] = subject
    return claims


def _register_jti_use(jti: str, exp: float, now: float) -> None:
    """Single-use denylist with exp-bound retention (stated S3 semantics).

    ``cache.add`` is atomic: the first validation wins, a replay inside the
    retention window finds the key present and is rejected. Retention covers
    the assertion's remaining life plus twice the stated skew.

    Stated degradation: if the cache backend itself is unavailable, replay
    protection degrades to exp-bound-only for that window (logged, never
    silent) instead of failing the route closed — the assertion is read-only,
    scope-bound to this route, and lives ≤5 s, so a replay inside its lifetime
    yields the same snapshot and cannot widen authority. A persistent outage
    is visible in logs; the route's availability matters more than replay
    defense-in-depth for a read-only ≤5 s credential.
    """
    retention = int(max(exp - now, 0) + 2 * CLOCK_SKEW_TOLERANCE_SECONDS) + 1
    retention = max(1, min(retention, 30))
    fingerprint = hashlib.sha256(jti.encode('utf-8')).hexdigest()
    key = f'{_JTI_CACHE_PREFIX}{fingerprint}'
    try:
        added = cache.add(key, True, timeout=retention)
    except Exception as exc:  # cache backend outage — stated degradation
        logger.warning('v2-internal-auth jti_denylist_unavailable reason=cache-error class=%s',
                       type(exc).__name__)
        return
    if not added:
        raise SubjectAssertionRejected(REASON_REPLAYED_JTI)


def authenticate_internal_service(request, *, expected_issuer, expected_audience,
                                  expected_scope, require_parent_hash=False,
                                  body=None):
    """Authenticate one v2 internal request end-to-end.

    Returns ``(principal, claims)`` on success; raises
    :class:`SubjectAssertionRejected` with a fixed-enum reason on every
    failure. Never logs assertion contents, credentials, or subject PII.
    """
    import time as _time

    service_name = (request.META.get(SERVICE_HEADER, '') or '').strip()
    credential = request.META.get(CREDENTIAL_HEADER, '') or ''
    assertion = request.META.get(ASSERTION_HEADER, '') or ''

    if not service_name and not credential and not assertion:
        raise SubjectAssertionRejected(REASON_MISSING_HEADERS)

    credentials = _credential_map()
    keys = _verification_keys()
    if not credentials or not keys:
        # Fail closed: an unconfigured route serves nobody (HLD §4.1 — the v2
        # path never inherits the tokenless compatibility mode).
        raise SubjectAssertionRejected(REASON_NOT_CONFIGURED)

    if service_name not in credentials:
        raise SubjectAssertionRejected(REASON_UNKNOWN_CALLER)
    if not _constant_time_equal(credential, credentials[service_name]):
        # S4: the credential is bound to one principal; presenting it under
        # another service name is a mismatch, not an authentication.
        raise SubjectAssertionRejected(REASON_CREDENTIAL_MISMATCH)

    if not assertion:
        raise SubjectAssertionRejected(REASON_ASSERTION_MISSING)

    _reject_if_body_carries_identity(body)

    _parse_and_check_header(assertion)
    claims = _verify_signature(assertion, keys)
    now = _time.time()
    claims = _validate_claims(
        claims,
        expected_issuer=expected_issuer,
        expected_audience=expected_audience,
        expected_scope=expected_scope,
        require_parent_hash=require_parent_hash,
        now=now,
    )
    _register_jti_use(claims['jti'], float(claims['exp']), now)

    logger.info(
        'v2-internal-auth ok service=%s reason=none',
        service_name,
    )
    return service_name, claims


def subject_fingerprint(issuer: str, subject, email) -> str:
    """sha256 over issuer+subject+canonical email (HLD §4.2).

    Lets the caller prove a snapshot belongs to its request without echoing
    subject PII back over the wire.
    """
    material = f'{issuer}|{int(subject)}|{(email or "").strip().lower()}'
    return 'sha256:' + hashlib.sha256(material.encode('utf-8')).hexdigest()
