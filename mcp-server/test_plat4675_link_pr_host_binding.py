"""PLAT-4675 P1 (kai review, PR #8): `pulse_link_pr` must hard-bind the
authenticated Forgejo head_sha fetch to HTTPS ``origin.shizuha.com``.

The fetch carries ``FORGEJO_TOKEN`` in an Authorization header, so it must NEVER
be sent to an arbitrary/attacker-supplied host, and a non-HTTPS Origin URL must
not trigger it either. These pin that the token can only ever reach the exact
canonical Origin host over HTTPS.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


def _empty_item(*a, **k):
    # GET returns an item with no linked PRs; PATCH/POST return a benign dict.
    return {"item_key": "X-1", "metadata": {"pull_requests": []}}


class LinkPrForgejoHostBindingTests(unittest.TestCase):
    def _run(self, url):
        """Call link_pr with FORGEJO_TOKEN set; return the mocked requests.get."""
        with mock.patch.dict(os.environ, {"FORGEJO_TOKEN": "secret-forgejo-token"}), \
                mock.patch.object(pulse_server, "api_request", side_effect=_empty_item), \
                mock.patch.object(pulse_server.requests, "get") as mock_get:
            mock_get.return_value = mock.Mock(ok=True, json=lambda: {"head": {"sha": "abc123"}})
            pulse_server.link_pr("TASK-1", url)
            return mock_get

    def test_token_not_sent_to_arbitrary_host(self):
        # A look-alike path on a hostile host must NOT trigger an authenticated fetch.
        mock_get = self._run("https://evil.example.com/owner/repo/pulls/1")
        self.assertFalse(mock_get.called, "FORGEJO_TOKEN must not be fetched from a non-Origin host")

    def test_http_origin_rejected(self):
        # Non-HTTPS Origin URL must not carry the token (downgrade protection).
        mock_get = self._run("http://origin.shizuha.com/owner/repo/pulls/1")
        self.assertFalse(mock_get.called, "non-HTTPS Origin URL must not trigger an authenticated fetch")

    def test_subdomain_lookalike_rejected(self):
        # origin.shizuha.com.evil.com must not match the exact-host pattern.
        mock_get = self._run("https://origin.shizuha.com.evil.com/o/r/pulls/1")
        self.assertFalse(mock_get.called, "look-alike subdomain must not trigger an authenticated fetch")

    def test_query_and_fragment_smuggling_rejected(self):
        for url in (
            "https://origin.shizuha.com/o/r/pulls/1?redirect=evil",
            "https://origin.shizuha.com/o/r/pulls/1#evil",
        ):
            with self.subTest(url=url):
                self.assertFalse(self._run(url).called)

    def test_canonical_origin_https_fetches_with_token(self):
        mock_get = self._run("https://origin.shizuha.com/shizuha-labs/books/pulls/7")
        self.assertTrue(mock_get.called)
        call = mock_get.call_args
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        self.assertTrue(
            url.startswith("https://origin.shizuha.com/api/v1/repos/shizuha-labs/books/pulls/7"),
            f"fetch must target the canonical Origin API host, got {url!r}",
        )
        self.assertEqual(call.kwargs["headers"]["Authorization"], "token secret-forgejo-token")


if __name__ == "__main__":
    unittest.main()
