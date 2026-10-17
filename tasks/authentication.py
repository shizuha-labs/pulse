"""
Custom JWT Authentication for Shizuha Pulse

This authentication class validates JWT tokens issued by Shizuha ID
without requiring the user to exist in the local database.

In a microservices architecture with unified login (like Atlassian):
- Shizuha ID issues JWT tokens
- Other services (Pulse, Admin) validate and trust those tokens
- User data is fetched from the token claims, not the local database
"""

import logging
from rest_framework import authentication, exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth import get_user_model

from tasks.token_revocation import is_token_revoked

logger = logging.getLogger(__name__)

# PLAT-8243: structured 401 rejection-reason log. Every auth rejection in this
# module logs ``auth_rejection reason=...`` BEFORE the 401 is returned, so an
# investigator can distinguish expired / version_skew / claim_less /
# bad_signature / revoked / service_scope rejections from the logs alone
# (PLAT-8236: three one-reads to triangulate what one reason line names).
# NEVER log the token itself — only kid / token_version metadata.
AUTH_REJECTION_REASONS = (
    'expired', 'version_skew', 'claim_less', 'bad_signature',
    'revoked', 'service_scope',
)


def _jwt_rejection_metadata(raw_token):
    """Best-effort kid + token_version extraction from an UNVERIFIED token.

    Diagnostics only: the values are logged as unverified metadata alongside
    the rejection reason. Never includes the token (or any signature material).
    """
    import jwt as _jwt
    meta = {'kid': None, 'token_version': None}
    try:
        header = _jwt.get_unverified_header(raw_token) or {}
        meta['kid'] = header.get('kid')
    except Exception:
        pass
    try:
        payload = _jwt.decode(raw_token, options={'verify_signature': False})
        meta['token_version'] = payload.get('token_version', payload.get('ver'))
    except Exception:
        pass
    return meta


def _log_auth_rejection(reason, detail, raw_token=None, *, kid=None, token_version=None):
    """Emit the structured rejection line. reason ∈ AUTH_REJECTION_REASONS."""
    if raw_token is not None:
        meta = _jwt_rejection_metadata(raw_token)
        kid = kid if kid is not None else meta['kid']
        token_version = token_version if token_version is not None else meta['token_version']
    logger.warning(
        'auth_rejection reason=%s kid=%s token_version=%s detail=%s',
        reason, kid if kid is not None else '-',
        token_version if token_version is not None else '-',
        detail,
    )


def _classify_rejection(detail):
    """Map a rejection message to a structured reason (PLAT-8243)."""
    msg = (detail or '').lower()
    if 'expired' in msg:
        return 'expired'
    if 'revoked' in msg:
        return 'revoked'
    if 'kid' in msg or 'audience' in msg:
        # unknown kid (key-rotation skew) and aud-mismatch rejections are both
        # issuer/consumer version-skew classes, not signature forgeries.
        return 'version_skew'
    if 'no recognizable user identification' in msg or 'user_id' in msg:
        return 'claim_less'
    return 'bad_signature'


User = get_user_model()


def _record_agent_heartbeat(email, *, source='auth'):
    """PLS-494 (aoi contract): refresh the canonical agent-liveness marker.

    Delegates to the shared ``agent_liveness`` module so the WRITE folds aliases
    with the SAME helper the failover-drain read uses and — critically — is
    OBSERVABLE on storage failure instead of silently swallowed. The caller fires
    this at the authenticated request/activity boundary for EVERY resolved actor,
    NOT only when the best-effort JWT ``is_agent`` claim is present (that claim
    can be absent on a genuinely authenticated agent mutation — the split-brain
    that let PLS-204 drain an actively-producing owner). Never raises: auth must
    not break on a liveness-marker failure. (PLAT-694 runtime-agnostic liveness
    is preserved — remote/BYO agents still refresh via every authenticated call.)
    """
    from .agent_liveness import record_agent_heartbeat
    return record_agent_heartbeat(email, source=source)


