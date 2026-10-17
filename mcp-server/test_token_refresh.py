import os
import secrets
import sys
import time
import unittest
from unittest import mock

import jwt

sys.path.insert(0, os.path.dirname(__file__))
import pulse_server  # noqa: E402


_TEST_JWT_SIGNING_KEY = secrets.token_urlsafe(32)


def _token(seconds):
    return jwt.encode({"exp": int(time.time()) + seconds, "email": "kai@shizuha.com"}, _TEST_JWT_SIGNING_KEY, algorithm="HS256")


class TokenRefreshTests(unittest.TestCase):
    def setUp(self):
        pulse_server._REFRESHED_AGENT_TOKEN = ""
        pulse_server._REFRESHED_AGENT_TOKEN_EXP = 0

    def test_agent_token_refreshes_inside_window(self):
        old = _token(5)
        new = _token(3600)
        with mock.patch.object(pulse_server, "_get_request_headers", return_value={"authorization": f"Bearer {old}", "x-shizuha-agent-refresh-token": "refresh-1"}), \
             mock.patch.object(pulse_server, "_refresh_agent_token", return_value=new) as refresh:
            self.assertEqual(pulse_server._get_effective_agent_token(), new)
            refresh.assert_called_once_with("refresh-1")

    def test_agent_token_not_refreshed_outside_window(self):
        current = _token(3600)
        with mock.patch.object(pulse_server, "_get_request_headers", return_value={"authorization": f"Bearer {current}", "x-shizuha-agent-refresh-token": "refresh-1"}), \
             mock.patch.object(pulse_server, "_refresh_agent_token", return_value=_token(7200)) as refresh:
            self.assertEqual(pulse_server._get_effective_agent_token(), current)
            refresh.assert_not_called()

    def test_missing_agent_token_fails_closed_without_service_mint(self):
        with mock.patch.object(pulse_server, "_get_request_headers", return_value={}), \
             mock.patch.object(pulse_server, "PULSE_JWT_TOKEN", ""):
            self.assertEqual(pulse_server._get_effective_agent_token(), "")
            with self.assertRaisesRegex(ValueError, "Authorization header"):
                pulse_server.get_headers()


if __name__ == "__main__":
    unittest.main()
