"""PLAT-9838 — lifetime head-mutation fence tests (pair for PLAT-9837).

Covers: mint-time citation equality (idempotent re-cite), force-push
fencing with typed `mint_fenced_stale` + audit line, settlement denies
(fenced / uncited), the re-anchor case (rescope deferred to the ADR-0090
owner — fence records, never auto-rescopes), and successor-PR scoping
(no cross-PR fencing).

PLAT-13400: written in plain unittest idiom — the CI backend suite runs
under Django's runner in a venv without pytest, so a module-level
``import pytest`` made the whole module an ImportError error (run 10536,
5465 green / 1 error). Keep this file pytest-free.
"""

from __future__ import annotations

import logging

from unittest import TestCase

from tasks.services import head_mutation_fence as fence


class HeadMutationFenceTests(TestCase):
    def setUp(self):
        fence._MINTS.clear()

    def tearDown(self):
        fence._MINTS.clear()

    def test_register_cites_live_head_and_is_idempotent(self):
        """Citation is literal (repo/pr/head) and exact re-cites are no-ops."""
        first = fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
        )
        self.assertEqual(first, {"repo": "unit/unit", "pr": 7, "head": "70b4531a",
                                 "fenced": False, "fence": None})
        # exact re-cite: same record, no duplicate
        again = fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
        )
        self.assertIs(again, first)
        # partial / heuristic pins deny
        with self.assertRaises(ValueError):
            fence.register_mint_reference(mint_id=1, repo="", pr=7, head="x")
        with self.assertRaises(ValueError):
            fence.register_mint_reference(mint_id=1, repo="u/u", pr=0, head="x")
        with self.assertRaises(ValueError):
            fence.register_mint_reference(mint_id=1, repo="u/u", pr=7, head="")

    def test_force_push_fences_outstanding_mints_typed_and_loud(self):
        """force_push fences the mint citing the old head: typed + audit line."""
        fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
        )
        with self.assertLogs('tasks.services.head_mutation_fence',
                             level=logging.WARNING) as captured:
            fenced = fence.fence_mints_on_head_mutation(
                repo="unit/unit", pr=7, new_head="8b09617", event="force_push",
            )
        self.assertEqual(len(fenced), 1)
        fence_rec = fenced[0]["fence"]
        self.assertEqual(fence_rec["code"], "mint_fenced_stale")
        self.assertEqual(fence_rec["event"], "force_push")
        self.assertEqual(fence_rec["old_head"], "70b4531a")
        self.assertEqual(fence_rec["new_head"], "8b09617")
        # the every-drop-is-loud contract: one audit line per fenced mint
        self.assertTrue(
            any("mint_fenced_stale[force_push]" in r.message
                for r in captured.records)
        )
        # idempotence contract: already-fenced mints emit NO new audit line —
        # caplog.at_level in the pytest original only set the level, it never
        # asserted a record existed. Call directly, assert the empty result.
        self.assertEqual(
            fence.fence_mints_on_head_mutation(
                repo="unit/unit", pr=7, new_head="8b09617", event="force_push",
            ),
            [],
        )
        # unclosed taxonomy denies: an unknown event shape must not honor
        with self.assertRaises(ValueError):
            fence.fence_mints_on_head_mutation(
                repo="unit/unit", pr=7, new_head="deadbeef",
                event="mystery_event",
            )

    def test_settlement_denies_fenced_and_uncited(self):
        """assert_mint_live denies typed on fenced and never-cited mints."""
        with self.assertRaises(fence.MintFencingError) as ctx:
            fence.assert_mint_live(9999)
        self.assertEqual(ctx.exception.code, "mint_unregistered")

        fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
        )
        # live cited mint settles: returns its citation
        live = fence.assert_mint_live(4242)
        self.assertEqual(live["head"], "70b4531a")

        fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="8b09617", event="successor_pr",
        )
        with self.assertRaises(fence.MintFencingError) as ctx:
            fence.assert_mint_live(4242)
        self.assertEqual(ctx.exception.code, "mint_fenced_stale")
        self.assertIn("successor_pr", str(ctx.exception))
        self.assertIn("70b4531a", str(ctx.exception))
        self.assertIn("8b09617", str(ctx.exception))

    def test_reanchor_case_rescope_deferred_to_owner(self):
        """Fence records the event and stops; the owner re-anchors explicitly."""
        fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
        )
        fenced = fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="a8e4ad3b7658",
            event="re_anchor",
        )
        self.assertEqual(len(fenced), 1)  # fenced, typed — never silent
        # the fence did NOT auto-rescope: settlement still denies
        with self.assertRaises(fence.MintFencingError):
            fence.assert_mint_live(4242)
        # the ADR-0090 owner explicitly re-anchors: NEW citation lands live
        reanchored = fence.register_mint_reference(
            mint_id=4242, repo="unit/unit", pr=7, head="a8e4ad3b7658",
        )
        self.assertIs(reanchored["fenced"], False)
        self.assertEqual(reanchored["head"], "a8e4ad3b7658")
        self.assertIs(fence.assert_mint_live(4242), reanchored)

    def test_successor_pr_scoped_no_cross_pr_fencing(self):
        """A mutation on one PR never fences mints pinned to another PR."""
        fence.register_mint_reference(
            mint_id=1, repo="unit/unit", pr=7, head="70b4531a",
        )
        fence.register_mint_reference(
            mint_id=2, repo="unit/unit", pr=9, head="70b4531a",
        )
        fence.register_mint_reference(
            mint_id=3, repo="other/other", pr=7, head="70b4531a",
        )
        fenced = fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="8b09617", event="successor_pr",
        )
        self.assertEqual(len(fenced), 1)  # only mint 1: same repo AND same PR
        # untouched mints still settle live
        self.assertEqual(fence.assert_mint_live(2)["pr"], 9)
        self.assertEqual(fence.assert_mint_live(3)["repo"], "other/other")

    def test_cite_live_head_fences_previous_then_registers_live(self):
        """The mint path must call the fence when the cited head changes."""
        fence.cite_live_head("task:unit/unit#7", "unit/unit", 7, "70b4531a")
        live = fence.cite_live_head(
            "task:unit/unit#7", "unit/unit", 7, "a8e4ad3b7658", previous_head="70b4531a",
        )
        self.assertIs(live["fenced"], False)
        self.assertEqual(live["head"], "a8e4ad3b7658")
        self.assertEqual(
            fence.assert_mint_live("task:unit/unit#7")["head"], "a8e4ad3b7658",
        )