def _normalize_memberships(raw) -> dict:
    """
    Coerce the JWT's organization_memberships claim into {int: str}.

    JSON serialization forces object keys to strings, so the claim arrives
    as {"1": "admin", "7": "owner"}. Downstream PermissionService code
    filters Item.project.org_id (integer FK) — keeping keys as int here
    removes a class of "1 != '1'" bugs in resolver arithmetic.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def _extract_org_memberships(validated_token, user_id=None, user_email=None) -> dict:
    """
    Read `organization_memberships` from a validated JWT, with a live
    self-heal path for tokens that pre-date the claim.

    Behavior:
      - Claim is present (even as `{}`):  trust the JWT value — it was
        stamped at token-mint time from authoritative data and tokens
        only live ~15min before being refreshed.
      - Claim is ABSENT entirely (key not in token): fetch fresh from
        shizuha-admin so old cached tokens upgrade themselves instead of
        silently degrading to "no memberships → no elevated access".

    The present-vs-absent distinction is what matters — `{}` is a
    legitimate "user is in zero orgs" state (personal workspace only,
    pending invite, etc.) and must NOT trigger a live fetch; fetching
    would mean calling admin-service on every request for users with
    no memberships. Only tokens that predate the claim get the fallback.
    """
    if 'organization_memberships' in validated_token:
        return _normalize_memberships(validated_token['organization_memberships'])
    return _fetch_memberships_live(user_id, user_email)


def _fetch_memberships_live(user_id, user_email=None) -> dict:
    """
    Ask shizuha-admin for the user's active OrganizationMemberships.

    This is the "old token didn't carry the claim yet" fallback. It adds
    one HTTP call per request for users whose tokens predate the claim,
    which self-resolves as those tokens get refreshed (max 15 min for
    access-token TTL, 7 days for refresh token).

    Failures are non-fatal and return `{}` — same as no elevated grants.
    Never raises; never blocks auth if admin-service is down.
    """
    if user_id is None:
        return {}
    import os
    import requests as _req
    base = os.environ.get('SHIZUHA_ADMIN_API_URL') or 'http://shizuha-admin:8003/api'
    url = f"{base}/internal/users/{user_id}/memberships/"
    params = {}
    if user_email:
        params['email'] = user_email
    try:
        resp = _req.get(url, params=params, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return _normalize_memberships(data.get('memberships') or {})
    except _req.RequestException as exc:
        logger.warning(
            f"Live memberships fallback failed for user {user_id}: {exc}. "
            f"Falling back to empty grants."
        )
    return {}


def _mapped_role(memberships, org_id):
    """Read a role from a membership map with int or string keys."""
    if not isinstance(memberships, dict) or org_id is None:
        return None
    try:
        oid = int(org_id)
    except (TypeError, ValueError):
        return None
    for key in (oid, str(oid)):
        role = memberships.get(key)
        if role:
            return role
    return None


def merge_live_membership(user, org_id):
    """Return this user's role in ``org_id``, refreshing a stale JWT once.

    Memberships are stamped when the token is minted. A person who just
    created an organization is already a member in Admin and not yet in that
    token, so Pulse would refuse the first task until they signed in again.
    One live read per request fills the gap. A failed read keeps the token map.
    An empty map is left empty so callers can still use their admin fallback.
    """
    try:
        oid = int(org_id)
    except (TypeError, ValueError):
        return None
    memberships = getattr(user, 'organization_memberships', None)
    role = _mapped_role(memberships, oid)
    if role:
        return role
    if getattr(user, '_memberships_live_checked', False):
        return None
    user._memberships_live_checked = True
    live = _fetch_memberships_live(
        getattr(user, 'id', None),
        getattr(user, 'email', None),
    )
    if not live:
        return None
    if not isinstance(memberships, dict):
        memberships = {}
    memberships.update(live)
    user.organization_memberships = memberships
    return _mapped_role(memberships, oid)


class FederatedUser:
    """
    A lightweight user object for federated authentication.
    Used when the user exists in Shizuha ID but not in the local database.
    """
    def __init__(self, user_id, email=None, username=None, first_name=None, last_name=None,
                 date_joined=None, is_staff=False, is_superuser=False, enabled_services=None,
                 organization_memberships=None):
        from django.utils import timezone
        self.id = user_id
        self.pk = user_id
        self._original_email = email  # Store original to detect fallback
        self.email = email or f"user_{user_id}@shizuha.id"
        self.username = username or f"user_{user_id}"
        self.first_name = first_name or ""
        self.last_name = last_name or ""
        self.date_joined = date_joined or timezone.now()
        self.is_authenticated = True
        self.is_active = True
        self.is_anonymous = False
        self.is_staff = is_staff
        self.is_superuser = is_superuser
        # None = unrestricted (staff/superuser), [] = no services, [...] = specific services
        self.enabled_services = enabled_services
        # Tenant-scoped RBAC claim: {org_id_int: role_str}.
        # Normalized from the JWT where JSON forces string keys; we coerce
        # back to ints so PermissionService filters can do integer FK matches
        # without per-call string conversion. Empty dict = no elevated grants.
        self.organization_memberships = _normalize_memberships(organization_memberships)

        # Log warning if using fallback email (indicates JWT claims issue)
        if not email:
            logger.warning(
                f"FederatedUser {user_id} created without email claim, using fallback: {self.email}"
            )

    @property
    def admin_org_ids(self):
        """Org IDs where this user has owner or admin role (L1 of the resolver)."""
        return [oid for oid, role in self.organization_memberships.items()
                if role in ('owner', 'admin')]

    @property
    def member_org_ids(self):
        """Org IDs where this user has any active membership (L2b for open projects)."""
        return list(self.organization_memberships.keys())

    def role_in_org(self, org_id):
        """Lookup the user's role in a specific org. Returns None if not a member."""
        if org_id is None:
            return None
        try:
            return self.organization_memberships.get(int(org_id))
        except (TypeError, ValueError):
            return None

    @property
    def has_real_email(self):
        """Returns True if the email came from JWT claims, False if using fallback."""
        return self._original_email is not None

    def __str__(self):
        return f"FederatedUser({self.id})"

    def save(self, *args, **kwargs):
        pass  # No-op for federated users


