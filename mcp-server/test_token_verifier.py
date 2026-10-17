import asyncio
import json
import os
import secrets
import sys
import time
import unittest
from unittest import mock

import jwt

sys.path.insert(0, os.path.dirname(__file__))
from base import (  # noqa: E402
    ShizuhaTokenVerifier,
    _oauth_issuer,
    _register_oauth_discovery_routes,
    _resource_server_url,
)


_TEST_HS256_SIGNING_KEY = secrets.token_urlsafe(32)
_FAKE_RS256_SIGNING_KEY = secrets.token_urlsafe(32)


class _FakeSigningKey:
    key = _FAKE_RS256_SIGNING_KEY


class _FakeJwksClient:
    urls = []

    def __init__(self, url):
        self.url = url
        self.urls.append(url)

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


class OAuthUrlDefaultsTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_defaults_use_canonical_https_without_redirects(self):
        self.assertEqual(_oauth_issuer(), "https://shizuha.com/id")
        self.assertEqual(
            _resource_server_url("pulse"),
            "https://shizuha.com/mcp/pulse",
        )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_discovery_advertises_canonical_authorization_and_resource(self):
        routes = {}

        class FakeMcp:
            def custom_route(self, path, methods):
                def register(handler):
                    routes[path] = handler
                    return handler

                return register

        from starlette.requests import Request

        _register_oauth_discovery_routes(FakeMcp(), "pulse")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "path": "/.well-known/oauth-protected-resource",
                "raw_path": b"/.well-known/oauth-protected-resource",
                "query_string": b"",
                "headers": [
                    (b"host", b"shizuha.com"),
                    (b"x-forwarded-prefix", b"/mcp/pulse"),
                ],
                "server": ("shizuha.com", 443),
            }
        )
        response = asyncio.run(
            routes["/.well-known/oauth-protected-resource"](request)
        )
        metadata = json.loads(response.body)
        self.assertEqual(metadata["resource"], "https://shizuha.com/mcp/pulse")
        self.assertEqual(
            metadata["authorization_servers"],
            ["https://shizuha.com/id"],
        )


class ShizuhaTokenVerifierTests(unittest.TestCase):
    def setUp(self):
        _FakeJwksClient.urls.clear()

    def test_hs256_shared_secret_token_is_rejected(self):
        token = jwt.encode(
            {"user_id": 7, "exp": int(time.time()) + 3600},
            _TEST_HS256_SIGNING_KEY,
            algorithm="HS256",
        )
        verifier = ShizuhaTokenVerifier("test", "https://id.example")

        result = asyncio.run(verifier.verify_token(token))

        self.assertIsNone(result)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_rs256_jwks_token_authenticates(self):
        token = "header.payload.signature"
        verifier = ShizuhaTokenVerifier("test", _oauth_issuer())
        with mock.patch("jwt.PyJWKClient", _FakeJwksClient), mock.patch(
            "base.jwt.decode",
            return_value={"user_id": 7, "exp": int(time.time()) + 3600},
        ) as decode:
            result = asyncio.run(verifier.verify_token(token))

        self.assertIsNotNone(result)
        self.assertEqual(result.client_id, "7")
        decode.assert_called_once()
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])
        self.assertEqual(
            _FakeJwksClient.urls,
            ["https://shizuha.com/id/.well-known/jwks.json"],
        )

    def test_expired_rs256_token_is_rejected_for_refresh(self):
        token = "header.payload.signature"
        verifier = ShizuhaTokenVerifier("test", "https://id.example")
        with mock.patch("jwt.PyJWKClient", _FakeJwksClient), mock.patch(
            "base.jwt.decode",
            side_effect=jwt.ExpiredSignatureError("expired"),
        ):
            result = asyncio.run(verifier.verify_token(token))

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
