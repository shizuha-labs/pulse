"""PLS-834: pulse_link_pr rejects new singular Origin /pull/N and supersedes legacy rows."""

from __future__ import annotations

import unittest
from unittest import mock

import pulse_server


class PulseLinkPrSingularTests(unittest.TestCase):
    def test_new_singular_origin_url_fails_closed(self):
        out = pulse_server.link_pr(
            "PLAT-1",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337",
        )
        self.assertIn("Error:", out)
        self.assertIn("/pulls/337", out)
        self.assertIn("malformed", out.lower())

    def test_singular_with_query_fails_closed_even_with_repo_override(self):
        """rui P1: query/fragment must not fall through to generic append."""
        for url in (
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337?x=1",
            "https://origin.shizuha.com/shizuha-labs/deploy/pull/337#frag",
            "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337?x=1",
            "http://origin.shizuha.com/shizuha-labs/deploy/pulls/337",
            "https://user:pass@origin.shizuha.com/shizuha-labs/deploy/pulls/337",
            "https://origin.shizuha.com:8443/shizuha-labs/deploy/pulls/337",
        ):
            with self.subTest(url=url):
                with mock.patch.object(pulse_server, "api_request") as api:
                    out = pulse_server.link_pr(
                        "PLAT-1",
                        url,
                        repo="shizuha-labs/deploy",
                        pr_number=337,
                    )
                self.assertIn("Error:", out, url)
                self.assertIn("/pulls/337", out)
                api.assert_not_called()

    def test_canonical_link_supersedes_legacy_singular_row(self):
        item = {
            "id": 1,
            "item_key": "PLAT-5671",
            "metadata": {
                "pull_requests": [
                    {
                        "url": "https://origin.shizuha.com/shizuha-labs/deploy/pull/337",
                        "status": "open",
                        "repo": "shizuha-labs/deploy",
                        "number": 337,
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
            "state": "closed",
            "merged": True,
            "merged_at": "2026-08-02T00:00:00Z",
            "head": {"sha": "a" * 40},
        }
        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = fj_payload

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.os.environ, "get", side_effect=lambda k, d=None: "tok" if k in ("FORGEJO_TOKEN", "ORIGIN_TOKEN") else d), \
             mock.patch.object(pulse_server.requests, "get", return_value=resp):
            out = pulse_server.link_pr(
                "PLAT-5671",
                "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337",
            )

        self.assertIn("Linked", out)
        self.assertTrue(patches)
        prs = patches[-1]["metadata"]["pull_requests"]
        urls = {p["url"]: p for p in prs}
        self.assertIn("https://origin.shizuha.com/shizuha-labs/deploy/pulls/337", urls)
        legacy = urls["https://origin.shizuha.com/shizuha-labs/deploy/pull/337"]
        self.assertEqual(
            legacy.get("superseded_by"),
            "https://origin.shizuha.com/shizuha-labs/deploy/pulls/337",
        )


if __name__ == "__main__":
    unittest.main()
