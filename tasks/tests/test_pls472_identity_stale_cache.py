"""PLS-472: Pulse resolver tolerates a transient shizuha-id lookup timeout with
a bounded last-known-good identity cache.

A transient shizuha-id outage must not silently return None and orphan
otherwise routable team work. On timeout the resolver serves the bounded
last-known-good result (with fail-loud freshness telemetry); security/review
authority elevation callers keep allow_stale=False (default) so stale data is
never used to elevate.
"""
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from tasks.user_service import (
    _BoundedIdentityCache,
    _IDENTITY_CACHE_MAX,
    _IDENTITY_CACHE_STALE_GRACE_SECONDS,
    UserService,
)


class BoundedIdentityCacheTests(SimpleTestCase):
    def setUp(self):
        self.cache = _BoundedIdentityCache(max_entries=2, ttl=60)

    def test_fresh_entry_is_returned(self):
        self.cache.set('k', {'id': 1}, now=1000.0)
        self.assertEqual(self.cache.get('k', now=1010.0), {'id': 1})

    def test_expired_entry_is_not_returned_fresh(self):
        self.cache.set('k', {'id': 1}, now=1000.0)
        self.assertIsNone(self.cache.get('k', now=1100.0))  # > 60s TTL

    def test_stale_grace_serves_within_window(self):
        self.cache.set('k', {'id': 1}, now=1000.0)
        # Within the 900s stale-grace window.
        self.assertEqual(self.cache.get_stale('k', now=1500.0), {'id': 1})
        # Beyond the stale-grace window.
        self.assertIsNone(self.cache.get_stale('k', now=2000.0))

    def test_cached_miss_never_serves_stale(self):
        self.cache.set('k', None, now=1000.0)
        self.assertIsNone(self.cache.get('k', now=1010.0))
        self.assertIsNone(self.cache.get_stale('k', now=1010.0))

    def test_cache_is_bounded_lru(self):
        self.cache.set('a', {'id': 1}, now=1000.0)
        self.cache.set('b', {'id': 2}, now=1000.0)
        self.cache.set('c', {'id': 3}, now=1000.0)
        self.assertIsNone(self.cache.get('a', now=1000.0))  # evicted (LRU)
        self.assertEqual(self.cache.get('b', now=1000.0), {'id': 2})
        self.assertEqual(self.cache.get('c', now=1000.0), {'id': 3})

    def test_default_max_is_bounded(self):
        self.assertGreaterEqual(_IDENTITY_CACHE_MAX, 1024)


class UserServiceStaleFallbackTests(SimpleTestCase):
    def setUp(self):
        self.service = UserService()
        self.service._identity_cache.clear()

    def _make_session(self, side_effect):
        session = Mock()
        session.get.side_effect = side_effect
        self.service.session = session

    def _force_stale(self, email):
        """Push the cached entry past its TTL but within the stale-grace window."""
        import time as _time
        key = f'identity:email:{email}'
        self.service._identity_cache._data[key] = (
            _time.time() - 400, self.service._identity_cache._data[key][1]
        )

    def test_timeout_serves_last_known_good_when_allowed(self):
        # First lookup succeeds and populates the cache.
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 42, 'email': 'a@x.io'}))
        )
        self.assertEqual(
            self.service.get_user_by_email('a@x.io')['id'], 42
        )
        # Force the entry stale (past TTL, within stale grace), then time out;
        # allow_stale serves the last-known-good identity.
        self._force_stale('a@x.io')
        self._make_session(Mock(side_effect=requests.Timeout('down')))
        with self.assertLogs('tasks.user_service', level='WARNING'):
            result = self.service.get_user_by_email('a@x.io', allow_stale=True)
        self.assertEqual(result['id'], 42)

    def test_timeout_does_not_serve_stale_by_default(self):
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 42, 'email': 'a@x.io'}))
        )
        self.service.get_user_by_email('a@x.io')
        self._force_stale('a@x.io')
        self._make_session(Mock(side_effect=requests.Timeout('down')))
        # Default allow_stale=False: never elevate with stale data.
        self.assertIsNone(self.service.get_user_by_email('a@x.io'))

    def test_404_is_not_orphaned_into_phantom_on_timeout(self):
        # 404 caches an explicit miss; a later timeout must NOT resurrect it.
        self._make_session(Mock(return_value=Mock(status_code=404)))
        self.assertIsNone(self.service.get_user_by_email('missing@x.io'))
        self._make_session(Mock(side_effect=requests.Timeout('down')))
        self.assertIsNone(self.service.get_user_by_email('missing@x.io', allow_stale=True))

    def test_orgs_do_not_cross_via_cache(self):
        # Different emails never share cache entries (org isolation).
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 1, 'email': 'a@org1.io'}))
        )
        self.service.get_user_by_email('a@org1.io')
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 2, 'email': 'b@org2.io'}))
        )
        self.assertEqual(self.service.get_user_by_email('b@org2.io')['id'], 2)
        # a@org1 still cached as its own identity.
        self.assertEqual(self.service.get_user_by_email('a@org1.io')['id'], 1)

    def test_capability_change_is_reflected_after_cache_ttl(self):
        # A stale cached capability must not outlive the TTL for fresh reads.
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 1, 'is_staff': True}))
        )
        self.service.get_user_by_email('a@x.io')
        # Force the cache entry past its TTL.
        key = 'identity:email:a@x.io'
        self.service._identity_cache._data[key] = (
            0.0, self.service._identity_cache._data[key][1]
        )
        self._make_session(
            Mock(return_value=Mock(status_code=200, json=lambda: {'id': 1, 'is_staff': False}))
        )
        self.assertFalse(self.service.get_user_by_email('a@x.io')['is_staff'])
