"""PLS-487 / ADR-0072: django.core.cache is a shared Redis backend in production.

Acceptance covered here:
- Production (non-TESTING) settings resolve CACHES['default'] to RedisCache with
  KEY_PREFIX=pulse:djcache and a finite TIMEOUT (not None).
- Hermetic test settings keep LocMem (no live-Redis dependency).
- Shared-backend atomicity: two racers on Redis SET NX (the primitive behind
  Django 4.2 RedisCache.add) produce exactly one winner. Two independent
  LocMemCache instances both win — proving the partition defect the ticket names.
- The URL helper pins a dedicated DB index when CACHE_REDIS_URL is unset, so
  cache keys never share the Celery broker keyspace.

Note: races use threads (not multiprocessing.fork). Premerge runs with
``--parallel``, and nesting fork inside a parallel worker deadlocks the CI job.
Thread races still exercise the shared Redis store; LocMem partition is shown
via two independent LocMemCache instances (distinct LOCATION = distinct heaps,
the same isolation separate processes get for free).
"""
from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class Pls487SettingsContractTests(unittest.TestCase):
    def test_production_caches_block_is_redis_with_prefix_and_finite_timeout(self):
        """ADR-0072 §1: non-TESTING settings must not resolve to LocMem."""
        from taskmanager import settings as s

        self.assertTrue(hasattr(s, '_pls487_cache_redis_location'))
        src = (ROOT / 'taskmanager' / 'settings.py').read_text()
        self.assertIn(
            "'BACKEND': 'django.core.cache.backends.redis.RedisCache'",
            src,
        )
        self.assertIn("'KEY_PREFIX': 'pulse:djcache'", src)
        self.assertIn("'TIMEOUT': 300", src)
        # Must never set global TIMEOUT=None.
        self.assertNotIn("'TIMEOUT': None", src)
        # Hermetic override must keep LocMem.
        self.assertIn(
            "'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'",
            src,
        )

    def test_hermetic_default_is_locmem(self):
        from django.core.cache import caches
        from taskmanager import test_settings as ts

        backend = caches['default']
        self.assertIn(
            'locmem',
            backend.__class__.__module__ + '.' + backend.__class__.__name__,
        )
        self.assertEqual(
            ts.CACHES['default']['BACKEND'],
            'django.core.cache.backends.locmem.LocMemCache',
        )

    def test_url_helper_pins_dedicated_db_when_cache_url_unset(self):
        from taskmanager.settings import _pls487_cache_redis_location

        with mock.patch.dict(os.environ, {
            'REDIS_URL': 'redis://pulse-redis:6379/0',
            'CELERY_BROKER_URL': 'redis://pulse-redis:6379/0',
        }, clear=False):
            os.environ.pop('CACHE_REDIS_URL', None)
            loc = _pls487_cache_redis_location()
        self.assertEqual(loc, 'redis://pulse-redis:6379/2')

    def test_url_helper_honours_explicit_cache_redis_url(self):
        from taskmanager.settings import _pls487_cache_redis_location

        with mock.patch.dict(os.environ, {
            'CACHE_REDIS_URL': 'redis://pulse-redis:6379/6',
        }, clear=False):
            loc = _pls487_cache_redis_location()
        self.assertEqual(loc, 'redis://pulse-redis:6379/6')


class Pls487SharedBackendAtomicityTests(unittest.TestCase):
    """Prove shared-backend atomicity vs LocMem partition (acceptance item 3)."""

    def test_locmem_partition_lets_both_instances_win(self):
        """Control: two independent LocMem caches both win cache.add (the defect).

        Distinct LOCATION → distinct in-process heaps, the same isolation
        separate gunicorn/Celery processes get under the pre-fix default.
        """
        from django.core.cache.backends.locmem import LocMemCache

        a = LocMemCache('pls487-worker-a', {})
        b = LocMemCache('pls487-worker-b', {})
        key = 'pls487:locmem:race'
        # Clear any prior residue in the class-level store for these locations.
        a.delete(key)
        b.delete(key)
        self.assertTrue(a.add(key, '1', timeout=60))
        self.assertTrue(b.add(key, '1', timeout=60),
                        'independent LocMem instances must both win (partition)')

    def test_shared_redis_set_nx_admits_exactly_one_winner(self):
        """Load-bearing: N threads racing SET NX on Redis → exactly one winner.

        Django 4.2 RedisCache.add is SET NX EX; racing the same primitive is
        the acceptance proof that the shared backend is actually shared and
        atomic across concurrent callers (the multi-worker fan-out case).
        """
        redis_url = os.environ.get(
            'CACHE_REDIS_URL',
            os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/6'),
        )
        race_key = 'pulse:djcache:test:pls487:redis:race'
        try:
            import redis as redis_lib
            probe = redis_lib.Redis.from_url(
                redis_url, socket_connect_timeout=1, socket_timeout=1,
            )
            probe.ping()
            probe.delete(race_key)
        except Exception as exc:  # pragma: no cover - environment-dependent
            self.skipTest(f'Redis unavailable for shared-backend race: {exc}')

        barrier = threading.Barrier(8)

        def _attempt() -> bool:
            import redis as redis_lib
            client = redis_lib.Redis.from_url(
                redis_url, socket_connect_timeout=2, socket_timeout=2,
            )
            barrier.wait(timeout=10)
            return bool(client.set(race_key, b'1', nx=True, ex=60))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: _attempt(), range(8)))

        self.assertEqual(
            results.count(True), 1,
            f'shared Redis must admit exactly one winner; got {results}',
        )
        self.assertEqual(
            results.count(False), 7,
            f'seven losers must observe the key already held; got {results}',
        )

    def test_django_redis_cache_add_is_atomic_when_configured(self):
        """Django RedisCache.add itself is single-winner when pointed at Redis.

        Spins a throwaway RedisCache alias (does not touch hermetic default
        LocMem) so the Django API path is covered, not only redis-py.
        """
        redis_url = os.environ.get(
            'CACHE_REDIS_URL',
            os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/6'),
        )
        try:
            import redis as redis_lib
            redis_lib.Redis.from_url(
                redis_url, socket_connect_timeout=1, socket_timeout=1,
            ).ping()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f'Redis unavailable: {exc}')

        from django.core.cache.backends.redis import RedisCache

        cache = RedisCache(redis_url, {
            'KEY_PREFIX': 'pulse:djcache:test',
            'TIMEOUT': 60,
        })
        key = 'pls487:django-redis-add'
        cache.delete(key)
        self.assertTrue(cache.add(key, 'first', timeout=60))
        self.assertFalse(cache.add(key, 'second', timeout=60),
                         'second add on shared RedisCache must lose')
        self.assertEqual(cache.get(key), 'first')


if __name__ == '__main__':
    unittest.main()
