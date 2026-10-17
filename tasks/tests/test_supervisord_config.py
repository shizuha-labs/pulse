import configparser
from pathlib import Path
import re

from django.test import SimpleTestCase


SUPERVISOR_CONFIG = Path(__file__).parents[2] / "supervisord.conf"


def _config():
    parser = configparser.RawConfigParser()
    parser.read(SUPERVISOR_CONFIG)
    return parser


class SupervisorConfigTests(SimpleTestCase):
    def test_gunicorn_access_log_tokens_are_escaped_for_supervisor(self):
        command = next(
            line.removeprefix("command=")
            for line in SUPERVISOR_CONFIG.read_text().splitlines()
            if line.startswith("command=") and "taskmanager.wsgi:application" in line
        )

        self.assertIn("--access-logformat", command)
        self.assertIn("duration_us=%%(D)s", command)
        self.assertIn("pid=%%(p)s", command)
        self.assertIsNone(
            re.search(r"(?<!%)%\([^)]+\)", command),
            "Supervisor treats unescaped %(name) tokens as its own interpolation",
        )

    def test_serving_processes_have_a_bounded_graceful_drain_window(self):
        """PLAT-4124: rollouts must not SIGKILL in-flight REST/MCP calls."""
        config = _config()

        self.assertGreaterEqual(
            config.getint("program:django", "stopwaitsecs"), 30
        )
        self.assertGreaterEqual(
            config.getint("program:mcp-server", "stopwaitsecs"), 30
        )
