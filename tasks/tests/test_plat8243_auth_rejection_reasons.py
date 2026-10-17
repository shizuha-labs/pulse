"""PLAT-8243 — every 401 rejection carries a structured reason line.

PLAT-8236 datum: the gateway's auth logs showed bare 401s with NO structured
rejection reason — the investigator cannot distinguish expired / version-skew /
claim-less / bad-signature rejections from the logs alone (three one-reads to
triangulate what one reason line names). This suite proves each rejection class
emits ``auth_rejection reason=<class>`` BEFORE the 401, with kid/token_version
metadata and never the token itself.
"""
import time
import jwt as pyjwt
from unittest.mock import patch

from django.test import SimpleTestCase
from django.conf import settings
from rest_framework_simplejwt.exceptions import InvalidToken

from tasks import authentication as auth
from tasks.authentication import ShizuhaJWTAuthentication, ShizuhaIDAuthentication


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


def _hs256(claims, secret=None, kid=None):
    return pyjwt.encode(claims, secret or _hs_secret(), algorithm='HS256',
                        headers={'kid': kid} if kid else None)


class AuthRejectionReasonTests(SimpleTestCase):
    """Each test asserts the structured line AND the rejection itself.

    SimpleTestCase: the rejection paths never touch the DB (revocation is
    patched at the module boundary), so the suite runs DB-free.
    """

    def _assert_reason(self, logs, reason):
        lines = [o for o in logs.output if 'auth_rejection' in o]
        self.assertTrue(lines, 'no auth_rejection line emitted')
        self.assertIn(f'reason={reason}', lines[-1])
        return lines
    def test_expired_token_logs_expired(self):
        tok = _hs256(_claims(exp=int(time.time()) - 100))
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(InvalidToken):
                ShizuhaJWTAuthentication().get_validated_token(tok)
        self._assert_reason(logs, 'expired')

    def test_tampered_signature_logs_bad_signature(self):
        tok = _hs256(_claims())
        tampered = tok[:-6] + ('aaaaaa' if tok[-6:] != 'aaaaaa' else 'bbbbbb')
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(InvalidToken):
                ShizuhaJWTAuthentication().get_validated_token(tampered)
        self._assert_reason(logs, 'bad_signature')

    def test_unsupported_alg_logs_bad_signature(self):
        tok = pyjwt.encode(_claims(), _hs_secret(), algorithm='HS384')
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(InvalidToken):
                ShizuhaJWTAuthentication().get_validated_token(tok)
        self._assert_reason(logs, 'bad_signature')

    def test_missing_user_id_logs_claim_less(self):
        # The PLAT-5104 projection class: valid signature, no identity claim.
        tok = _hs256(_claims(user_id=None))
        with patch.object(auth, 'is_token_revoked', return_value=False):
            with self.assertLogs(auth.logger, level='WARNING') as logs:
                with self.assertRaises(InvalidToken):
                    ShizuhaJWTAuthentication().get_user(
                        ShizuhaJWTAuthentication().get_validated_token(tok))
        self._assert_reason(logs, 'claim_less')

    def test_audience_mismatch_logs_version_skew(self):
        tok = _hs256(_claims(aud=['other-service']))
        with patch.dict('os.environ', {'SHIZUHA_AUD_ENFORCE': 'true',
                                       'SHIZUHA_SERVICE_NAME': 'pulse'}):
            with patch.object(auth, 'is_token_revoked', return_value=False):
                with self.assertLogs(auth.logger, level='WARNING') as logs:
                    with self.assertRaises(InvalidToken):
                        ShizuhaJWTAuthentication().get_user(
                            ShizuhaJWTAuthentication().get_validated_token(tok))
        self._assert_reason(logs, 'version_skew')

    def test_unknown_rs256_kid_logs_version_skew(self):
        # RS256 token whose kid is absent from JWKS = key-rotation skew.
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        tok = pyjwt.encode(_claims(), pem, algorithm='RS256', headers={'kid': 'stale-kid'})
        with patch.object(auth, '_jwks_fetch_keys', return_value={}):
            with self.assertLogs(auth.logger, level='WARNING') as logs:
                with self.assertRaises(InvalidToken):
                    ShizuhaJWTAuthentication().get_validated_token(tok)
        lines = self._assert_reason(logs, 'version_skew')
        self.assertIn('kid=stale-kid', lines[-1])

    def test_revoked_token_logs_revoked(self):
        tok = _hs256(_claims(client_id='broker'))
        with patch.object(auth, 'is_token_revoked', return_value=True):
            with self.assertLogs(auth.logger, level='WARNING') as logs:
                with self.assertRaises(InvalidToken):
                    ShizuhaJWTAuthentication().get_validated_token(tok)
        self._assert_reason(logs, 'revoked')

    def test_id_auth_expired_token_logs_expired(self):
        tok = _hs256(_claims(exp=int(time.time()) - 100))
        request = type('R', (), {'META': {'HTTP_AUTHORIZATION': f'Bearer {tok}'}})()
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(Exception):
                ShizuhaIDAuthentication().authenticate(request)
        self._assert_reason(logs, 'expired')

    def test_rejection_line_never_carries_the_token(self):
        tok = _hs256(_claims(exp=int(time.time()) - 100))
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(InvalidToken):
                ShizuhaJWTAuthentication().get_validated_token(tok)
        for line in logs.output:
            self.assertNotIn(str(tok), line, 'token material leaked into the rejection log')

    def test_rejection_line_carries_kid_metadata(self):
        tok = _hs256(_claims(exp=int(time.time()) - 100), kid='kid-123')
        with self.assertLogs(auth.logger, level='WARNING') as logs:
            with self.assertRaises(InvalidToken):
                ShizuhaJWTAuthentication().get_validated_token(tok)
        lines = self._assert_reason(logs, 'expired')
        self.assertIn('kid=kid-123', lines[-1])
