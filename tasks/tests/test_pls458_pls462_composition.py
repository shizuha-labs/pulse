"""PLS-458 × PLS-462 composition regression (aoi ruling, 2026-07-14).

The re-entry gate must keep a SINGLE deployed authority for immutable-provenance
vs active-review obligations — while PLS-458's `SecurityGateState` +
`_validate_security_gate_state_atomic` commit-time revalidation composes
alongside it. f827687's redundant `architect_arbitration` /
`_validate_architect_arbitration` / `superseded_urls` path was dropped so two
schemas can't disagree and open a bypass.

PLS-600 update (0c2b3c0e, 2026-07-25): the standalone PLS-462
`_review_reentry_architect_superseded_keys` schema was retired from main and
its obligation folded into the inline v6 `review-reentry-active-tuple-v1`
evaluation. The single-authority invariant now requires that retired schema to
STAY retired — a resurrected parallel superseded-key path would re-open the
two-schemas-disagree bypass the aoi ruling closed.
"""
from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class Pls458Pls462CompositionTests(SimpleTestCase):
    def test_pls462_superseded_key_schema_retired(self):
        # PLS-600 (0c2b3c0e) folded superseded-key filtering into the inline v6
        # active-tuple evaluation. The standalone schema must remain gone.
        self.assertFalse(
            hasattr(WorkflowService, "_review_reentry_architect_superseded_keys"),
            "the retired PLS-462 superseded-key schema must stay retired "
            "(PLS-600 folded it into the inline v6 active-tuple evaluation; "
            "a parallel schema would violate the single-authority ruling)",
        )

    def test_pls458_atomic_security_gate_present(self):
        self.assertTrue(
            hasattr(WorkflowService, "_validate_security_gate_state_atomic"),
            "PLS-458 commit-time SecurityGateState revalidation must compose alongside PLS-462",
        )

    def test_redundant_arbitration_path_removed(self):
        # The single-authority invariant: f827687's parallel arbitration schema
        # must be gone (dead method + superseded_urls guard removed).
        self.assertFalse(
            hasattr(WorkflowService, "_validate_architect_arbitration"),
            "the redundant architect_arbitration path must be dropped (aoi ruling)",
        )
