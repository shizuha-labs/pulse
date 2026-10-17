"""Regression coverage for operator-request hygiene AND team-slug canonicalization
in pulse_create_blocker_task.

PLS-539: `ceo-decisions` is a real, org-scoped CEO Decisions team and must NEVER
be aliased to the physical-only `human-ops` lane — the canonicalizer must keep the
two slugs distinct so an operator DECISION routes to the decision team rather than
being rewritten into a physical-operator queue."""
import os
import unittest
from unittest import mock

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")

import pulse_server  # noqa: E402


class OperatorBlockerHygieneTests(unittest.TestCase):
    def test_placeholder_path_is_rejected_before_api_write(self):
        with mock.patch.object(pulse_server, "api_request") as api:
            out = pulse_server.create_blocker_task(
                parent_id="SCLI-327",
                team_slug="human-ops",
                title="Perform the macOS smoke",
                description="Run /tmp/build/.../Shizuha.app and capture a screenshot.",
            )

        self.assertIn("exact executable path", out)
        self.assertIn("No blocker task was created", out)
        api.assert_not_called()

    def test_ceo_decisions_canonicalizes_to_itself_not_human_ops(self):
        # PLS-539 (P2, revi #313): the retired PLAT-724 alias
        # `ceo-decisions -> human-ops` must stay dead. The canonicalizer keeps the
        # decision team distinct from the physical-only operator lane, so an
        # operator DECISION reaches the CEO Decisions team, not human-ops.
        self.assertEqual(pulse_server._canonical_team_slug("ceo-decisions"), "ceo-decisions")
        self.assertEqual(pulse_server._canonical_team_slug("ceo_decisions"), "ceo-decisions")
        self.assertEqual(pulse_server._canonical_team_slug("human-ops"), "human-ops")
        self.assertNotEqual(
            pulse_server._canonical_team_slug("ceo-decisions"),
            pulse_server._canonical_team_slug("human-ops"),
        )
        # The legacy human-ops aliases still fold (leads/triage), proving the
        # assertion above is a real distinction and not a no-op canonicalizer.
        self.assertEqual(pulse_server._canonical_team_slug("leads"), "human-ops")

    def test_unprepared_operator_decision_is_rejected_before_api_write(self):
        with mock.patch.object(pulse_server, "api_request") as api:
            out = pulse_server.create_blocker_task(
                parent_id="TRD-1",
                team_slug="ceo-decisions",
                title="Decide intraday data access path",
                description="## Options\n- AngelOne\n- Fyers\n- Dhan",
            )

        self.assertIn("specific recommendation", out)
        self.assertIn("No blocker task was created", out)
        api.assert_not_called()

    def test_prepared_operator_decision_passes_hygiene_gate(self):
        self.assertIsNone(
            pulse_server._operator_blocker_hygiene_error(
                "human-ops",
                "Decide intraday data access path",
                "## Recommendation\nUse provider A because coverage and cost are verified.",
            )
        )

    def test_non_operator_team_blocker_is_not_subject_to_decision_gate(self):
        self.assertIsNone(
            pulse_server._operator_blocker_hygiene_error(
                "engineering",
                "Decide implementation approach",
                "Engineering owns this reversible choice.",
            )
        )


if __name__ == "__main__":
    unittest.main()