# ───────────────────────── OAuth 2.0 advisory + enforcement ─────────────────
# Inlined from packages/shizuha-auth: Phase 2/4 of OAuth migration. Services
# that import the shared package should prefer that copy; this duplicate
# keeps parity while the migration to the shared package is in-flight.

def _oauth_advisory_claim_check(validated_token):
    """Log mismatched `aud`/`iss` claims without rejecting. Returns status dict."""
    import os as _os
    from django.conf import settings as _st
    service_name = getattr(_st, 'SHIZUHA_SERVICE_NAME', '') or _os.environ.get('SHIZUHA_SERVICE_NAME', '')
    expected_issuer = getattr(_st, 'SHIZUHA_EXPECTED_ISSUER', '') or _os.environ.get('SHIZUHA_EXPECTED_ISSUER', '')
    aud = validated_token.get('aud')
    if aud is not None and aud not in ('*', ['*']):
        aud_list = aud if isinstance(aud, list) else [aud]
        if service_name and service_name not in aud_list:
            logger.warning(
                f"JWT aud={aud_list!r} does not include service={service_name!r}. "
                f"Phase 2 advisory — token accepted."
            )
    iss = validated_token.get('iss')
    if iss is not None and expected_issuer and iss != expected_issuer:
        logger.warning(
            f"JWT iss={iss!r} does not match expected={expected_issuer!r}. "
            f"Phase 2 advisory — token accepted."
        )


