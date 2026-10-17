"""PLS-496: pulse_link_pr Origin head_sha + host allowlist (revi P1)."""
from __future__ import annotations

import copy
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


ORIGIN_URL = "https://origin.shizuha.com/shizuha-labs/drive/pulls/8"
LOOKALIKE = "https://origin.shizuha.attacker.example/shizuha-labs/drive/pulls/8"
GH_URL = "https://github.com/shizuha-labs/drive/pull/8"


class _Resp:
    def __init__(self, ok, data):
        self.ok = ok
        self._data = data

    def json(self):
        return self._data


class OriginLinkPrHeadShaTests(unittest.TestCase):
    def test_origin_url_fetches_head_sha_via_canonical_base(self):
        item = {"id": 1, "item_key": "PLAT-4253", "metadata": {"pull_requests": []}}
        patches = []
        seen_urls = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            seen_urls.append(url)
            # Must hit fixed base, not attacker host
            self.assertTrue(
                url.startswith("https://origin.shizuha.com/")
                or url.startswith("http://forgejo-http.origin.svc.cluster.local/"),
                f"untrusted fetch target: {url}",
            )
            self.assertIn("/api/v1/repos/shizuha-labs/drive/pulls/8", url)
            if headers:
                self.assertIn("Authorization", headers)
            return _Resp(True, {
                "state": "open",
                "merged": False,
                "head": {"sha": "e0e0374897ae33221b9024036ebd4a746f4b7235"},
            })

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLAT-4253", ORIGIN_URL, repo="shizuha-labs/drive", pr_number=8)

        self.assertIn("Linked PR #8", out)
        pr = patches[0]["metadata"]["pull_requests"][0]
        self.assertEqual(pr["head_sha"], "e0e0374897ae33221b9024036ebd4a746f4b7235")
        self.assertEqual(pr["status"], "open")
        self.assertTrue(seen_urls)

    def test_lookalike_host_never_receives_authenticated_request(self):
        item = {"id": 1, "item_key": "X", "metadata": {"pull_requests": []}}
        seen = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            seen.append((url, headers))
            raise AssertionError(f"must not fetch lookalike: {url}")

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "secret-token"}, clear=False):
            # Still links metadata without head_sha (no trusted fetch)
            out = pulse_server.link_pr("X", LOOKALIKE)

        self.assertEqual(seen, [])
        # Linked without head_sha is OK; critical is no token leak
        self.assertTrue("Linked" in out or "already" in out.lower() or "PR" in out)

    def test_origin_relink_refreshes_stale_head_sha(self):
        item = {
            "id": 1,
            "item_key": "PLAT-4253",
            "metadata": {
                "pull_requests": [{
                    "url": ORIGIN_URL,
                    "repo": "shizuha-labs/drive",
                    "number": 8,
                    "status": "open",
                    "head_sha": "e1af1d3f96fd4fa84593f4ee475612d4497e32fc",
                }]
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
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            return _Resp(True, {
                "state": "open",
                "merged": False,
                "head": {"sha": "e0e0374897ae33221b9024036ebd4a746f4b7235"},
            })

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLAT-4253", ORIGIN_URL)

        self.assertIn("Refreshed", out)
        self.assertEqual(
            patches[0]["metadata"]["pull_requests"][0]["head_sha"],
            "e0e0374897ae33221b9024036ebd4a746f4b7235",
        )

    def test_origin_relink_backfills_partial_null_bounce_head_idempotently(self):
        """PLS-684: a provider-confirmed relink heals the exact null bounce
        entry without rewriting another PR's historical snapshot."""
        legacy_url = "https://github.com/shizuha-labs/deploy/pull/494"
        origin_url = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/7"
        current_head = "c4ab822260c6fa05abf0e9615355d4edd8a21860"
        legacy_head = "01df0ca0e6fec4dec98cc857018090407f30be6d"
        item = {
            "id": 1,
            "item_key": "PLAT-4602",
            "metadata": {
                "pull_requests": [
                    {
                        "url": legacy_url,
                        "repo": "shizuha-labs/deploy",
                        "number": 494,
                        "status": "open",
                        "head_sha": legacy_head,
                        "linked_at": "2026-07-13T21:31:51Z",
                    },
                    {
                        "url": origin_url,
                        "repo": "shizuha-labs/deploy",
                        "number": 7,
                        "status": "open",
                        "head_sha": current_head,
                        "linked_at": "2026-07-14T20:48:54Z",
                    },
                ],
                "review_bounce_at": "2026-07-14T20:49:50Z",
                "review_bounce_pr_heads": {
                    legacy_url: legacy_head,
                    origin_url: None,
                },
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return copy.deepcopy(item)
            if method == "PATCH":
                patches.append(copy.deepcopy(data))
                item["metadata"] = copy.deepcopy(data["metadata"])
                return copy.deepcopy(item)
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            return _Resp(True, {
                "state": "open",
                "merged": False,
                "head": {"sha": current_head},
            })

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            first = pulse_server.link_pr("PLAT-4602", origin_url)
            second = pulse_server.link_pr("PLAT-4602", origin_url)

        self.assertIn("Refreshed", first)
        self.assertIn("bounce_head", first)
        self.assertEqual(second, "PR already linked to task #PLAT-4602")
        self.assertEqual(len(patches), 1, "the second relink must be a metadata no-op")
        metadata = patches[0]["metadata"]
        self.assertEqual(metadata["review_bounce_pr_heads"][legacy_url], legacy_head)
        self.assertEqual(metadata["review_bounce_pr_heads"][origin_url], current_head)
        self.assertEqual(metadata["review_bounce_backfill_source"], "pulse_link_pr")
        self.assertTrue(metadata["review_bounce_backfilled_at"])

    def test_origin_relink_replaces_null_mirror_key_without_duplicate(self):
        mirror_url = "https://github.com/shizuha-labs/deploy/pull/7"
        origin_url = "https://origin.shizuha.com/shizuha-labs/deploy/pulls/7"
        current_head = "c4ab822260c6fa05abf0e9615355d4edd8a21860"
        item = {
            "id": 1,
            "item_key": "PLS-684-MIRROR-KEY",
            "metadata": {
                "pull_requests": [{
                    "url": origin_url,
                    "repo": "shizuha-labs/deploy",
                    "number": 7,
                    "status": "open",
                    "head_sha": current_head,
                    "linked_at": "2026-07-14T20:48:54Z",
                }],
                "review_bounce_at": "2026-07-14T20:49:50Z",
                "review_bounce_pr_heads": {mirror_url: None},
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return copy.deepcopy(item)
            if method == "PATCH":
                patches.append(copy.deepcopy(data))
                item["metadata"] = copy.deepcopy(data["metadata"])
                return copy.deepcopy(item)
            raise AssertionError(method)

        with mock.patch.object(
            pulse_server, "api_request", side_effect=api_request,
        ), mock.patch.object(
            pulse_server.requests, "get", return_value=_Resp(True, {
                "state": "open",
                "merged": False,
                "head": {"sha": current_head},
            }),
        ), mock.patch.dict(
            os.environ, {"FORGEJO_TOKEN": "t"}, clear=False,
        ):
            out = pulse_server.link_pr("PLS-684-MIRROR-KEY", origin_url)

        self.assertIn("bounce_head", out)
        self.assertEqual(len(patches), 1)
        self.assertEqual(
            patches[0]["metadata"]["review_bounce_pr_heads"],
            {origin_url: current_head},
        )

    def test_origin_relink_never_overwrites_populated_bounce_head(self):
        """A later provider head refresh must not rewrite audit history."""
        old_head = "75e882bb75e882bb75e882bb75e882bb75e882bb"
        current_head = "c4ab822260c6fa05abf0e9615355d4edd8a21860"
        item = {
            "id": 1,
            "item_key": "PLAT-4602",
            "metadata": {
                "pull_requests": [{
                    "url": ORIGIN_URL,
                    "repo": "shizuha-labs/drive",
                    "number": 8,
                    "status": "open",
                    "head_sha": current_head,
                    "linked_at": "2026-07-14T20:48:54Z",
                }],
                "review_bounce_at": "2026-07-14T20:49:50Z",
                "review_bounce_pr_heads": {ORIGIN_URL: old_head},
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return copy.deepcopy(item)
            if method == "PATCH":
                patches.append(copy.deepcopy(data))
                return copy.deepcopy(item)
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", return_value=_Resp(True, {
                 "state": "open",
                 "merged": False,
                 "head": {"sha": current_head},
             })), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLAT-4602", ORIGIN_URL)

        self.assertEqual(out, "PR already linked to task #PLAT-4602")
        self.assertEqual(patches, [])
        self.assertEqual(
            item["metadata"]["review_bounce_pr_heads"][ORIGIN_URL],
            old_head,
        )

    def test_origin_relink_does_not_synthesize_no_pr_bounce_snapshot(self):
        """Rule 7: a PR linked after a no-PR bounce keeps the explicit-scope
        first-artifact path; relink must not manufacture a bounce head."""
        current_head = "c4ab822260c6fa05abf0e9615355d4edd8a21860"
        item = {
            "id": 1,
            "item_key": "PLAT-4837",
            "metadata": {
                "pull_requests": [{
                    "url": ORIGIN_URL,
                    "repo": "shizuha-labs/drive",
                    "number": 8,
                    "status": "open",
                    "head_sha": current_head,
                    "linked_at": "2026-07-14T20:50:00Z",
                }],
                "review_bounce_at": "2026-07-14T20:49:50Z",
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return copy.deepcopy(item)
            if method == "PATCH":
                patches.append(copy.deepcopy(data))
                return copy.deepcopy(item)
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", return_value=_Resp(True, {
                 "state": "open",
                 "merged": False,
                 "head": {"sha": current_head},
             })), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLAT-4837", ORIGIN_URL)

        self.assertEqual(out, "PR already linked to task #PLAT-4837")
        self.assertEqual(patches, [])
        self.assertNotIn("review_bounce_pr_heads", item["metadata"])

    def test_plain_owner_repo_key_relink_backfills_existing_row(self):
        """Live recurrence: rendered ``owner/repo#N`` keys must refresh too."""
        short_key = "shizuha-labs/shizuha-beta#21"
        item = {
            "id": 1,
            "item_key": "PLAT-4638",
            "metadata": {
                "pull_requests": [{
                    "url": short_key,
                    "repo": "shizuha-labs/shizuha-beta",
                    "number": 21,
                    "status": "open",
                }]
            },
        }
        patches = []
        seen_urls = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                item["metadata"] = data["metadata"]
                return item
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            seen_urls.append(url)
            return _Resp(True, {
                "state": "closed",
                "merged": True,
                "head": {"sha": "befafda13de4"},
            })

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLAT-4638", short_key)

        self.assertIn("Refreshed", out)
        self.assertEqual(
            seen_urls,
            ["https://origin.shizuha.com/api/v1/repos/"
             "shizuha-labs/shizuha-beta/pulls/21"],
        )
        pr = patches[0]["metadata"]["pull_requests"][0]
        self.assertEqual(pr["head_sha"], "befafda13de4")
        self.assertEqual(pr["status"], "merged")

    def test_origin_merged_pr_status_backfill(self):
        item = {
            "id": 1,
            "item_key": "PLS-496",
            "metadata": {
                "pull_requests": [{
                    "url": ORIGIN_URL,
                    "repo": "shizuha-labs/drive",
                    "number": 8,
                    "status": "open",
                    "head_sha": "old",
                }]
            },
        }
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            return _Resp(True, {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-07-15T00:00:00Z",
                "head": {"sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
            })

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "t"}, clear=False):
            out = pulse_server.link_pr("PLS-496", ORIGIN_URL)

        self.assertIn("Refreshed", out)
        pr = patches[0]["metadata"]["pull_requests"][0]
        self.assertEqual(pr["status"], "merged")
        self.assertEqual(pr["head_sha"], "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    def test_github_path_is_refused_without_a_provider_call(self):
        item = {"id": 1, "item_key": "X", "metadata": {"pull_requests": []}}
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        def get(url, headers=None, timeout=None):
            raise AssertionError(f"GitHub must not be queried: {url}")

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", side_effect=get), \
             mock.patch.dict(os.environ, {"GITHUB_TOKEN": "g"}, clear=False):
            out = pulse_server.link_pr("X", GH_URL)

        self.assertIn("read-only mirror", out)
        self.assertEqual(patches, [])



    def test_origin_link_supersedes_github_mirror(self):
        """Relinking Origin supersedes the retired GitHub mirror of the same
        owner/repo#N: the mirror's status is PRESERVED as immutable audit
        history (PLAT-1296 — not rewritten to closed), and only ``superseded_by``
        is annotated to point at the Origin canonical."""
        patches = []
        def api_request(method, path, data=None):
            if method == "GET":
                return {
                    "metadata": {
                        "pull_requests": [
                            {
                                "url": "https://github.com/shizuha-labs/scs/pull/12",
                                "status": "open",
                                "head_sha": "oldmirror",
                            }
                        ]
                    }
                }
            if method == "PATCH":
                patches.append(data)
                return {"ok": True}
            return {}
        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server, "requests") as req, \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "ci-test-token"}, clear=False):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {
                "head": {"sha": "425efeeb045dda21729c47ff4d1c3a6d962d33d6"},
                "state": "open",
                "merged": False,
                "title": "scs",
            }
            req.get.return_value = resp
            out = pulse_server.link_pr(
                "PLS-496",
                "https://origin.shizuha.com/shizuha-labs/scs/pulls/12",
            )
        self.assertTrue(patches, out)
        prs = patches[0]["metadata"]["pull_requests"]
        gh = [p for p in prs if "github.com" in p["url"]][0]
        self.assertEqual(gh["status"], "open")  # PLAT-1296: status preserved, not rewritten
        self.assertEqual(
            gh.get("superseded_by"),
            "https://origin.shizuha.com/shizuha-labs/scs/pulls/12",
        )

    def test_origin_link_does_not_supersede_mirror_without_provider_head(self):
        """Provider success without immutable head provenance fails closed."""
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return {"metadata": {"pull_requests": [{
                    "url": "https://github.com/shizuha-labs/scs/pull/12",
                    "status": "open", "head_sha": "frozen",
                }]}}
            if method == "PATCH":
                patches.append(data)
                return {"ok": True}
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", return_value=_Resp(True, {
                 "state": "open", "merged": False, "head": {"sha": ""},
             })), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "ci-test-token"}, clear=False):
            pulse_server.link_pr(
                "PLS-496", "https://origin.shizuha.com/shizuha-labs/scs/pulls/12"
            )

        gh = [p for p in patches[0]["metadata"]["pull_requests"] if "github.com" in p["url"]][0]
        self.assertNotIn("superseded_by", gh)

    def test_origin_short_reference_writes_canonical_supersession_url(self):
        """The accepted short link key never leaks into trusted row metadata."""
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return {"metadata": {"pull_requests": [{
                    "url": "https://github.com/shizuha-labs/scs/pull/12",
                    "status": "open", "head_sha": "frozen",
                }]}}
            if method == "PATCH":
                patches.append(data)
                return {"ok": True}
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", return_value=_Resp(True, {
                 "state": "open", "merged": False, "head": {"sha": "origin-head"},
             })), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "ci-test-token"}, clear=False):
            pulse_server.link_pr("PLS-496", "shizuha-labs/scs#12")

        gh = [p for p in patches[0]["metadata"]["pull_requests"] if "github.com" in p["url"]][0]
        self.assertEqual(
            gh.get("superseded_by"),
            "https://origin.shizuha.com/shizuha-labs/scs/pulls/12",
        )

    def test_failed_metadata_patch_leaves_mirror_undispositioned(self):
        """A provider-confirmed mapping is not durable when the PATCH rejects."""
        durable = {"metadata": {"pull_requests": [{
            "url": "https://github.com/shizuha-labs/scs/pull/12",
            "status": "open", "head_sha": "frozen",
        }]}}

        def api_request(method, path, data=None):
            if method == "GET":
                return copy.deepcopy(durable)
            if method == "PATCH":
                return {"error": "conflict: metadata write rejected"}
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get", return_value=_Resp(True, {
                 "state": "open", "merged": False, "head": {"sha": "origin-head"},
             })), \
             mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "ci-test-token"}, clear=False):
            out = pulse_server.link_pr(
                "PLS-496", "https://origin.shizuha.com/shizuha-labs/scs/pulls/12"
            )

        self.assertIn("Error", out)
        self.assertNotIn("superseded_by", durable["metadata"]["pull_requests"][0])

    def test_existing_link_refresh_surfaces_patch_error(self):
        """revi prior-head P1 #3: re-linking an existing PR whose metadata PATCH
        fails must return an error, NOT a false 'Refreshed'. The Origin head
        advanced (oldsha -> newsha) so a PATCH is attempted; it errors and
        link_pr must surface that rather than claim success."""
        def api_request(method, path, data=None):
            if method == "GET":
                return {"metadata": {"pull_requests": [
                    {
                        "url": "https://origin.shizuha.com/shizuha-labs/scs/pulls/12",
                        "status": "open",
                        "head_sha": "oldsha",
                    }
                ]}}
            if method == "PATCH":
                return {"error": "conflict: metadata write rejected"}
            return {}
        with mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "ci-test-token"}), \
             mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server, "requests") as req:
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {
                "head": {"sha": "newsha"}, "state": "open", "merged": False, "title": "scs",
            }
            req.get.return_value = resp
            out = pulse_server.link_pr(
                "PLS-496",
                "https://origin.shizuha.com/shizuha-labs/scs/pulls/12",
            )
        self.assertIn("Error refreshing", out)
        self.assertNotIn("Refreshed PR", out)

    def test_frozen_origin_shizuha_pr_is_rejected(self):
        item = {"id": 1, "item_key": "SCLI-586", "metadata": {"pull_requests": []}}
        patches = []

        def api_request(method, path, data=None):
            if method == "GET":
                return item
            if method == "PATCH":
                patches.append(data)
                return item
            raise AssertionError(method)

        with mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server.requests, "get") as get:
            out = pulse_server.link_pr(
                "SCLI-586",
                "https://origin.shizuha.com/shizuha-labs/shizuha/pulls/28",
            )
            get.assert_not_called()

        self.assertIn("ERROR:", out)
        self.assertIn("shizuha-beta", out)
        self.assertEqual(patches, [])


if __name__ == "__main__":
    unittest.main()
