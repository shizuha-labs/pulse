"""PLAT-13818 — Pulse consumes Hive's signed active generation, fail-closed.

Hive's document wins only when its Ed25519 signature verifies against the
pinned JWKS. Unreachable, non-200, a missing field, an unknown kid, and a bad
signature all fall back to the env pin, which itself fails closed.
"""
import base64
import json
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase

from tasks.services import capability_evidence as ce

KID = 'hive-cap-ed25519-v1'
PIN = {
    'producer_generation_id': 'hive-pinned',
    'producer_generation_digest': 'sha256:' + 'ab' * 32,
    'kid': KID,
}


def _keypair():
    private = Ed25519PrivateKey.generate()
    public_jwk = json.loads(
        __import__('jwt').algorithms.OKPAlgorithm.to_jwk(private.public_key()))
    public_jwk['kid'] = KID
    return private, {KID: ce._jwt_algorithms.OKPAlgorithm.from_jwk(json.dumps(public_jwk))}


def _document():
    return {
        'kid': KID,
        'producer_generation_id': 'hive-live',
        'producer_generation_digest': 'sha256:' + 'cd' * 32,
        'activated_at': '2026-11-16T06:32:03Z',
        'source_sha': 'c' * 40,
    }


def _signed(private, doc):
    payload = dict(doc)
    raw = ce._canonical_generation_bytes(payload)
    signature = private.sign(raw)
    payload['signature'] = base64.urlsafe_b64encode(signature).rstrip(b'=').decode()
    return payload


class ActiveGenerationTests(SimpleTestCase):
    def setUp(self):
        self.private, self.jwks = _keypair()
        self.env = {
            'PULSE_HIVE_EVIDENCE_URL': 'http://hive.internal/internal/v1/fleet/capability-evidence',
            'PULSE_HIVE_PRODUCER_GENERATION': json.dumps(PIN),
            'PULSE_HIVE_EVIDENCE_PUBLIC_JWKS': '{}',
        }

    def _response(self, status, body):
        resp = mock.Mock(status_code=status)
        resp.json.return_value = body
        return resp

    def _current(self, response=None, error=None):
        with mock.patch.object(ce, '_load_pinned_jwks', return_value=self.jwks), \
                mock.patch('requests.get', return_value=response, side_effect=error) as get:
            result = ce.current_producer_generation(self.env)
        return result, get

    def test_verified_document_wins_over_the_pin(self):
        result, get = self._current(self._response(200, _signed(self.private, _document())))
        self.assertEqual(result['producer_generation_id'], 'hive-live')
        self.assertIn('/active-generation', get.call_args.args[0])

    def test_bad_signature_falls_back_to_the_pin(self):
        doc = _signed(self.private, _document())
        doc['signature'] = base64.urlsafe_b64encode(b'x' * 64).rstrip(b'=').decode()
        result, _get = self._current(self._response(200, doc))
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')

    def test_unknown_kid_falls_back_to_the_pin(self):
        doc = _signed(self.private, _document())
        doc['kid'] = 'someone-else'
        result, _get = self._current(self._response(200, doc))
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')

    def test_missing_field_falls_back_to_the_pin(self):
        doc = _signed(self.private, _document())
        del doc['source_sha']
        result, _get = self._current(self._response(200, doc))
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')

    def test_non_200_falls_back_to_the_pin(self):
        result, _get = self._current(self._response(503, {}))
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')

    def test_unreachable_falls_back_to_the_pin(self):
        result, _get = self._current(error=ConnectionError('down'))
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')

    def test_no_endpoint_uses_the_pin_without_a_request(self):
        env = dict(self.env)
        del env['PULSE_HIVE_EVIDENCE_URL']
        with mock.patch.object(ce, '_load_pinned_jwks', return_value=self.jwks), \
                mock.patch('requests.get') as get:
            result = ce.current_producer_generation(env)
        self.assertEqual(result['producer_generation_id'], 'hive-pinned')
        get.assert_not_called()
