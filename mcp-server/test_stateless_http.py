"""
Regression tests for PLAT-612: SHIZUHA_MCP_STATELESS_HTTP feature flag.

Verifies that:
  1. _is_stateless_http() correctly reads the env var and CLI flag.
  2. run_mcp_server() passes stateless_http=True/False to mcp.run()
     depending on the env var.
  3. The flag is off by default (no stateless_http kwarg or False).
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import base as mcp_base  # noqa: E402  (the shared MCP base module)


# ---------------------------------------------------------------------------
# _is_stateless_http helper
# ---------------------------------------------------------------------------

class StatelessHttpEnvTests(unittest.TestCase):
    """_is_stateless_http() reads SHIZUHA_MCP_STATELESS_HTTP."""

    def _call(self, env_val=None, extra_argv=None):
        env_patch = {}
        if env_val is not None:
            env_patch["SHIZUHA_MCP_STATELESS_HTTP"] = env_val
        else:
            env_patch.pop("SHIZUHA_MCP_STATELESS_HTTP", None)

        argv = list(sys.argv)
        if extra_argv:
            argv.extend(extra_argv)

        with mock.patch.dict(os.environ, env_patch, clear=False), \
             mock.patch("sys.argv", argv):
            # Clear env key when we want it absent
            if env_val is None:
                os.environ.pop("SHIZUHA_MCP_STATELESS_HTTP", None)
            return mcp_base._is_stateless_http()

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHIZUHA_MCP_STATELESS_HTTP", None)
            with mock.patch("sys.argv", ["test"]):
                self.assertFalse(mcp_base._is_stateless_http())

    def test_env_true(self):
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "true"}):
            self.assertTrue(mcp_base._is_stateless_http())

    def test_env_1(self):
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "1"}):
            self.assertTrue(mcp_base._is_stateless_http())

    def test_env_yes(self):
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "yes"}):
            self.assertTrue(mcp_base._is_stateless_http())

    def test_env_false(self):
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "false"}):
            self.assertFalse(mcp_base._is_stateless_http())

    def test_env_false_overrides_cli_flag(self):
        """SHIZUHA_MCP_STATELESS_HTTP=false must suppress --stateless-http CLI flag."""
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "false"}), \
             mock.patch("sys.argv", ["test", "--stateless-http"]):
            self.assertFalse(mcp_base._is_stateless_http())

    def test_env_0_overrides_cli_flag(self):
        """SHIZUHA_MCP_STATELESS_HTTP=0 must suppress --stateless-http CLI flag."""
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "0"}), \
             mock.patch("sys.argv", ["test", "--stateless-http"]):
            self.assertFalse(mcp_base._is_stateless_http())

    def test_env_no_overrides_cli_flag(self):
        """SHIZUHA_MCP_STATELESS_HTTP=no must suppress --stateless-http CLI flag."""
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": "no"}), \
             mock.patch("sys.argv", ["test", "--stateless-http"]):
            self.assertFalse(mcp_base._is_stateless_http())

    def test_env_empty(self):
        with mock.patch.dict(os.environ, {"SHIZUHA_MCP_STATELESS_HTTP": ""}):
            self.assertFalse(mcp_base._is_stateless_http())

    def test_cli_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHIZUHA_MCP_STATELESS_HTTP", None)
            with mock.patch("sys.argv", ["test", "--http-port", "18101", "--stateless-http"]):
                self.assertTrue(mcp_base._is_stateless_http())

    def test_cli_flag_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHIZUHA_MCP_STATELESS_HTTP", None)
            with mock.patch("sys.argv", ["test", "--http-port", "18101"]):
                self.assertFalse(mcp_base._is_stateless_http())


# ---------------------------------------------------------------------------
# run_mcp_server passes stateless_http to mcp.run()
# ---------------------------------------------------------------------------

class RunMcpServerStatelessTests(unittest.TestCase):
    """run_mcp_server() must forward the stateless flag to mcp.run()."""

    def _fake_base(self):
        b = mock.MagicMock()
        b.service_name = "pulse"
        b.api_url = "http://localhost:8002/api"
        return b

    def _run(self, env_val=None, argv_extra=None):
        """Call run_mcp_server() with --http-port and capture mcp.run() kwargs."""
        argv = ["test", "--http-port", "18101"] + (argv_extra or [])
        env_patch = {}
        if env_val is not None:
            env_patch["SHIZUHA_MCP_STATELESS_HTTP"] = env_val

        mcp_mock = mock.MagicMock()
        mcp_mock.settings = mock.MagicMock()

        with mock.patch.dict(os.environ, env_patch, clear=False), \
             mock.patch("sys.argv", argv), \
             mock.patch.object(mcp_base, "setup_oauth_routes"):
            if env_val is None:
                os.environ.pop("SHIZUHA_MCP_STATELESS_HTTP", None)
            mcp_base.run_mcp_server(mcp_mock, self._fake_base())

        return mcp_mock.run.call_args

    def test_stateless_false_by_default(self):
        call_args = self._run()
        kwargs = call_args.kwargs if call_args.kwargs else {}
        args = call_args.args if call_args.args else ()
        # stateless_http should be False or absent
        stateless = kwargs.get("stateless_http", False)
        self.assertFalse(stateless, "stateless_http must be False by default")

    def test_stateless_true_via_env(self):
        call_args = self._run(env_val="true")
        kwargs = call_args.kwargs if call_args.kwargs else {}
        self.assertTrue(kwargs.get("stateless_http"), "stateless_http must be True when env=true")

    def test_stateless_true_via_cli(self):
        call_args = self._run(argv_extra=["--stateless-http"])
        kwargs = call_args.kwargs if call_args.kwargs else {}
        self.assertTrue(kwargs.get("stateless_http"), "stateless_http must be True via --stateless-http")

    def test_transport_is_streamable_http(self):
        call_args = self._run()
        # Ensure transport arg is still set correctly regardless of stateless flag
        kwargs = call_args.kwargs if call_args.kwargs else {}
        args = call_args.args if call_args.args else ()
        transport = kwargs.get("transport") or (args[0] if args else None)
        self.assertEqual(transport, "streamable-http")


if __name__ == "__main__":
    unittest.main()
