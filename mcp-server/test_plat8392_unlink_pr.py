"""PLAT-8392: ``pulse_unlink_pr`` — the PR-link purge path.

``pulse_link_pr`` refreshes/supersedes but can never REMOVE a row, so a stale
retired-hostname link (``git.shizuha.com``, PLS-1218/PLS-834 class) was
permanently un-purgeable and wedged the PLAT-1296/E review re-entry gate.
Contract locked here:

- exact-URL match removes that row;
- identity match (owner/repo/number) applies ONLY to retired-shape rows
  (legacy ``git.shizuha.com`` host, legacy singular ``/pull/N``) — a canonical
  ``origin.shizuha.com /pulls/N`` row is never removed by identity alone;
- the removed row is ARCHIVED under ``metadata['removed_pull_requests']`` with
  reason/timestamp/actor — audit history is never deleted;
- no match → explicit no-op message, no write.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp-server",
))
os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "off")

import pulse_server  # noqa: E402

LEGACY = "https://git.shizuha.com/shizuha-labs/hive/pulls/277"
CANONICAL = "https://origin.shizuha.com/shizuha-labs/hive/pulls/277"
SINGULAR = "https://origin.shizuha.com/shizuha-labs/hive/pull/277"


def _item(prs):
    return {"id": 1, "item_key": "PLAT-7725", "metadata": {"pull_requests": prs}}


class UnlinkPrTests(unittest.TestCase):
    def _run(self, item, pr_url, reason=""):
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request):
            out = pulse_server.unlink_pr("PLAT-7725", pr_url, reason=reason)
        return out, patches

    def test_exact_url_match_removes_row_and_archives(self):
        item = _item([
            {"url": LEGACY, "status": "open", "head_sha": ""},
            {"url": CANONICAL, "status": "merged", "head_sha": "f0307bf"},
        ])
        out, patches = self._run(item, LEGACY, reason="retired hostname")
        self.assertIn("Unlinked PR", out)
        self.assertEqual(len(patches), 1)
        prs = item["metadata"]["pull_requests"]
        self.assertEqual([r["url"] for r in prs], [CANONICAL])
        archive = item["metadata"]["removed_pull_requests"]
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]["url"], LEGACY)
        self.assertEqual(archive[0]["removed_reason"], "retired hostname")
        self.assertEqual(archive[0]["removed_by"], "pulse_unlink_pr")
        self.assertIn("removed_at", archive[0])

    def test_identity_match_removes_retired_shape_row(self):
        """Legacy-hostname row purged by identity when the canonical URL is
        given — the PLAT-7725 cleanup shape."""
        item = _item([
            {"url": LEGACY, "status": "open", "head_sha": ""},
            {"url": CANONICAL, "status": "open", "head_sha": "f0307bf"},
        ])
        out, _ = self._run(item, CANONICAL)
        self.assertIn("Unlinked PR", out)
        self.assertEqual(
            [r["url"] for r in item["metadata"]["pull_requests"]], [CANONICAL],
        )

    def test_identity_match_never_removes_canonical_row(self):
        """A canonical Origin plural row must not be removable by identity
        alone — only by its exact URL."""
        item = _item([{"url": CANONICAL, "status": "open", "head_sha": "f0307bf"}])
        out, patches = self._run(item, LEGACY)
        self.assertIn("No matching PR row", out)
        self.assertEqual(patches, [])
        self.assertEqual(len(item["metadata"]["pull_requests"]), 1)

    def test_singular_row_identity_match(self):
        item = _item([
            {"url": SINGULAR, "status": "open", "head_sha": ""},
            {"url": CANONICAL, "status": "open", "head_sha": "f0307bf"},
        ])
        out, _ = self._run(item, CANONICAL)
        self.assertIn("Unlinked PR", out)
        self.assertEqual(
            [r["url"] for r in item["metadata"]["pull_requests"]], [CANONICAL],
        )

    def test_no_match_is_explicit_noop(self):
        item = _item([{"url": CANONICAL, "status": "open", "head_sha": "f0307bf"}])
        out, patches = self._run(item, "https://origin.shizuha.com/shizuha-labs/other/pulls/9")
        self.assertIn("No matching PR row", out)
        self.assertEqual(patches, [])

    def test_no_pr_rows_noop(self):
        item = _item([])
        out, patches = self._run(item, LEGACY)
        self.assertIn("no linked PR rows", out)
        self.assertEqual(patches, [])


if __name__ == "__main__":
    unittest.main()
