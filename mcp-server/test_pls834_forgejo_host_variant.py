"""PLS-834 residual (live instance PLAT-7725): legacy ``git.shizuha.com``-host
PR rows.

The same Forgejo instance served PR URLs under ``git.shizuha.com``, and
``pulse_link_pr`` persisted such rows verbatim — ``open``, no ``head_sha`` —
because the ingress lookalike check only recognised ``origin.shizuha`` hosts.
The PLAT-1296 review re-entry gate then flagged the row forever ("no head_sha")
and nothing could refresh, terminalize, or supersede it.

Contract locked here:
- new ``git.shizuha.com`` links fail closed at ingress with the canonical-form
  error (no persistence, no provider request);
- existing ``git.shizuha.com`` rows are identity-matchable for the repair
  paths (supersession, bounce-head repair) — a provider-confirmed canonical
  link annotates the legacy row ``superseded_by`` and mints the canonical
  row, mirroring the reviewed singular ``/pull/N`` behavior;
- hostile variants (other hosts, paths, query/fragment) stay fail-closed.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tasks.origin_provenance import (
    origin_pr_link_ingress_error,
    parse_forgejo_legacy_host_pr_url,
    parse_linked_pr_identity,
    parse_origin_pr_reference,
)

import pulse_server


CANONICAL = "https://origin.shizuha.com/shizuha-labs/hive/pulls/277"
LEGACY_HOST = "https://git.shizuha.com/shizuha-labs/hive/pulls/277"


class ForgejoLegacyHostIngressTests(unittest.TestCase):
    def test_new_legacy_host_url_fails_closed(self):
        out = pulse_server.link_pr("PLAT-7725", LEGACY_HOST)
        self.assertIn("Error:", out)
        self.assertIn("/pulls/277", out)
        self.assertIn("malformed", out.lower())

    def test_new_legacy_host_url_fails_closed_even_with_overrides(self):
        with mock.patch.object(pulse_server, "api_request") as api:
            out = pulse_server.link_pr(
                "PLAT-7725",
                LEGACY_HOST,
                repo="shizuha-labs/hive",
                pr_number=277,
            )
        self.assertIn("Error:", out)
        self.assertIn("/pulls/277", out)
        api.assert_not_called()

    def test_ingress_error_names_canonical_form(self):
        err = origin_pr_link_ingress_error(LEGACY_HOST)
        self.assertIsNotNone(err)
        self.assertIn(CANONICAL, err)

    def test_unrelated_hosts_fail_closed_untrusted(self):
        # PLS-1226: a genuinely non-Origin URL must not be treated as an
        # Origin lookalike — AND it must no longer flow to the generic append
        # path (that fail-open persisted hostile-host rows). It is rejected as
        # an untrusted host before persistence.
        err = origin_pr_link_ingress_error("https://example.com/foo/bar/pulls/1")
        self.assertIsNotNone(err)
        self.assertIn("untrusted PR URL host", err)
        self.assertIn("example.com", err)
        self.assertNotIn("malformed Origin PR URL", err)

    def test_hostile_variants_fail_closed(self):
        for url in (
            "https://git.shizuha.com/shizuha-labs/hive/pulls/277?x=1",
            "https://git.shizuha.com/shizuha-labs/hive/pulls/277#frag",
            "http://git.shizuha.com/shizuha-labs/hive/pulls/277",
            "https://git.shizuha.com:8443/shizuha-labs/hive/pulls/277",
        ):
            with self.subTest(url=url):
                with mock.patch.object(pulse_server, "api_request") as api:
                    out = pulse_server.link_pr("PLAT-7725", url)
                self.assertIn("Error:", out, url)
                api.assert_not_called()

    def test_lookalike_host_is_not_legacy_origin(self):
        # Exact-host binding: a lookalike host is NOT the legacy Origin host —
        # it flows to the generic non-Origin path like any unknown host.
        from tasks.origin_provenance import origin_pr_link_ingress_error

        # PLS-1226: the lookalike host is NOT the legacy Origin host (parser
        # below) — and it is now rejected as an untrusted host rather than
        # flowing to the generic append path.
        err = origin_pr_link_ingress_error(
            "https://git.shizuha.com.evil.com/shizuha-labs/hive/pulls/277"
        )
        self.assertIsNotNone(err)
        self.assertIn("untrusted PR URL host", err)
        self.assertIn("git.shizuha.com.evil.com", err)
        self.assertIsNone(
            parse_forgejo_legacy_host_pr_url(
                "https://git.shizuha.com.evil.com/shizuha-labs/hive/pulls/277"
            )
        )


class ForgejoLegacyHostIdentityTests(unittest.TestCase):
    def test_legacy_host_parser_matches_both_path_shapes(self):
        for url in (
            LEGACY_HOST,
            "https://git.shizuha.com/shizuha-labs/hive/pull/277",
            LEGACY_HOST + "/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_forgejo_legacy_host_pr_url(url),
                    ("shizuha-labs", "hive", 277),
                )

    def test_legacy_host_parser_rejects_non_pr_paths(self):
        for url in (
            "https://git.shizuha.com/shizuha-labs/hive",
            "https://git.shizuha.com/shizuha-labs/hive/pulls/0",
            "https://git.shizuha.com/shizuha-labs/hive/commits/main",
        ):
            with self.subTest(url=url):
                self.assertIsNone(parse_forgejo_legacy_host_pr_url(url))

    def test_identity_matcher_equates_legacy_host_with_canonical(self):
        self.assertEqual(
            parse_linked_pr_identity(LEGACY_HOST),
            parse_linked_pr_identity(CANONICAL),
        )
        self.assertEqual(parse_linked_pr_identity(LEGACY_HOST)[0], "shizuha-labs")

    def test_canonical_parser_still_rejects_legacy_host(self):
        # The legacy host is an identity/repair target only — never a trusted
        # ingress or API-construction key.
        self.assertIsNone(parse_origin_pr_reference(LEGACY_HOST))


class ForgejoLegacyHostSupersessionTests(unittest.TestCase):
    def _link_canonical_over_legacy_row(self, legacy_url, fj_state="open"):
        item = {
            "id": 1,
            "item_key": "PLAT-7725",
            "metadata": {
                "pull_requests": [
                    {
                        "url": legacy_url,
                        "status": "open",
                        "repo": "shizuha-labs/hive",
                        "number": 277,
                    }
                ]
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError((method, path))

        fj_payload = {
            "state": fj_state,
            "merged": fj_state == "merged",
            "merged_at": "2026-09-09T00:00:00Z" if fj_state == "merged" else None,
            "head": {"sha": "b" * 40},
        }
        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = fj_payload

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.os.environ, "get", side_effect=lambda k, d=None: "tok" if k in ("FORGEJO_TOKEN", "ORIGIN_TOKEN") else d), \
             mock.patch.object(pulse_server.requests, "get", return_value=resp):
            out = pulse_server.link_pr("PLAT-7725", CANONICAL)

        return out, patches

    def test_canonical_link_supersedes_legacy_row_and_mints_canonical(self):
        """Mirrors the reviewed singular-row behavior: the legacy-host row is
        NOT identity-refreshed in place — the canonical link mints a fresh
        provider-confirmed row and annotates the legacy row superseded_by
        (immutable URL preserved). The re-entry gate then sees one active
        canonical obligation with provenance and one retired audit row."""
        out, patches = self._link_canonical_over_legacy_row(LEGACY_HOST)
        self.assertIn("Linked", out)
        self.assertTrue(patches)
        prs = patches[-1]["metadata"]["pull_requests"]
        urls = {p["url"]: p for p in prs}
        self.assertIn(CANONICAL, urls)
        self.assertEqual(urls[CANONICAL]["head_sha"], "b" * 40)
        legacy = urls[LEGACY_HOST]
        self.assertEqual(legacy.get("superseded_by"), CANONICAL)

    def test_merged_canonical_terminalizes_legacy_row(self):
        out, patches = self._link_canonical_over_legacy_row(
            LEGACY_HOST, fj_state="merged"
        )
        self.assertTrue(patches)
        prs = patches[-1]["metadata"]["pull_requests"]
        urls = {p["url"]: p for p in prs}
        self.assertEqual(urls[CANONICAL]["status"], "merged")
        self.assertEqual(
            urls[LEGACY_HOST].get("superseded_by"), CANONICAL,
        )

    def test_unrelated_legacy_rows_are_untouched(self):
        item = {
            "id": 1,
            "item_key": "PLAT-7725",
            "metadata": {
                "pull_requests": [
                    {
                        "url": "https://git.shizuha.com/shizuha-labs/other/pulls/999",
                        "status": "open",
                    }
                ]
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError((method, path))

        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = {
            "state": "open",
            "merged": False,
            "head": {"sha": "c" * 40},
        }

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.os.environ, "get", side_effect=lambda k, d=None: "tok" if k in ("FORGEJO_TOKEN", "ORIGIN_TOKEN") else d), \
             mock.patch.object(pulse_server.requests, "get", return_value=resp):
            pulse_server.link_pr("PLAT-7725", CANONICAL)

        prs = patches[-1]["metadata"]["pull_requests"]
        unrelated = [p for p in prs if p["url"].endswith("/other/pulls/999")]
        self.assertEqual(len(unrelated), 1)
        self.assertNotIn("superseded_by", unrelated[0])


if __name__ == "__main__":
    unittest.main()
