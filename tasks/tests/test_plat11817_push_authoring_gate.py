"""PLAT-11817 regression: no-push seats cannot author, bad blobs cannot apply."""

from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from tasks.services.push_authoring_gate import (
    HandoffRejected,
    PushAuthoringRefused,
    assert_can_author,
    push_capable,
    verify_handoff,
)


class _Item:
    def __init__(self, metadata=None, labels=None, origin_repo=''):
        self.metadata = metadata or {}
        self.labels = labels or []
        self.origin_repo = origin_repo


def _fresh():
    return timezone.now().isoformat()


def _stale():
    return (timezone.now() - timedelta(hours=25)).isoformat()


class PushAuthoringGateTests(SimpleTestCase):
    def test_g1_no_push_seat_is_refused_before_authoring(self):
        item = _Item(metadata={
            'origin_repo': 'shizuha-labs/pulse',
            'seat_capabilities': {
                'shion@shizuha.com': {
                    'shizuha-labs/pulse': {'push': False, 'probed_at': _fresh()},
                },
            },
        })
        with self.assertRaises(PushAuthoringRefused) as caught:
            assert_can_author(item, 'shion@shizuha.com')
        self.assertEqual(caught.exception.code, 'no_push_capability')
        self.assertIn('cannot push to shizuha-labs/pulse', str(caught.exception))
        self.assertIn('write:repository', str(caught.exception))

    def test_g2_unknown_and_expired_capability_fail_closed(self):
        item = _Item(metadata={'origin_repo': 'shizuha-labs/pulse'})
        self.assertFalse(push_capable(item, 'shion@shizuha.com', 'shizuha-labs/pulse'))
        with self.assertRaises(PushAuthoringRefused):
            assert_can_author(item, 'shion@shizuha.com')

        expired = _Item(metadata={
            'origin_repo': 'shizuha-labs/pulse',
            'seat_capabilities': {
                'shion@shizuha.com': {
                    'shizuha-labs/pulse': {'push': True, 'probed_at': _stale()},
                },
            },
        })
        self.assertFalse(push_capable(
            expired, 'shion@shizuha.com', 'shizuha-labs/pulse',
        ))
        with self.assertRaises(PushAuthoringRefused):
            assert_can_author(expired, 'shion@shizuha.com')

    def test_g5_fresh_push_capability_allows_authoring(self):
        item = _Item(metadata={
            'origin_repo': 'shizuha-labs/pulse',
            'seat_capabilities': {
                'saki@shizuha.com': {
                    'shizuha-labs/pulse': {'push': True, 'probed_at': _fresh()},
                },
            },
        })
        assert_can_author(item, 'saki@shizuha.com')

    def test_non_code_item_is_not_gated(self):
        assert_can_author(_Item(metadata={}), 'shion@shizuha.com')

    def test_g3_sha256_mismatch_rejects_without_a_usable_pin(self):
        with self.assertRaises(HandoffRejected) as caught:
            verify_handoff(
                b'corrupted', 'sha256:' + 'ab' * 32,
                current_base_sha='abc123', blob_base_sha='abc123',
            )
        self.assertEqual(caught.exception.code, 'sha256_mismatch')

    def test_g4_stale_base_commit_fails_closed(self):
        import hashlib
        blob = b'patch'
        pin = hashlib.sha256(blob).hexdigest()
        with self.assertRaises(HandoffRejected) as caught:
            verify_handoff(
                blob, pin,
                current_base_sha='new-main', blob_base_sha='old-main',
            )
        self.assertEqual(caught.exception.code, 'base_not_current')

    def test_matching_blob_and_base_verify(self):
        import hashlib
        blob = b'patch'
        pin = hashlib.sha256(blob).hexdigest()
        self.assertEqual(verify_handoff(
            blob, 'sha256:' + pin,
            current_base_sha='abc123', blob_base_sha='abc123',
        ), pin)