def _oauth_enforce_audience_claim(validated_token):
    """Phase 4: when SHIZUHA_AUD_ENFORCE=True, reject tokens with mismatched aud."""
    import os as _os
    from django.conf import settings as _st
    from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
    enforce = bool(getattr(_st, 'SHIZUHA_AUD_ENFORCE', False)) or \
        _os.environ.get('SHIZUHA_AUD_ENFORCE', '').lower() in ('1', 'true', 'yes')
    if not enforce:
        return
    service_name = getattr(_st, 'SHIZUHA_SERVICE_NAME', '') or _os.environ.get('SHIZUHA_SERVICE_NAME', '')
    if not service_name:
        return
    aud = validated_token.get('aud')
    if aud is None or aud == '*' or aud == ['*']:
        return
    aud_list = aud if isinstance(aud, list) else [aud]
    if service_name not in aud_list:
        logger.warning(
            f"JWT rejected: aud={aud_list!r} does not include required service={service_name!r}"
        )
        # PLAT-8243: aud-mismatch rejection is the version_skew class.
        _log_auth_rejection('version_skew', f"Token audience {aud_list} does not include '{service_name}'")
        raise _InvalidToken(f"Token audience {aud_list} does not include '{service_name}'")


# ───────────────────────── OAuth 2.0 RS256 validation ──────────────────────
# Phase 5 of OAuth migration: accept tokens signed with RS256 (shizuha-id's
# RSA key, public half published at /id/.well-known/jwks.json). Falls back
# from default HS256 validation so legacy tokens keep working during grace.

class _Rs256Token(dict):
    """Lightweight dict-like token — satisfies simplejwt get_user() interface."""
    pass


_JWKS_CACHE = {'keys': {}, 'fetched_at': 0}
_JWKS_TTL = 600  # seconds
_ALLOWED_JWT_ALGS = {'HS256', 'RS256'}


def _jwks_fetch_keys(force_refresh=False):
    """Fetch/cache shizuha-id JWKS keys by kid.

    PLAT-675: RS256 verification must honor `kid` and fail closed. A normal
    cached lookup may use a fresh in-memory key set; a forced refresh (used on
    kid miss/rotation) returns ONLY the just-fetched key set, never stale keys,
    so an unknown kid or JWKS outage cannot accidentally validate against an old
    fallback key.
    """
    import json as _json, os as _os, time as _time, urllib.request as _urlreq
    from tasks.net_utils import safe_urlopen  # PLAT-2006: scheme-guarded urlopen (http/https only)
    import jwt as _jwt
    if (not force_refresh and _time.time() - _JWKS_CACHE['fetched_at'] < _JWKS_TTL
            and _JWKS_CACHE['keys']):
        return _JWKS_CACHE['keys']
    issuer = (_os.environ.get('SHIZUHA_EXPECTED_ISSUER') or
              'http://shizuha-id:8001').rstrip('/')
    jwks_url = _os.environ.get('SHIZUHA_OAUTH_JWKS_URL') or f'{issuer}/.well-known/jwks.json'
    try:
        resp = safe_urlopen(jwks_url, timeout=5)
        jwks = _json.loads(resp.read())
        keys = {
            k['kid']: _jwt.algorithms.RSAAlgorithm.from_jwk(_json.dumps(k))
            for k in jwks.get('keys', [])
            if k.get('kid') and k.get('kty') == 'RSA' and k.get('alg') in (None, 'RS256')
        }
        _JWKS_CACHE['keys'] = keys
        _JWKS_CACHE['fetched_at'] = _time.time()
        return keys
    except Exception as _exc:
        logger.warning(f'JWKS fetch from {jwks_url} failed: {_exc}')
        return {} if force_refresh else _JWKS_CACHE['keys']


