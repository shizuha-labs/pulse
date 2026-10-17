from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from scripts.live_pulse_tenant_isolation_probe import (
    Actor,
    PulseProbe,
    parse_args,
)


class _Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _InMemoryProbe(PulseProbe):
    """Exercise the orchestration contract without live credentials/network."""

    def __init__(self):
        super().__init__(
            'https://pulse.invalid/api',
            Actor('A', 'token-a', '1', '101'),
            Actor('B', 'token-b', '2', '202'),
            'field-1', 1.0, 'release-abc',
        )
        self.next_id = 10
        self.items = {}

    def request(self, actor, method, path, *, expected, data=None, params=None,
                extra_headers=None):
        self.evidence.append({'step': f'{actor.label}:{method}:{path}', 'status': 200})
        if path == 'health/data/':
            return _Response(headers={'X-Shizuha-Release': 'release-abc'}), {}
        if method == 'POST' and path == 'items/':
            self.next_id += 1
            row = {'id': str(self.next_id), 'title': data['title'], 'status': 'open'}
            self.items[(actor.label, row['id'])] = row
            return _Response(201), dict(row)
        if method == 'POST' and path in ('comments/', 'work-logs/', 'custom-field-values/'):
            self.next_id += 1
            return _Response(201), {'id': str(self.next_id)}
        if method == 'GET' and path.startswith('items/'):
            item_id = path.split('/')[1]
            own = self.items.get((actor.label, item_id))
            if own is not None:
                return _Response(200), dict(own)
            return _Response(404), {'detail': 'not found'}
        if actor.label == 'A' and method == 'GET' and path == 'custom-field-values/':
            return _Response(200), {'results': []}
        if actor.label == 'A' and method == 'GET':
            return _Response(404), {'detail': 'not found'}
        if method == 'POST' and path == 'items/bulk/':
            item_id = str(data['item_ids'][0])
            operation = data['operation']
            own = (actor.label, item_id) in self.items
            affected = 1 if own else 0
            if own and operation == 'delete':
                self.items.pop((actor.label, item_id))
            key = 'deleted' if operation == 'delete' else 'updated'
            return _Response(200), {key: affected}
        if method == 'DELETE' and path.startswith('items/'):
            item_id = path.split('/')[1]
            self.items.pop((actor.label, item_id), None)
            return _Response(204), None
        raise AssertionError(f'unhandled fake request: {actor.label} {method} {path}')


class LivePulseProbeTests(unittest.TestCase):
    def _args(self):
        return [
            '--base-url', 'https://example.test/pulse/api',
            '--token-a', 'token-a', '--token-b', 'token-b',
            '--project-a', '101', '--project-b', '202',
            '--org-a', '1', '--org-b', '2',
            '--custom-field-id', 'field-1',
        ]

    def test_cli_refuses_non_disjoint_actors(self):
        args = self._args()
        args[args.index('--org-b') + 1] = '1'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(args)

        args = self._args()
        args[args.index('--token-b') + 1] = 'token-a'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(args)

    def test_cli_refuses_plaintext_remote_target(self):
        args = self._args()
        args[args.index('--base-url') + 1] = 'http://example.test/pulse/api'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(args)

    @mock.patch('scripts.live_pulse_tenant_isolation_probe.secrets.token_hex', return_value='nonce')
    def test_full_contract_passes_and_cleans_every_item(self, _nonce):
        probe = _InMemoryProbe()
        result = probe.run()
        cleanup_errors = probe.cleanup()
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(cleanup_errors, [])
        self.assertEqual(probe.items, {})
        rendered = repr(result)
        self.assertNotIn('token-a', rendered)
        self.assertNotIn('token-b', rendered)
        self.assertGreaterEqual(result['checks'], 10)


if __name__ == '__main__':
    unittest.main()
