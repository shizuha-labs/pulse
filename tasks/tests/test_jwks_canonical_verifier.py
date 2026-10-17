"""CANONICAL JWKS verifier — security test vectors (RS256 migration Step 1).

Keep in sync, do not fork: this suite is the bypass-rejection proof for
ShizuhaJWTAuthentication.get_validated_token and is copied verbatim into every
shizuha-id JWT consumer alongside the verifier. It proves, on every consumer:

  1. valid HS256 token (shared secret)            -> accepted  (zero regression)
  2. alg='none' token                             -> REJECTED  (no alg-confusion bypass)
  3. unsupported alg (RS384)                       -> REJECTED  (allowlist is exactly {HS256,RS256})
  4. HS256/RS256 key-confusion (RS256 claims       -> REJECTED  (HS256 verifies vs the shared
     re-signed HS256 with the RSA PUBLIC key)                    secret, never an RSA key)
  5. synthetic RS256 signed by a test key in JWKS -> accepted  (RS256 path live, proven before the flip)
  6. RS256 token, JWKS empty/unavailable          -> REJECTED  (fail-closed, never allow-through)
  7. revoked broker JWT (no client_id) via         -> REJECTED  (SCLI-376: revocation consulted for
     get_validated_token                                       every token type; no client_id skip)
  8. active agent JWT (no client_id) via           -> accepted  (SCLI-376: active non-client token
     get_validated_token                                       still passes)
"""
import time
import jwt as pyjwt
from unittest.mock import MagicMock, patch
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from django.test import TestCase
from django.conf import settings
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from tasks import authentication as auth
from tasks.authentication import ShizuhaJWTAuthentication
from tasks import token_revocation as trev


def _hs_secret():
    return settings.SIMPLE_JWT.get('SIGNING_KEY') or settings.SECRET_KEY


def _claims(**over):
    now = int(time.time())
    base = {
        'token_type': 'access', 'jti': 'test-jti', 'user_id': 4242,
        'email': 'qa@shizuha.com', 'iat': now, 'exp': now + 3600,
    }
    base.update(over)
    return base


def _forge_hs256(claims, secret):
    """Hand-forge an HS256 JWT with an arbitrary secret.

    Modern PyJWT refuses pyjwt.encode(..., <RSA PEM>, algorithm='HS256') (it
    rejects using an asymmetric key as an HMAC secret), which is exactly the
    key-confusion forge we must simulate. Build the token by hand so the SERVER
    verifier is still exercised — it must reject a token HMAC'd with the RSA
    public key rather than the real shared secret.
    """
    import base64
    import hashlib
    import hmac
    import json

    def _b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b'=')

    if isinstance(secret, str):
        secret = secret.encode()
    header = _b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    payload = _b64(json.dumps(claims, separators=(',', ':')).encode())
    signing_input = header + b'.' + payload
    sig = _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return (signing_input + b'.' + sig).decode()