def _try_rs256_validate(raw_token):
    """Return a _Rs256Token on success, None if not RS256, raise if bad RS256."""
    import jwt as _jwt
    from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
    try:
        header = _jwt.get_unverified_header(raw_token)
    except Exception:
        return None
    alg = header.get('alg')
    if alg not in _ALLOWED_JWT_ALGS:
        raise _InvalidToken(f'Unsupported JWT alg={alg!r}')
    if alg != 'RS256':
        return None
    kid = header.get('kid')
    if not kid:
        _log_auth_rejection('bad_signature', 'RS256 token missing kid', raw_token)
        raise _InvalidToken('RS256 token missing kid')
    keys = _jwks_fetch_keys()
    key = keys.get(kid)
    if key is None:
        # Re-fetch once in case of key rotation; fail closed on miss/outage.
        keys = _jwks_fetch_keys(force_refresh=True)
        key = keys.get(kid)
    if key is None:
        # PLAT-8243: unknown kid after a forced refresh = issuer key-rotation
        # skew (the consumer's JWKS view is behind/ahead of the signer).
        _log_auth_rejection('version_skew', f'No JWKS key available for kid={kid!r}', raw_token, kid=kid)
        raise _InvalidToken(f'No JWKS key available for kid={kid!r}')
    try:
        payload = _jwt.decode(
            raw_token, key=key, algorithms=['RS256'],
            options={'verify_aud': False, 'verify_exp': True},
        )
    except _jwt.PyJWTError as exc:
        _log_auth_rejection(_classify_rejection(str(exc)), f'RS256 validation failed: {exc}', raw_token, kid=kid)
        raise _InvalidToken(f'RS256 validation failed: {exc}')
    return _Rs256Token(payload)

