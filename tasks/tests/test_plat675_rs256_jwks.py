"""PLAT-675: Pulse accepts shizuha-id RS256/JWKS tokens alongside legacy HS256.

These are synthetic-token tests for the first consumer rollout slice. They pin
security properties before the fleet-wide signing flip: kid lookup, alg allowlist,
alg=none rejection, no kid fallback, and fail-closed JWKS rotation misses.
"""
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase
from rest_framework_simplejwt.exceptions import InvalidToken

from tasks import authentication as auth


class PulseRs256JwksValidationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def _token(self, *, kid='test-kid', alg='RS256', key=None, extra=None):
        payload = {
            'token_type': 'access',
            'user_id': 123,
            'email': 'rs256-user@shizuha.com',
            'exp': int(time.time()) + 300,
            **(extra or {}),
        }
        headers = {'kid': kid} if kid is not None else {}
        return jwt.encode(payload, key or self.private_key, algorithm=alg, headers=headers)

    def test_rs256_token_validates_by_kid_from_jwks(self):
        token = self._token(kid='pulse-test')
        with mock.patch.object(auth, '_jwks_fetch_keys', return_value={'pulse-test': self.public_key}):
            validated = auth._try_rs256_validate(token)
        self.assertEqual(validated['user_id'], 123)
        self.assertEqual(validated['email'], 'rs256-user@shizuha.com')

    def test_hs256_token_is_left_for_legacy_simplejwt_path(self):
        token = jwt.encode({'user_id': 123, 'exp': int(time.time()) + 300}, 'secret', algorithm='HS256')
        self.assertIsNone(auth._try_rs256_validate(token))

    def test_alg_none_is_rejected(self):
        token = jwt.encode({'user_id': 123, 'exp': int(time.time()) + 300}, key='', algorithm='none')
        with self.assertRaises(InvalidToken):
            auth._try_rs256_validate(token)

    def test_rs256_requires_kid_no_first_key_fallback(self):
        token = self._token(kid=None)
        with mock.patch.object(auth, '_jwks_fetch_keys', return_value={'some-key': self.public_key}):
            with self.assertRaises(InvalidToken):
                auth._try_rs256_validate(token)

    def test_unknown_kid_refetches_once_and_fails_closed(self):
        token = self._token(kid='rotated-key')
        with mock.patch.object(auth, '_jwks_fetch_keys', side_effect=[{'old-key': self.public_key}, {}]) as fetch:
            with self.assertRaises(InvalidToken):
                auth._try_rs256_validate(token)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(fetch.call_args_list[1].kwargs.get('force_refresh'))
