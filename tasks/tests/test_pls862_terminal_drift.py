"""PLS-862 / ADR-0073 drift guard.

ADR-0073 requires that the MCP's TERMINAL_STATUSES is an explicit allow-list that
MIRRORS the backend source of truth exactly (tasks/services/actionable_items.py
:: TERMINAL_STATUS_SLUGS) and never maintains a superset. Before this fix the MCP
carried 'scheduled' — the sole drift — which silently reclassified parked
recurring work as terminal and hid it from every count on the unfiltered
assignee query.

These tests import the LIVE sets from both sides (not literals) so a future
drift re-fails instead of silently passing.
"""
import os
import sys

from django.test import SimpleTestCase

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_MCP_DIR = os.path.join(_REPO_ROOT, 'mcp-server')
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

os.environ.setdefault('SHIZUHA_PULSE_URL', 'http://localhost:8002/pulse/api')

import pulse_server  # noqa: E402
from tasks.services.actionable_items import TERMINAL_STATUS_SLUGS  # noqa: E402


class MCPBackendTerminalSetDriftGuard(SimpleTestCase):
    def test_mcp_terminal_set_matches_backend_exactly(self):
        self.assertEqual(
            set(pulse_server.TERMINAL_STATUSES),
            set(TERMINAL_STATUS_SLUGS),
            "MCP TERMINAL_STATUSES must mirror backend TERMINAL_STATUS_SLUGS "
            "(ADR-0073); the MCP must never maintain a superset (PLS-862 removed "
            "'scheduled' from the MCP set — if this fails, a drift has re-entered).",
        )

    def test_scheduled_is_not_terminal_on_either_side(self):
        self.assertNotIn('scheduled', TERMINAL_STATUS_SLUGS)
        self.assertNotIn('scheduled', pulse_server.TERMINAL_STATUSES)

    def test_scheduled_has_non_terminal_waiting_home(self):
        """scheduled must be classifiable as parked/waiting, not vanished."""
        self.assertIn('scheduled', pulse_server.PARKED_WAITING_SLUGS)
