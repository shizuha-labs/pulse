"""
Unit tests for tasks.token_revocation (PLAT-1032 / SCLI-376).

Test vectors:
  1. No client_id claim (agent/session/broker JWT) → revocation IS consulted
  2. Falsy client_id (empty string) → revocation IS consulted (no skip)
  3. Revoked token (active=False) → rejected
  4. Active token (active=True) → allowed
  5. Network error → fail closed (deny)
  6. HTTP non-200 → fail closed (deny)
  7. Non-JSON response → fail closed (deny)
  8. Cache TTL: second call within TTL skips the network
  9. Cache expired: fresh call after TTL triggers a new lookup

SCLI-376 (hiro architecture determination, ID-1 §8): the revocation consult is
applied to EVERY validated token — agent/session/broker JWTs (no client_id)
are tracked in the id revocation store and must be rejected once revoked. The
pre-SCLI-376 skip for missing client_id is what let a revoked broker JWT stay
200 in Hive/Pulse while ID 401'd it (split-brain runtime).
"""
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase

from tasks.token_revocation import (
    _CACHE_TTL_SECONDS,
    _REVOCATION_CACHE,
    _cache_key,
    is_token_revoked,
)


class TokenRevocationTests(TestCase):
    def setUp(self):
        # Isolate each test: flush the module-level cache.
        _REVOCATION_CACHE.clear()

    # ------------------------------------------------------------------ #
    # 1–2. Non-OAuth tokens (no / empty client_id) — revocation consulted #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_no_client_id_consults_revocation(self, mock_post):
        """Tokens without client_id are still revocation-checked (SCLI-376)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        self.assertFalse(is_token_revoked({}, "raw.token.value"))
        mock_post.assert_called_once()

    @patch("tasks.token_revocation.requests.post")
    def test_no_client_id_revoked_is_rejected(self, mock_post):
        """A revoked agent/broker JWT (no client_id) must be denied — SCLI-376."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": False}
        mock_post.return_value = mock_resp

        self.assertTrue(is_token_revoked({}, "revoked.broker.jwt"))
        mock_post.assert_called_once()

    @patch("tasks.token_revocation.requests.post")
    def test_empty_client_id_consults_revocation(self, mock_post):
        """Falsy client_id (empty string) is NOT skipped — SCLI-376."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        self.assertFalse(is_token_revoked({"client_id": ""}, "raw.token.value"))
        mock_post.assert_called_once()

    @patch("tasks.token_revocation.requests.post")
    def test_bytes_raw_token_from_auth_flow_works(self, mock_post):
        """SimpleJWT hands `get_validated_token` bytes — must not 500.

        Regression for the premerge failure: the real DRF auth flow passes the
        raw Authorization token as `bytes`, and the new SCLI-376 revocation
        consult hit `'bytes' object has no attribute 'encode'` in `_cache_key`,
        turning every authenticated request into a 500 (marker-writing mutation
        returned 500 not in (200, 201)). The str-only unit tests could not see
        this because they called `is_token_revoked` with a `str`.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        # bytes raw token, both the cache-key hash and the JSON body must work.
        raw = b"eyJhbGciOiJIUzI1NiJ9.some.payload.preimage"
        self.assertFalse(is_token_revoked({}, raw))
        mock_post.assert_called_once()
        # The request body must contain a decodable str token, not raw bytes.
        called_kwargs = mock_post.call_args.kwargs
        self.assertIsInstance(called_kwargs["json"]["token"], str)
        self.assertEqual(called_kwargs["json"]["token"], "eyJhbGciOiJIUzI1NiJ9.some.payload.preimage")
        # And the cache key must be stable across str/bytes representations of
        # the SAME token (identical SHA-256 after normalization).
        self.assertEqual(
            _cache_key("abc.def.ghi"), _cache_key(b"abc.def.ghi")
        )

    # ------------------------------------------------------------------ #
    # 3. Revoked token → rejected                                          #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_revoked_token_returns_true(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": False}
        mock_post.return_value = mock_resp

        self.assertTrue(is_token_revoked({"client_id": "app"}, "revoked.jwt"))

    # ------------------------------------------------------------------ #
    # 4. Active (non-revoked) token → allowed                              #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_active_token_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        self.assertFalse(is_token_revoked({"client_id": "app"}, "active.jwt"))

    # ------------------------------------------------------------------ #
    # 5–7. Lookup failures → fail closed (deny)                            #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post", side_effect=OSError("connection refused"))
    def test_network_error_fails_closed(self, _mock):
        result = is_token_revoked({"client_id": "app"}, "some.jwt")
        self.assertTrue(result, "Network error must FAIL CLOSED (deny access)")

    @patch("tasks.token_revocation.requests.post")
    def test_non_200_response_fails_closed(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_post.return_value = mock_resp

        result = is_token_revoked({"client_id": "app"}, "some.jwt")
        self.assertTrue(result, "HTTP 503 must FAIL CLOSED (deny access)")

    @patch("tasks.token_revocation.requests.post")
    def test_bad_json_response_fails_closed(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("bad json")
        mock_post.return_value = mock_resp

        result = is_token_revoked({"client_id": "app"}, "some.jwt")
        self.assertTrue(result, "Non-JSON response must FAIL CLOSED (deny access)")

    # ------------------------------------------------------------------ #
    # 8. Cache hit within TTL — second call skips the network              #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_cache_hit_prevents_duplicate_lookup(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        token = "cached.jwt"
        is_token_revoked({"client_id": "app"}, token)
        is_token_revoked({"client_id": "app"}, token)

        mock_post.assert_called_once()

    # ------------------------------------------------------------------ #
    # 9. Expired cache entry → fresh network call                          #
    # ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_expired_cache_triggers_new_lookup(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        token = "expiry.jwt"
        key = _cache_key(token)

        # Plant a stale cache entry that expired one second ago.
        _REVOCATION_CACHE[key] = (time.monotonic() - _CACHE_TTL_SECONDS - 1, False)

        is_token_revoked({"client_id": "app"}, token)

        mock_post.assert_called_once()


# ------------------------------------------------------------------ #
# 10. PLAT-5996 — stale token_version rejection (revive-by-replay)    #
# ------------------------------------------------------------------ #

    @patch("tasks.token_revocation.requests.post")
    def test_stale_token_version_is_rejected(self, mock_post):
        """A replayed pre-revocation token (token_version < current) is denied
        even when the OAuth-store row is gone — PLAT-5996."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True, "token_version": 3}
        mock_post.return_value = mock_resp

        self.assertTrue(
            is_token_revoked({"token_version": 1}, "stale.jwt")
        )

    @patch("tasks.token_revocation.requests.post")
    def test_current_token_version_is_allowed(self, mock_post):
        """A token carrying the current version is allowed — PLAT-5996."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True, "token_version": 3}
        mock_post.return_value = mock_resp

        self.assertFalse(
            is_token_revoked({"token_version": 3}, "current.jwt")
        )

    @patch("tasks.token_revocation.requests.post")
    def test_no_token_version_claim_falls_back_to_active_check(self, mock_post):
        """Coordinated rollout: a token WITHOUT the token_version claim is
        evaluated by the active check alone (no version comparison) — PLAT-5996."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True, "token_version": 3}
        mock_post.return_value = mock_resp

        self.assertFalse(
            is_token_revoked({}, "no-version.jwt")
        )

    @patch("tasks.token_revocation.requests.post")
    def test_response_without_token_version_skips_version_check(self, mock_post):
        """Rollback path: a response WITHOUT token_version (old ID) skips the
        version comparison and relies on the active check — PLAT-5996."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": True}
        mock_post.return_value = mock_resp

        self.assertFalse(
            is_token_revoked({"token_version": 1}, "old-id.jwt")
        )

    @patch("tasks.token_revocation.requests.post")
    def test_stale_version_with_revoked_active_is_still_rejected(self, mock_post):
        """A token that is BOTH stale-version AND revoked-active is denied —
        PLAT-5996 (the active check already rejects it)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": False, "token_version": 3}
        mock_post.return_value = mock_resp

        self.assertTrue(
            is_token_revoked({"token_version": 1}, "stale-revoked.jwt")
        )
