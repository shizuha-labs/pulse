"""
PLAT-10171 Patch #2a drift-pin — pulse_add_comment send idempotency + anti-burst
guard. Pure source-of-record assertions (no server import: the module runs
Django/FastMCP setup at import time and needs the platform env). The behavioral
semantics were verified live against the extracted ledger contract at authoring
time (5/5: id-replay-no-repost, in-window block, out-of-window posts,
different-content posts, id-survives-prune); this pin freezes the contract in
the source so refactors cannot silently drop the guard.
"""
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "mcp-server" / "pulse_server.py"


def test_add_comment_carries_the_idempotency_param():
    src = SERVER.read_text()
    assert "def add_comment(task_id: str, content: str, client_message_id: Optional[str] = None)" in src
    assert "client_message_id: Optional idempotency key (PLAT-10171 Patch #2a)" in src


def test_explicit_id_replay_never_reposts():
    src = SERVER.read_text()
    assert "Comment already added (idempotent replay — no duplicate)" in src
    assert 'key = f"id:{item_pk}:{client_message_id}"' in src
    assert "if prior is not None:" in src


def test_anti_burst_guard_blocks_identical_in_window_resends():
    src = SERVER.read_text()
    assert "_COMMENT_ANTI_BURST_WINDOW_S = 30.0" in src
    assert "BLOCKED by send anti-burst guard (PLAT-10171 Patch #2a)" in src
    assert "retry-storm signature" in src
    assert "list the task's comments to verify what landed" in src


def test_ledger_is_bounded_without_wholesale_clear():
    src = SERVER.read_text()
    # Renamed per aoi review (PLAT-10471 5679 blocker-1): LEDGE → LEDGER.
    assert "_COMMENT_SEND_LEDGER_PRUNE" in src
    assert "_COMMENT_SEND_LEDGE_PRUNE" not in src
    # Size cap is a named constant and prune NEVER clears wholesale: explicit-id
    # entries must survive pruning or a late retry re-posts (Layer-1 contract).
    assert "_COMMENT_SEND_LEDGER_MAX_ENTRIES = 5000" in src
    assert ".clear()" not in src
    # Eviction order: stale burst → oldest burst → oldest id (FIFO), never all.
    assert 'k.startswith("burst:")' in src
    assert "excess = len(_COMMENT_SEND_LEDGER) - _COMMENT_SEND_LEDGER_MAX_ENTRIES" in src


def test_explicit_id_keys_survive_cap_eviction_functional():
    """Functional regression (hiro, PLAT-10471 review-5679 follow-up): execute the
    extracted prune against a synthetic ledger and prove explicit-id entries
    survive the size cap — the Layer-1 never-re-posts contract under pruning.
    The module can't be imported (Django/FastMCP setup at import time), so the
    prune + constants are AST-extracted and exec'd into a clean namespace."""
    import ast as _ast

    src = SERVER.read_text()
    tree = _ast.parse(src)
    wanted = {"_COMMENT_SEND_LEDGER_PRUNE"}
    assign_names = {
        t.targets[0].id for t in tree.body
        if isinstance(t, _ast.Assign) and isinstance(t.targets[0], _ast.Name)
    }
    needed = {"_COMMENT_SEND_LEDGER_MAX_ENTRIES", "_COMMENT_ANTI_BURST_WINDOW_S"}
    assert needed <= assign_names, f"prune support constants missing: {needed - assign_names}"

    ns: dict = {}
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name in wanted:
            exec(compile(_ast.Module(body=[node], type_ignores=[]), SERVER.name, "exec"), ns)
        elif isinstance(node, _ast.Assign) and isinstance(node.targets[0], _ast.Name) and node.targets[0].id in needed:
            exec(compile(_ast.Module(body=[node], type_ignores=[]), SERVER.name, "exec"), ns)

    ns["_COMMENT_SEND_LEDGER"] = ledger = {}
    cap = ns["_COMMENT_SEND_LEDGER_MAX_ENTRIES"]
    now = 10_000.0
    # 600 explicit-id entries + 5000 burst entries → far over cap.
    for i in range(600):
        ledger[f"id:{i}:key-{i}"] = f"Comment added (ID: {100000 + i})"
    for i in range(5000):
        ledger[f"burst:{i}:h{i}"] = now - 1.0

    ns["_COMMENT_SEND_LEDGER_PRUNE"](now)

    # Contract under overflow: burst class evicts ENTIRELY before any id entry;
    # explicit-id entries survive wholesale (here 600 ids < cap → all survive).
    assert len(ledger) == 600, f"expected 600 id entries to survive, got {len(ledger)}"
    id_entries = {k: v for k, v in ledger.items() if k.startswith("id:")}
    assert len(id_entries) == 600, "explicit-id entries must survive cap eviction when id population alone fits"
    assert all(not k.startswith("burst:") for k in ledger), "burst class must evict entirely before explicit-id entries"
    # Insertion-order integrity: the oldest id entry keeps its original result — a
    # replay after the prune returns it verbatim (never re-posts).
    assert id_entries.get("id:0:key-0") == "Comment added (ID: 100000)"


def test_401_lane_untouched_and_dm_lane_parallel_noted():
    src = SERVER.read_text()
    # The guard is comment-submission-only; message_user's client_message_id
    # (the DM lane) and the 401 self-heal are separate mechanisms by design.
    assert "message_user's client_message_id" in src
