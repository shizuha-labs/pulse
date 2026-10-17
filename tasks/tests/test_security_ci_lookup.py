"""security-ci lookup must not AND search= with source_id= (ledger remint)."""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def _load_script():
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'security-ci-to-pulse.py'
    spec = importlib.util.spec_from_file_location('security_ci_to_pulse', script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SecurityCiLookupTests(TestCase):
    def test_find_existing_queries_source_id_without_search(self):
        module = _load_script()
        urls = []

        # PLAT-5442: the list serializer omits source_id (PLAT-2688), so the
        # positive-confirmation guard detail-fetches each candidate. The fake
        # mirrors Pulse's real shapes: list rows without source_id, detail
        # payloads WITH it.
        def fake_request(method, url, token, body=None):
            urls.append(url)
            if '/items/?' in url:
                return {'results': [{'id': 'uuid-1', 'item_key': 'PLAT-5135'}]}
            return {
                'id': 'uuid-1',
                'item_key': 'PLAT-5135',
                'source_id': 'security-ci:shizuha-labs/cortex:ledger',
            }

        with patch.object(module, 'pulse_request', fake_request):
            found = module.pulse_find_existing(
                'http://pulse/api', 'tok',
                'security-ci:shizuha-labs/cortex:ledger',
            )
        self.assertEqual(found['item_key'], 'PLAT-5135')
        # Exactly one list query + one detail fetch (positive confirmation).
        self.assertEqual(len(urls), 2)
        list_url, detail_url = urls
        self.assertIn('/items/?', list_url)
        self.assertIn('source_id=', list_url)
        self.assertNotIn('search=', list_url)
        self.assertIn('/items/uuid-1/', detail_url)

    # ── PLAT-5442 ledger-absence probe (zero exact rows on a :ledger key) ──

    def test_probe_detail_fetch_failure_fails_closed_absence_unproven(self):
        import contextlib
        import io

        # The zero-rows probe detail-fetches each title-probe row. A row that
        # cannot be fetched proves nothing either way: the loop WARN+skips it
        # (mirroring the confirmation loop) but absence stays UNPROVEN — the
        # mint must stay fail-closed instead of silently minting a duplicate.
        def fake_request(method, url, token, body=None):
            if '/items/?' in url:
                if 'search=' in url:
                    return {'results': [{'id': 'uuid-9', 'item_key': 'PLAT-9999'}]}
                return {'results': []}
            raise RuntimeError('detail fetch 503')

        module = _load_script()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(RuntimeError) as ctx:
                with patch.object(module, 'pulse_request', fake_request):
                    module.pulse_find_existing_all(
                        'http://pulse/api', 'tok',
                        'security-ci:shizuha-labs/cortex:ledger',
                    )
        self.assertIn('UNPROVEN', str(ctx.exception))
        self.assertIn('uuid-9', stderr.getvalue())
        self.assertIn('proves nothing either way', stderr.getvalue())

    def test_probe_positively_confirmed_ledger_still_fails_closed(self):
        # A title-probe row whose detail carries the exact source_id proves
        # the ledger EXISTS behind a silently-ignored filter — hard raise.
        def fake_request(method, url, token, body=None):
            if '/items/?' in url:
                if 'search=' in url:
                    return {'results': [{'id': 'uuid-2', 'item_key': 'PLAT-5430'}]}
                return {'results': []}
            return {
                'id': 'uuid-2', 'item_key': 'PLAT-5430',
                'source_id': 'security-ci:shizuha-labs/cortex:ledger',
            }

        module = _load_script()
        with patch.object(module, 'pulse_request', fake_request):
            with self.assertRaises(RuntimeError) as ctx:
                module.pulse_find_existing_all(
                    'http://pulse/api', 'tok',
                    'security-ci:shizuha-labs/cortex:ledger',
                )
        self.assertIn('EXISTS', str(ctx.exception))

    def test_probe_absence_proven_when_all_rows_fetch_non_matching(self):
        # Title-probe rows that fetch fine but carry a different source_id are
        # unrelated lookalikes: absence is proven, empty match set returns.
        def fake_request(method, url, token, body=None):
            if '/items/?' in url:
                if 'search=' in url:
                    return {'results': [{'id': 'uuid-3', 'item_key': 'PLAT-5725'}]}
                return {'results': []}
            return {
                'id': 'uuid-3', 'item_key': 'PLAT-5725',
                'source_id': 'security-ci:other/repo:finding',
            }

        module = _load_script()
        with patch.object(module, 'pulse_request', fake_request):
            found = module.pulse_find_existing_all(
                'http://pulse/api', 'tok',
                'security-ci:shizuha-labs/cortex:ledger',
            )
        self.assertEqual(found, [])