class ShizuhaJWTAuthentication(JWTAuthentication):
    """
    Custom JWT authentication that works with Shizuha ID tokens.

    If the user doesn't exist locally, creates a FederatedUser object
    from the token claims instead of failing with 401.
    """


    def get_validated_token(self, raw_token):
        """CANONICAL JWKS verifier — keep in sync, do not fork (RS256 migration Step 1).

        Hardened dual-scheme validation of shizuha-id tokens:
          - alg-allowlist {HS256, RS256} ONLY; ``alg='none'`` (and any other alg) is
            rejected up front — defense-in-depth against an alg-confusion bypass even
            though each downstream path already pins its own algorithm.
          - Route STRICTLY by the declared alg and validate with ONLY the matching
            key. An RS256-shaped token therefore cannot be replayed as HS256 signed
            with the RSA public key: HS256 verifies against the shared JWT_SECRET_KEY,
            never an RSA key — i.e. no HS256/RS256 key-confusion.
          - HS256: simplejwt against the shared signing secret (legacy/default).
          - RS256: shizuha-id JWKS, fail-closed (missing/unavailable key -> 401).
        """
        import jwt as _jwt
        from rest_framework_simplejwt.exceptions import InvalidToken as _InvalidToken
        try:
            header = _jwt.get_unverified_header(raw_token) or {}
        except Exception:
            _log_auth_rejection('bad_signature', 'Malformed JWT header', raw_token)
            raise _InvalidToken('Malformed JWT header')
        alg = (header.get('alg') or '').upper()
        if alg not in ('HS256', 'RS256'):
            # rejects 'none', empty, and any non-allowlisted alg (e.g. RS384, ES256)
            _log_auth_rejection('bad_signature', f'Unsupported JWT alg {alg!r}: only HS256/RS256 accepted', raw_token, kid=header.get('kid'))
            raise _InvalidToken(f'Unsupported JWT alg {alg!r}: only HS256/RS256 accepted')
        if alg == 'RS256':
            tok = _try_rs256_validate(raw_token)
            if tok is None:
                _log_auth_rejection('bad_signature', 'RS256 token failed JWKS validation', raw_token, kid=header.get('kid'))
                raise _InvalidToken('RS256 token failed JWKS validation')
            validated = tok
        else:
            # alg == 'HS256': simplejwt verifies the signature against the shared secret
            # (never an RSA key), so a public-key-signed HS256 forgery cannot pass.
            try:
                validated = super().get_validated_token(raw_token)
            except (TokenError, _InvalidToken) as _exc:
                # PLAT-8243: classify the simplejwt rejection (expired vs
                # bad_signature) before re-raising — the 401 detail alone is
                # what made the PLAT-8236 investigation start blind.
                _log_auth_rejection(_classify_rejection(str(_exc)), f'HS256 validation failed: {_exc}', raw_token, kid=header.get('kid'))
                raise
        # SCLI-376 interim (hiro architecture determination, ID-1 §8 FAIL CLOSED):
        # every consumer must reject a revoked access token on its next request.
        # Pulse previously had NO revocation consult here (signature+expiry only),
        # and the shared is_token_revoked skipped agent/session/broker JWTs (no
        # client_id claim) — so revoked broker tokens stayed 200 in Hive/Pulse
        # while ID correctly 401'd them (split-brain runtime). Consult revocation
        # for EVERY validated token regardless of client_id.
        claims = dict(getattr(validated, 'payload', validated) or {})
        if is_token_revoked(claims, raw_token):
            _log_auth_rejection('revoked', 'Token has been revoked', raw_token, kid=header.get('kid'))
            raise _InvalidToken('Token has been revoked')
        return validated

    def get_user(self, validated_token):
        """
        Get the user from the validated token.
        If user doesn't exist locally, return a FederatedUser.

        Handles both integer user IDs (from Shizuha ID) and UUID user IDs (from agents).
        """
        if (validated_token.get('is_service')
                or str(validated_token.get('sub', '')).startswith('service:')):
            _log_auth_rejection('service_scope', 'Service credentials require an explicitly scoped endpoint.')
            raise InvalidToken('Service credentials require an explicitly scoped endpoint.')
        _oauth_advisory_claim_check(validated_token)
        _oauth_enforce_audience_claim(validated_token)
        try:
            user_id = validated_token.get('user_id')
            if user_id is None:
                # PLAT-8243: the claim-less class — the live fleet-scale issue
                # (PLAT-5104 projection, ~38% authoritative_empty rate). This
                # reason line names it in one read.
                _log_auth_rejection('claim_less', 'Token contained no recognizable user identification')
                raise InvalidToken('Token contained no recognizable user identification')

            # Extract additional claims for FederatedUser
            email = validated_token.get('email')
            username = validated_token.get('username')
            first_name = validated_token.get('first_name')
            last_name = validated_token.get('last_name')

            # Extract staff/superuser claims
            is_staff = validated_token.get('is_staff', False)
            is_superuser = validated_token.get('is_superuser', False)

            # Extract enabled_services from token
            enabled_services = validated_token.get('enabled_services')
            # Staff/superuser override
            if validated_token.get('is_staff') or validated_token.get('is_superuser'):
                enabled_services = None  # None = unrestricted

            org_memberships = _extract_org_memberships(validated_token, user_id=user_id, user_email=email)

            # PLS-494 (aoi contract): refresh the canonical liveness marker at the
            # authenticated actor boundary for EVERY resolved actor — NOT only when
            # the best-effort `is_agent` claim is present (a genuinely authenticated
            # agent mutation can carry no/false `is_agent`, which is exactly how an
            # actively-producing owner lost its liveness evidence and got drained).
            # The helper records only agent-domain identities and is observable on
            # storage failure. (Preserves PLAT-694 runtime-agnostic liveness.)
            _record_agent_heartbeat(email)

            # Check if this is an agent token (agents have UUID user_id and is_agent=True)
            is_agent = validated_token.get('is_agent', False)
            if is_agent:
                logger.debug(f"Agent token detected for {email}, using federated user")
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )

            # CRITICAL: shizuha-id user_ids (1, 2, 3…) collide with local
            # Django auto-increment PKs on each federated service's DB
            # (Pulse local id=1 is admin@example.com). Blindly looking up
            # by PK hijacks the federated identity and returns whichever
            # local user happens to sit at that PK. When the token carries
            # an `email` claim (the shizuha-id SSO happy path), trust the
            # claim and return a FederatedUser — don't touch the local DB.
            if email:
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )

            # Legacy / local-login token without an email claim — fall back
            # to local DB lookup so Django admin session flows still work.
            try:
                int_user_id = int(user_id)
                user = User.objects.get(pk=int_user_id)
                # Graft org memberships onto the local User too — PermissionService
                # reads `.organization_memberships` uniformly across federated and
                # local users, so this avoids a service call per request for humans.
                user.organization_memberships = org_memberships
                return user
            except (User.DoesNotExist, ValueError, TypeError):
                logger.debug(
                    f"User {user_id} not found locally and token has no "
                    f"email claim; returning placeholder FederatedUser"
                )
                return FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )

        except KeyError:
            raise InvalidToken('Token contained no recognizable user identification')


