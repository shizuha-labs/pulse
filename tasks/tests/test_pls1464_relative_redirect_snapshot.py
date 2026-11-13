"""PLS-1464 follow-through: the reviewer-disposition snapshot authority must
resolve RELATIVE redirect Locations from renamed repos against the configured
Origin host (found live 2026-11-13 on PLAT-6178: shizuha-beta -> shizuha rename
emits a relative 301; the pre-fix guard rejected it, wedging Review Passed for
every renamed-repo carrier)."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services import workflow_service as ws


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


HEAD = 'a' * 40
PR_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha-beta/pulls/358'
ORIGIN = 'http://forgejo-http.origin.svc.cluster.local'


class RelativeRedirectSnapshotTests(SimpleTestCase):
    def _item(self):
        meta = {
            'review_bounce_pr_heads': {PR_URL: HEAD},
            'pull_requests': [{'url': PR_URL, 'head_sha': HEAD, 'status': 'open'}],
        }
        return SimpleNamespace(metadata=meta, save=lambda **kw: None)

    def test_relative_location_is_resolved_and_snapshot_succeeds(self):
        item = self._item()
        calls = []
        legacy_base = ORIGIN + '/api/v1/repos/shizuha-labs/shizuha-beta/pulls/358'

        def fake_get(url, headers=None, timeout=5, allow_redirects=False, **kw):
            calls.append(url)
            if url == legacy_base:
                # The renamed-repo path 301s with a RELATIVE Location.
                return _Resp(301, None, {'Location': '/api/v1/repos/shizuha-labs/shizuha/pulls/358'})
            if url.endswith('/pulls/358'):
                return _Resp(200, {'head': {'sha': HEAD}})
            if '/reviews' in url:
                return _Resp(200, [])
            raise AssertionError('unexpected url ' + url)

        with mock.patch.dict('os.environ', {'ORIGIN_URL': ORIGIN, 'FORGEJO_TOKEN': 't'}):
            with mock.patch('requests.get', side_effect=fake_get):
                snapshot = ws.WorkflowService._refresh_reviewer_request_snapshot(item)

        self.assertIsNotNone(snapshot, 'relative redirect must resolve, not wedge')
        self.assertEqual(snapshot['ids'], [])
        self.assertEqual(snapshot['heads'], {PR_URL: HEAD})
        self.assertEqual(item.metadata['open_change_request_ids'], [])
        # The followed request went to the resolved absolute same-host URL.
        self.assertIn(ORIGIN + '/api/v1/repos/shizuha-labs/shizuha/pulls/358', calls)

    def test_cross_host_absolute_location_still_rejected(self):
        item = self._item()

        def fake_get(url, headers=None, timeout=5, allow_redirects=False, **kw):
            if url.endswith('/pulls/358'):
                return _Resp(301, None, {'Location': 'https://evil.example/api/v1/repos/shizuha-labs/shizuha/pulls/358'})
            raise AssertionError('must not follow cross-host redirect')

        with mock.patch.dict('os.environ', {'ORIGIN_URL': ORIGIN, 'FORGEJO_TOKEN': 't'}):
            with mock.patch('requests.get', side_effect=fake_get):
                snapshot = ws.WorkflowService._refresh_reviewer_request_snapshot(item)

        self.assertIsNone(snapshot, 'cross-host redirect must stay rejected')
