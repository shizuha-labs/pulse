import ipaddress
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse
from unittest import TestCase
from unittest import skipUnless

import redis
from django.conf import settings

from scripts.hermetic_socket_guard import HermeticityError


class Orig124AgentGateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path(".ci-local-gate.sh").read_text()

    def test_source_is_streamed_to_dind_instead_of_bind_mounted(self):
        self.assertIn('docker cp "$PWD/." "$APP:/workspace"', self.script)
        self.assertIn(
            'docker cp "$LOCAL_SOURCE_ARCHIVE" "$APP:/tmp/local-source.tar"',
            self.script,
        )
        self.assertIn('tar -xf /tmp/local-source.tar -C /app', self.script)
        self.assertNotIn('-v "$PWD', self.script)

    def test_unreachable_live_image_has_frozen_lock_fallback(self):
        self.assertIn('SHIZUHA_CI_FALLBACK_IMAGE:-python:3.12-bookworm', self.script)
        self.assertIn("uv sync --frozen --no-dev --no-install-project --no-editable", self.script)
        self.assertNotIn("apt-get", self.script)

    def test_datastores_are_exposed_only_on_app_loopback(self):
        self.assertIn('-p 127.0.0.1::5432', self.script)
        self.assertIn('-p 127.0.0.1::6379', self.script)
        self.assertIn('POSTGRES_HOST=127.0.0.1', self.script)
        self.assertIn('REDIS_URL=redis://127.0.0.1:$RD_PORT/5', self.script)
        self.assertIn('docker create --name "$APP" --network host', self.script)
        self.assertNotIn('-e SHIZUHA_HERMETIC_ALLOWED_HOSTS=', self.script)

    def test_focused_django_test_label_is_supported_and_validated(self):
        self.assertIn('TEST_TARGET="${SHIZUHA_TEST_TARGET:-tasks}"', self.script)
        self.assertIn("test '$TEST_TARGET'", self.script)
        self.assertIn(
            'docker cp "$PWD/.ci-local-gate.sh" "$APP:/app/.ci-local-gate.sh"',
            self.script,
        )

    def test_live_image_stale_migrations_are_removed_before_graph_check(self):
        self.assertIn(
            'ls -1 "$PWD"/tasks/migrations/0*.py',
            self.script,
        )
        self.assertIn(
            'docker cp "$LOCAL_MIGRATION_LIST" "$APP:/tmp/local-migration-list"',
            self.script,
        )
        self.assertIn(
            'grep -qxF \\"\\$base\\" /tmp/local-migration-list || rm -f \\"\\$f\\"',
            self.script,
        )


@skipUnless(os.environ.get('SHIZUHA_SCRATCH_GATE') == '1',
            'scratch-fallback topology regression')
class Orig157ScratchFallbackLoopbackTests(TestCase):
    """Exercise real Redis while the PLAT-1358 socket guard is installed."""

    def test_real_redis_uses_loopback_and_bridge_escape_is_blocked(self):
        redis_url = settings.REDIS_URL
        redis_host = urlparse(redis_url).hostname
        self.assertTrue(
            redis_host and ipaddress.ip_address(redis_host).is_loopback,
            f'scratch Redis must be loopback, got {redis_url!r}',
        )
        self.assertTrue(
            ipaddress.ip_address(settings.DATABASES['default']['HOST']).is_loopback,
            'scratch PostgreSQL must be loopback',
        )
        self.assertNotIn('SHIZUHA_HERMETIC_ALLOWED_HOSTS', os.environ)

        # Negative control: the same test-owned Redis sidecar is also present on
        # the private bridge, but direct access to that non-loopback address must
        # fail at the hermetic guard before a TCP connection is opened.
        bridge_host = os.environ['SHIZUHA_SCRATCH_GATE_REDIS_SIDECAR_HOST']
        self.assertFalse(ipaddress.ip_address(bridge_host).is_loopback)
        with self.assertRaises(HermeticityError):
            socket.create_connection((bridge_host, 6379), timeout=1)

        key = f'orig157:scratch-loopback:{uuid.uuid4().hex}'
        client = redis.Redis.from_url(redis_url)
        self.addCleanup(client.delete, key)
        self.assertTrue(client.set(key, b'loopback-only', ex=30))
        self.assertEqual(client.get(key), b'loopback-only')
        self.assertEqual(client.delete(key), 1)