class ShizuhaIDAuthentication(authentication.BaseAuthentication):
    """
    Alternative authentication that validates tokens against Shizuha ID service.

    This can be used to verify tokens by calling the Shizuha ID API directly,
    which is useful for:
    - Getting fresh user data
    - Validating token hasn't been revoked
    - Supporting token refresh flows
    """

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if not auth_header.startswith('Bearer '):
            return None

        token = auth_header.split(' ')[1]

        try:
            # Validate the token locally first (fast path)
            validated_token = AccessToken(token)
            user_id = validated_token.get('user_id')

            if user_id is None:
                # PLAT-8243: claim-less class (PLAT-5104 projection carrier).
                _log_auth_rejection('claim_less', 'Invalid token: no user_id claim', token)
                raise exceptions.AuthenticationFailed('Invalid token')

            # Extract additional claims for FederatedUser
            email = validated_token.get('email')
            username = validated_token.get('username')
            first_name = validated_token.get('first_name')
            last_name = validated_token.get('last_name')

            # Extract staff/superuser claims
            is_staff = validated_token.get('is_staff', False)
            is_superuser = validated_token.get('is_superuser', False)

            # Extract enabled_services from token
            enabled_services = validated_token.get('enabled_services')
            # Staff/superuser override
            if validated_token.get('is_staff') or validated_token.get('is_superuser'):
                enabled_services = None  # None = unrestricted

            org_memberships = _extract_org_memberships(validated_token, user_id=user_id, user_email=email)

            # PLS-494 (aoi contract): refresh the canonical liveness marker at the
            # authenticated actor boundary for EVERY resolved actor — the other
            # authenticated request path (see ShizuhaJWTAuthentication.get_user).
            _record_agent_heartbeat(email)

            # Check if this is an agent token
            is_agent = validated_token.get('is_agent', False)
            if is_agent:
                logger.debug(f"Agent token detected for {email}")
                federated_user = FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )
                return (federated_user, validated_token)

            # CRITICAL: shizuha-id numeric user_ids can collide with local
            # Django PKs. If the token carries federated email claims, trust
            # them and avoid local-PK lookup, matching ShizuhaJWTAuthentication.
            if email:
                federated_user = FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )
                return (federated_user, validated_token)

            # Legacy / local-login token without an email claim — preserve the
            # local DB fallback for old admin/session flows only.
            try:
                int_user_id = int(user_id)
                user = User.objects.get(pk=int_user_id)
                user.organization_memberships = org_memberships
                return (user, validated_token)
            except (User.DoesNotExist, ValueError, TypeError):
                federated_user = FederatedUser(
                    user_id=user_id,
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    enabled_services=enabled_services,
                    organization_memberships=org_memberships,
                )
                return (federated_user, validated_token)

        except TokenError as e:
            # PLAT-8243: classify the local fast-path rejection (expired vs
            # bad_signature) before surfacing the 401.
            _log_auth_rejection(_classify_rejection(str(e)), f'Invalid token: {str(e)}', token)
            raise exceptions.AuthenticationFailed(f'Invalid token: {str(e)}')

    def authenticate_header(self, request):
        return 'Bearer realm="api"'
