"""PLAT-9838 — lifetime head-mutation fence tests (pair for PLAT-9837).

Covers: mint-time citation equality (idempotent re-cite), force-push
fencing with typed `mint_fenced_stale` + audit line, settlement denies
(fenced / uncited), the re-anchor case (rescope deferred to the ADR-0090
owner — fence records, never auto-rescopes), and successor-PR scoping
(no cross-PR fencing).
"""

from __future__ import annotations

import logging

import pytest

from tasks.services import head_mutation_fence as fence


@pytest.fixture(autouse=True)
def _clean_registry():
    fence._MINTS.clear()
    yield
    fence._MINTS.clear()


def test_register_cites_live_head_and_is_idempotent():
    """Citation is literal (repo/pr/head) and exact re-cites are no-ops."""
    first = fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
    )
    assert first == {"repo": "unit/unit", "pr": 7, "head": "70b4531a",
                     "fenced": False, "fence": None}
    # exact re-cite: same record, no duplicate
    again = fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
    )
    assert again is first
    # partial / heuristic pins deny
    with pytest.raises(ValueError):
        fence.register_mint_reference(mint_id=1, repo="", pr=7, head="x")
    with pytest.raises(ValueError):
        fence.register_mint_reference(mint_id=1, repo="u/u", pr=0, head="x")
    with pytest.raises(ValueError):
        fence.register_mint_reference(mint_id=1, repo="u/u", pr=7, head="")


def test_force_push_fences_outstanding_mints_typed_and_loud(caplog):
    """force_push fences the mint citing the old head: typed + audit line."""
    fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
    )
    with caplog.at_level(logging.WARNING):
        fenced = fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="8b09617", event="force_push",
        )
    assert len(fenced) == 1
    fence_rec = fenced[0]["fence"]
    assert fence_rec["code"] == "mint_fenced_stale"
    assert fence_rec["event"] == "force_push"
    assert fence_rec["old_head"] == "70b4531a"
    assert fence_rec["new_head"] == "8b09617"
    # the every-drop-is-loud contract: one audit line per fenced mint
    assert any("mint_fenced_stale[force_push]" in r.message for r in caplog.records)
    with caplog.at_level(logging.WARNING):
        assert fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="8b09617", event="force_push",
        ) == []
    # unclosed taxonomy denies: an unknown event shape must not honor
    with pytest.raises(ValueError):
        fence.fence_mints_on_head_mutation(
            repo="unit/unit", pr=7, new_head="deadbeef",
            event="mystery_event",
        )


def test_settlement_denies_fenced_and_uncited():
    """assert_mint_live denies typed on fenced and never-cited mints."""
    with pytest.raises(fence.MintFencingError) as exc:
        fence.assert_mint_live(9999)
    assert exc.value.code == "mint_unregistered"

    fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
    )
    # live cited mint settles: returns its citation
    live = fence.assert_mint_live(4242)
    assert live["head"] == "70b4531a"

    fence.fence_mints_on_head_mutation(
        repo="unit/unit", pr=7, new_head="8b09617", event="successor_pr",
    )
    with pytest.raises(fence.MintFencingError) as exc:
        fence.assert_mint_live(4242)
    assert exc.value.code == "mint_fenced_stale"
    assert "successor_pr" in str(exc.value)
    assert "70b4531a" in str(exc.value) and "8b09617" in str(exc.value)


def test_reanchor_case_rescope_deferred_to_owner():
    """Fence records the event and stops; the owner re-anchors explicitly."""
    fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="70b4531a",
    )
    fenced = fence.fence_mints_on_head_mutation(
        repo="unit/unit", pr=7, new_head="a8e4ad3b7658",
        event="re_anchor",
    )
    assert len(fenced) == 1  # fenced, typed — never silent
    # the fence did NOT auto-rescope: settlement still denies
    with pytest.raises(fence.MintFencingError):
        fence.assert_mint_live(4242)
    # the ADR-0090 owner explicitly re-anchors: NEW citation lands live
    reanchored = fence.register_mint_reference(
        mint_id=4242, repo="unit/unit", pr=7, head="a8e4ad3b7658",
    )
    assert reanchored["fenced"] is False
    assert reanchored["head"] == "a8e4ad3b7658"
    assert fence.assert_mint_live(4242) is reanchored


def test_successor_pr_scoped_no_cross_pr_fencing():
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
    assert len(fenced) == 1  # only mint 1: same repo AND same PR
    # untouched mints still settle live
    assert fence.assert_mint_live(2)["pr"] == 9
    assert fence.assert_mint_live(3)["repo"] == "other/other"


def test_cite_live_head_fences_previous_then_registers_live():
    """The mint path must call the fence when the cited head changes."""
    fence.cite_live_head("task:unit/unit#7", "unit/unit", 7, "70b4531a")
    live = fence.cite_live_head(
        "task:unit/unit#7", "unit/unit", 7, "a8e4ad3b7658", previous_head="70b4531a",
    )
    assert live["fenced"] is False
    assert live["head"] == "a8e4ad3b7658"
    assert fence.assert_mint_live("task:unit/unit#7")["head"] == "a8e4ad3b7658"
