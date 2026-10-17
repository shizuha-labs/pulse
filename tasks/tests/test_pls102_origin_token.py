"""PLS-102: Pulse must sign system-message origin tokens that Connect's
hardened InternalSystemMessageView (`_validate_system_origin_token`) accepts.

These tests replicate Connect's verifier exactly and assert the Pulse-side
helper produces a token it accepts — and that, with no secret configured, the
token is omitted (today's non-enforcing Connect, behaviour unchanged).
"""
import hashlib
import hmac
import time

from django.test import SimpleTestCase, override_settings

from tasks.services.connect_origin import (
    system_message_origin_token,
    system_message_headers,
)

SECRET = 'system-origin-secret'


def _connect_validates(token, *, secret, sender_service, recipient_user_id, content, max_age=300):
    """Faithful copy of connect.views.internal_api._validate_system_origin_token."""
    if sender_service not in {'shizuha-agent', 'shizuha-connect', 'shizuha-pulse'}:
        return False
    try:
        raw_ts, supplied = token.split(':', 1)
        ts = int(raw_ts)
    except (ValueError, AttributeError):
        return False
    if abs(int(time.time()) - ts) > max_age:
        return False
    body_hash = hashlib.sha256((content or '').encode('utf-8')).hexdigest()
    payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
    expected = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


@override_settings(CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET=SECRET)
class OriginTokenAcceptedByConnectTests(SimpleTestCase):
    def test_legacy_pulse_sender_service_is_rejected_by_connect_allowlist(self):
        token = system_message_origin_token(11, 'hi', 'pulse')
        self.assertFalse(
            _connect_validates(token, secret=SECRET, sender_service='pulse',
                               recipient_user_id=11, content='hi'),
            'Connect allowlist rejects legacy sender_service="pulse"; Pulse must use shizuha-pulse',
        )

    def test_token_validates_against_connect_verifier(self):
        recipient, content = 4242, '[Task Assigned] AT-1: do thing\nPriority: high'
        token = system_message_origin_token(recipient, content, 'shizuha-pulse')
        self.assertIsNotNone(token)
        self.assertTrue(
            _connect_validates(token, secret=SECRET, sender_service='shizuha-pulse',
                               recipient_user_id=recipient, content=content),
            'helper token must satisfy Connect\'s verifier',
        )

    def test_content_with_unicode_and_newlines(self):
        recipient, content = 7, 'Status: open → done\nüñïçødé body'
        token = system_message_origin_token(recipient, content, 'shizuha-pulse')
        self.assertTrue(
            _connect_validates(token, secret=SECRET, sender_service='shizuha-pulse',
                               recipient_user_id=recipient, content=content),
        )

    def test_tampered_content_is_rejected(self):
        recipient = 9
        token = system_message_origin_token(recipient, 'original', 'shizuha-pulse')
        self.assertFalse(
            _connect_validates(token, secret=SECRET, sender_service='shizuha-pulse',
                               recipient_user_id=recipient, content='TAMPERED'),
        )

    def test_headers_carry_internal_service_and_token(self):
        body = {'recipient_user_id': 11, 'content': 'hi', 'sender_service': 'shizuha-pulse'}
        headers = system_message_headers(body)
        self.assertEqual(headers['X-Internal-Service'], 'shizuha-pulse')
        self.assertIn('X-System-Message-Token', headers)
        self.assertTrue(
            _connect_validates(headers['X-System-Message-Token'], secret=SECRET,
                               sender_service='shizuha-pulse', recipient_user_id=11, content='hi'),
        )


@override_settings(CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET='')
class NoSecretDegradesGracefullyTests(SimpleTestCase):
    def test_token_is_none_without_secret(self):
        self.assertIsNone(system_message_origin_token(1, 'x', 'shizuha-pulse'))

    def test_headers_omit_token_but_keep_internal_service(self):
        headers = system_message_headers({'recipient_user_id': 1, 'content': 'x'})
        self.assertEqual(headers['X-Internal-Service'], 'shizuha-pulse')
        self.assertNotIn('X-System-Message-Token', headers)
