"""PLS-1226: hostile-host PR URLs must fail closed at pulse_link_pr ingress.

Live QA finding (zen, production @ 40dfc764): pulse_link_pr accepted
``https://evil.example.com/shizuha-labs/deploy/pulls/999888`` and persisted it
as an ``open`` no-provenance row — fail-open, violating the PLS-834 acceptance
("hostile host/query/fragment/different coordinates stay fail-closed"). The
malformed-path guard fired for path/query/fragment variants but the HOST check
was missing on the canonical-path shape: the ingress guard only inspected
Origin-ish hosts and returned None (→ verbatim persist) for everything else.

Contract locked here:
- ANY scheme:// URL that is not a trusted provider shape (canonical Origin
  plural, canonical GitHub singular) is rejected before persistence with an
  explicit untrusted-host error — no row, no provider request;
- short keys (owner/repo#N) and the no-pr sentinel are not URLs and keep
  their existing handling;
- a DEFINITIVE provider 404 on a canonical URL refuses to link (never mints
  an unconfirmable open obligation — the PLAT-5671 wedge class); transient
  failures keep the deferred-provenance design.
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
from tasks.origin_provenance import origin_pr_link_ingress_error  # noqa: E402

HOSTILE = "https://evil.example.com/shizuha-labs/deploy/pulls/999888"
CANONICAL = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/999888"


class HostileHostIngressTests(unittest.TestCase):
    def test_guard_rejects_hostile_host_canonical_path_shape(self):
        err = origin_pr_link_ingress_error(HOSTILE)
        self.assertIsNotNone(err)
        self.assertIn("untrusted PR URL host", err)
        self.assertIn("evil.example.com", err)

    def test_guard_rejects_other_schemes(self):
        for raw in (
            "ftp://evil.example.com/shizuha-labs/deploy/pulls/1",
            "javascript://evil.example.com/x",
        ):
            err = origin_pr_link_ingress_error(raw)
            self.assertIsNotNone(err, raw)
            self.assertIn("untrusted PR URL host", err)

    def test_guard_still_allows_trusted_shapes(self):
        self.assertIsNone(origin_pr_link_ingress_error(CANONICAL))
        mirror = origin_pr_link_ingress_error(
            "https://github.com/shizuha-labs/deploy/pull/999888")
        self.assertIn("read-only mirror", mirror)
        self.assertIsNone(origin_pr_link_ingress_error("shizuha-labs/deploy#999888"))

    def test_guard_still_rejects_legacy_host_with_canonical_guidance(self):
        err = origin_pr_link_ingress_error(
            "https://git.shizuha.com/shizuha-labs/deploy/pulls/1")
        self.assertIsNotNone(err)
        self.assertIn("malformed Origin PR URL", err)

    def test_link_pr_rejects_hostile_host_without_persistence(self):
        item = {"id": 1, "item_key": "PLS-834",
                "metadata": {"pull_requests": []}}
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request):
            out = pulse_server.link_pr("PLS-834", HOSTILE)
        self.assertIn("untrusted PR URL host", out)
        self.assertEqual(patches, [])
        self.assertEqual(item["metadata"]["pull_requests"], [])


class Provider404Tests(unittest.TestCase):
    def _run(self, pr_url, status_code, token="t"):
        item = {"id": 1, "item_key": "PLS-834",
                "metadata": {"pull_requests": []}}
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        resp = mock.Mock(ok=False, status_code=status_code)
        env = {"FORGEJO_TOKEN": token, "GITHUB_TOKEN": token,
               "ORIGIN_URL": "https://origin.shizuha",
               "PULSE_SUBSTRATE_CAP_MODE": "off"}
        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(pulse_server.requests, "get", return_value=resp):
            out = pulse_server.link_pr("PLS-834", pr_url)
        return out, patches, item

    def test_origin_404_refuses_to_link(self):
        out, patches, item = self._run(CANONICAL, 404)
        self.assertIn("PR not found on Origin", out)
        self.assertEqual(patches, [])
        self.assertEqual(item["metadata"]["pull_requests"], [])

    def _run_gh(self, pr_url, status_code):
        item = {"id": 1, "item_key": "PLS-834",
                "metadata": {"pull_requests": []}}
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        resp = mock.Mock(ok=False, status_code=status_code)
        env = {"GITHUB_TOKEN": "t", "ORIGIN_URL": "https://origin.shizuha",
               "PULSE_SUBSTRATE_CAP_MODE": "off"}
        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(pulse_server.requests, "get", return_value=resp):
            out = pulse_server.link_pr("PLS-834", pr_url)
        return out, patches, item

    def test_github_link_is_refused_without_calling_github(self):
        out, patches, item = self._run_gh(
            "https://github.com/shizuha-labs/deploy/pull/999888", 404)
        self.assertIn("read-only mirror", out)
        self.assertEqual(patches, [])
        self.assertEqual(item["metadata"]["pull_requests"], [])

    def test_transient_5xx_keeps_deferred_provenance(self):
        """A 500 is transient — the deferred-provenance design persists the
        row (open, no head) for the reconciler to confirm later."""
        out, patches, item = self._run(CANONICAL, 500)
        self.assertIn("Linked PR", out)
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(item["metadata"]["pull_requests"]), 1)


if __name__ == "__main__":
    unittest.main()
