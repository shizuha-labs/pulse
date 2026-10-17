"""
Regression tests for PLS-220: linked PRs must render with their stored
`owner/repo`, never a bare `#N`.

Two valid fix-PRs (CON-131 -> shizuha-labs/connect#97, HIVE-331 ->
shizuha-labs/hive#156) were bounced from review because the tool layer rendered
`PRs: #97 (open)` / `#156 (open)` and the review flow resolved the bare number
against a default repo (shizuha-tasks), where #97 and #156 also exist. These
tests pin repo-qualified rendering so the mis-resolution is impossible from tool
output alone.
"""
import os
import unittest

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class FmtPrLinksTests(unittest.TestCase):
    def test_flat_pull_requests_render_repo_qualified(self):
        item = {"pull_requests": [
            {"number": 97, "status": "open", "repo": "shizuha-labs/connect",
             "url": "https://github.com/shizuha-labs/connect/pull/97"},
        ]}
        self.assertEqual(pulse_server._fmt_pr_links(item), "shizuha-labs/connect#97 (open)")

    def test_nested_metadata_pull_requests_render_repo_qualified(self):
        item = {"metadata": {"pull_requests": [
            {"number": 156, "status": "open", "repo": "shizuha-labs/hive",
             "url": "https://github.com/shizuha-labs/hive/pull/156"},
        ]}}
        self.assertEqual(pulse_server._fmt_pr_links(item), "shizuha-labs/hive#156 (open)")

    def test_bare_number_never_emitted_when_repo_present(self):
        # The exact CON-131/HIVE-331 collision: #97 and #156 also exist in
        # shizuha-tasks. Output must not contain an ambiguous bare "#N".
        item = {"pull_requests": [
            {"number": 97, "status": "open", "repo": "shizuha-labs/connect",
             "url": "https://github.com/shizuha-labs/connect/pull/97"},
            {"number": 156, "status": "open", "repo": "shizuha-labs/hive",
             "url": "https://github.com/shizuha-labs/hive/pull/156"},
        ]}
        out = pulse_server._fmt_pr_links(item)
        self.assertIn("shizuha-labs/connect#97 (open)", out)
        self.assertIn("shizuha-labs/hive#156 (open)", out)
        # No bare "#97"/"#156" token that a consumer could resolve against a
        # default repo.
        self.assertNotIn(" #97", " " + out)
        self.assertNotIn(",#97", out)
        self.assertNotIn("(open), #156", out)

    def test_multiple_prs_joined(self):
        item = {"pull_requests": [
            {"number": 97, "status": "open", "repo": "shizuha-labs/connect"},
            {"number": 12, "status": "merged", "repo": "shizuha-labs/deploy"},
        ]}
        self.assertEqual(
            pulse_server._fmt_pr_links(item),
            "shizuha-labs/connect#97 (open), shizuha-labs/deploy#12 (merged)",
        )

    def test_detail_include_url_appends_canonical_url(self):
        item = {"pull_requests": [
            {"number": 97, "status": "open", "repo": "shizuha-labs/connect",
             "url": "https://github.com/shizuha-labs/connect/pull/97"},
        ]}
        out = pulse_server._fmt_pr_links(item, include_url=True)
        self.assertEqual(
            out,
            "shizuha-labs/connect#97 (open) — https://github.com/shizuha-labs/connect/pull/97",
        )

    def test_legacy_link_without_repo_falls_back_to_url_not_bare_number(self):
        item = {"pull_requests": [
            {"number": 42, "status": "open",
             "url": "https://github.com/shizuha-labs/id/pull/42"},
        ]}
        self.assertEqual(
            pulse_server._fmt_pr_links(item),
            "https://github.com/shizuha-labs/id/pull/42 (open)",
        )

    def test_legacy_link_without_repo_or_url_uses_bare_number_last_resort(self):
        item = {"pull_requests": [{"number": 42, "status": "open"}]}
        self.assertEqual(pulse_server._fmt_pr_links(item), "#42 (open)")

    def test_no_prs_returns_empty(self):
        self.assertEqual(pulse_server._fmt_pr_links({}), "")
        self.assertEqual(pulse_server._fmt_pr_links({"pull_requests": []}), "")

    def test_include_url_not_duplicated_when_ref_is_url(self):
        # Legacy link rendered as URL must not append the URL a second time.
        item = {"pull_requests": [
            {"number": 42, "status": "open",
             "url": "https://github.com/shizuha-labs/id/pull/42"},
        ]}
        out = pulse_server._fmt_pr_links(item, include_url=True)
        self.assertEqual(out.count("https://github.com/shizuha-labs/id/pull/42"), 1)


if __name__ == "__main__":
    unittest.main()