class CanonicalJWKSVerifierTests(TestCase):
    def setUp(self):
        # Fresh RSA keypair for the RS256 / key-confusion vectors.
        self._priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pub_pem = self._priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._priv_pem = self._priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self._orig_cache = dict(auth._JWKS_CACHE)
        # Inject our test public key into the JWKS cache under kid 'test'.
        auth._JWKS_CACHE['keys'] = {'test': self._priv.public_key()}
        auth._JWKS_CACHE['fetched_at'] = time.time() + 1e9  # never auto-refetch in tests
        # SCLI-376: get_validated_token now consults is_token_revoked on every
        # token. Default to "active" so pre-existing vectors don't hit the network;
        # the SCLI-376 regression vectors override to active=False.
        self._revocation_cache = dict(trev._REVOCATION_CACHE)
        trev._REVOCATION_CACHE.clear()
        self._resp = MagicMock()
        self._resp.status_code = 200
        self._resp.json.return_value = {"active": True}
        self._post_patcher = patch("tasks.token_revocation.requests.post", return_value=self._resp)
        self._mock_post = self._post_patcher.start()

    def tearDown(self):
        self._post_patcher.stop()
        auth._JWKS_CACHE.clear()
        auth._JWKS_CACHE.update(self._orig_cache)
        trev._REVOCATION_CACHE.clear()
        trev._REVOCATION_CACHE.update(self._revocation_cache)

    def _validate(self, token):
        return ShizuhaJWTAuthentication().get_validated_token(token)

    # 1 — zero regression
    def test_valid_hs256_accepted(self):
        tok = pyjwt.encode(_claims(), _hs_secret(), algorithm='HS256')
        validated = self._validate(tok)
        self.assertEqual(validated.get('user_id'), 4242)

    # 7 — SCLI-376: broker/agent JWT (no client_id), revoked in ID → REJECTED
    # through the real get_validated_token caller path, not the helper alone.
    # This is the production sequence: valid signature+expiry → revocation
    # consult → ID says active=False (revoked) → deny.
    def test_revoked_broker_jwt_rejected(self):
        tok = pyjwt.encode(
            _claims(user_id=777, email='broker@agents.shizuha.io'),
            _hs_secret(), algorithm='HS256',
        )
        self._resp.json.return_value = {"active": False}
        with self.assertRaises(InvalidToken):
            self._validate(tok)
        self._mock_post.assert_called_once()

    # 8 — SCLI-376: active agent JWT (no client_id) still ACCEPTED (zero regression)
    def test_active_agent_jwt_accepted(self):
        tok = pyjwt.encode(
            _claims(user_id=777, email='broker@agents.shizuha.io'),
            _hs_secret(), algorithm='HS256',
        )
        validated = self._validate(tok)
        self.assertEqual(validated.get('user_id'), 777)
        self._mock_post.assert_called_once()

    # 9 — PLAT-5996: a replayed pre-revocation token (token_version < current)
    # is REJECTED through the real get_validated_token caller path even when the
    # OAuth-store row is gone (revive-by-replay closure). Production sequence:
    # valid signature+expiry → revocation consult → ID active=True but current
    # token_version=3 > token's claim=1 → deny.
    def test_stale_token_version_rejected_through_real_caller(self):
        tok = pyjwt.encode(
            _claims(user_id=777, email='broker@agents.shizuha.io', token_version=1),
            _hs_secret(), algorithm='HS256',
        )
        self._resp.json.return_value = {"active": True, "token_version": 3}
        with self.assertRaises(InvalidToken):
            self._validate(tok)
        self._mock_post.assert_called_once()

    # 10 — PLAT-5996: a token carrying the CURRENT version is accepted through
    # the real caller (zero regression on the happy path).
    def test_current_token_version_accepted_through_real_caller(self):
        tok = pyjwt.encode(
            _claims(user_id=777, email='broker@agents.shizuha.io', token_version=3),
            _hs_secret(), algorithm='HS256',
        )
        self._resp.json.return_value = {"active": True, "token_version": 3}
        validated = self._validate(tok)
        self.assertEqual(validated.get('user_id'), 777)
        self._mock_post.assert_called_once()

    # 11 — PLAT-5996 coordinated rollout: a token WITHOUT the token_version
    # claim (old ID minted before the claim shipped) is evaluated by the active
    # check alone — accepted when active, no version comparison.
    def test_no_token_version_claim_accepted_during_rollout(self):
        tok = pyjwt.encode(
            _claims(user_id=777, email='broker@agents.shizuha.io'),
            _hs_secret(), algorithm='HS256',
        )
        self._resp.json.return_value = {"active": True, "token_version": 3}
        validated = self._validate(tok)
        self.assertEqual(validated.get('user_id'), 777)
        self._mock_post.assert_called_once()

    # 2 — the bypass ichi flagged
    def test_alg_none_rejected(self):
        tok = pyjwt.encode(_claims(), key='', algorithm='none')
        with self.assertRaises((InvalidToken, TokenError)):
            self._validate(tok)

    # 3 — allowlist is exactly {HS256, RS256}
    def test_unsupported_alg_rejected(self):
        tok = pyjwt.encode(_claims(), self._priv_pem, algorithm='RS384', headers={'kid': 'test'})
        with self.assertRaises((InvalidToken, TokenError)):
            self._validate(tok)

    # 4 — HS256/RS256 key confusion
    def test_key_confusion_rejected(self):
        # Attacker re-signs an RS256-claims token as HS256 using the PUBLIC key as
        # the HMAC secret. Must NOT verify (HS256 path uses the shared secret).
        # Hand-forged because modern PyJWT blocks encoding HS256 with a PEM key.
        tok = _forge_hs256(_claims(), self._pub_pem)
        with self.assertRaises((InvalidToken, TokenError)):
            self._validate(tok)

    # 5 — RS256 path proven synthetically before the signing flip
    def test_synthetic_rs256_accepted(self):
        tok = pyjwt.encode(_claims(), self._priv_pem, algorithm='RS256', headers={'kid': 'test'})
        validated = self._validate(tok)
        self.assertEqual(validated.get('user_id'), 4242)

    # 6 — fail-closed
    def test_rs256_fail_closed_when_no_jwks_key(self):
        auth._JWKS_CACHE['keys'] = {}
        auth._JWKS_CACHE['fetched_at'] = time.time() + 1e9  # block the re-fetch network call
        tok = pyjwt.encode(_claims(), self._priv_pem, algorithm='RS256', headers={'kid': 'missing'})
        with self.assertRaises((InvalidToken, TokenError)):
            self._validate(tok)
