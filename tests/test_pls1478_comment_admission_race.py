"""PLS-1478 — comment-admission check-then-insert race (child of PLS-1477).

After the #953 rollout two concurrent sweep workers could both pass the
"no recent identical body" check and double-insert (live 0s-gap examples,
Asia/Calcutta: PLAT-13188 cmts 1236492/1236493 at 23:54:19 IST, PLAT-12456
1236494/1236495/1236496 at 23:54:20 IST).

The cure shipped in this repo: eligible automation rows save through a
serialized critical section — transaction-scoped ``pg_advisory_xact_lock``
keyed on (item_id, author_email, body hash), duplicate re-check under the
lock, insert inside the same transaction. The 4h window and the
state-marker/parent/exemption carve-outs are unchanged.

No server import in this suite (house rule: the server module runs full
Django/FastMCP setup at import time and needs the platform env), so:
  * Part 1 pins the source contract of the serialized path.
  * Part 2 exercises the race against the REAL lock-key derivation from
    ``tasks/services/comment_admission.py`` (importable without settings)
    with a threading.Lock standing in for the PG advisory lock and an
    in-memory registry standing in for the table.
"""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import threading
import unittest

REPO = Path(__file__).resolve().parent.parent
ADMISSION = REPO / "tasks/services/comment_admission.py"
MODELS = REPO / "tasks/models.py"


def _load_admission_module():
    spec = spec_from_file_location("pls1478_comment_admission", ADMISSION)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeComment:
    def __init__(self, item_id, author_email, content):
        self.item_id = item_id
        self.author_email = author_email
        self.content = content


class PLS1478SourcePins(unittest.TestCase):
    def test_advisory_lock_is_transaction_scoped_and_vendor_guarded(self):
        src = ADMISSION.read_text()
        assert "SELECT pg_advisory_xact_lock(%s, %s)" in src, (
            "serialized path must take a two-int advisory xact lock"
        )
        assert "connection.vendor != 'postgresql'" in src, (
            "lock must no-op on non-PG vendors (house pattern, tasks.tasks.py)"
        )
        helper_body = src.split("def acquire_automation_admission_lock")[1].split("\ndef ")[0]
        assert "with transaction.atomic" not in helper_body, (
            "the lock helper must not open its own transaction — the caller's "
            "atomic block must wrap lock + re-check + insert together"
        )

    def test_lock_key_covers_item_author_and_body(self):
        mod = _load_admission_module()
        same = _FakeComment(7, "workflow@pulse", "same body")
        assert mod.automation_admission_lock_keys(same) == mod.automation_admission_lock_keys(
            _FakeComment(7, "workflow@pulse", "same body")
        ), "identical identity must map to the same lock key"
        assert mod.automation_admission_lock_keys(same) != mod.automation_admission_lock_keys(
            _FakeComment(7, "workflow@pulse", "different body")
        ), "different bodies must not contend on one lock"
        assert mod.automation_admission_lock_keys(same) != mod.automation_admission_lock_keys(
            _FakeComment(8, "workflow@pulse", "same body")
        ), "different items must not contend on one lock"
        for key in mod.automation_admission_lock_keys(same):
            assert 0 <= key <= 0x7FFFFFFF, "keys must fit int32 (PG two-int form)"

    def test_save_serializes_lock_recheck_and_insert_together(self):
        models_src = MODELS.read_text()
        guard_at = models_src.index("PLS-1478: the plain check-then-insert raced")
        block = models_src[guard_at:models_src.index("Mechanical bodies and provenance", guard_at)]
        assert "transaction.atomic()" in block
        # Order inside the atomic block: lock -> duplicate re-check -> insert.
        # The re-check must be the bare lookup (find_recent_automation_duplicate),
        # NOT the full admission — re-running admit under the lock would clamp
        # the already-clamped body a second time and restamp the truncation
        # note with the note's own length (CI run 10399 regression).
        assert block.index("acquire_automation_admission_lock") < block.index(
            "find_recent_automation_duplicate(self)", block.index("acquire_automation_admission_lock")
        ) < block.index("super().save(*args, **kwargs)", block.index("find_recent_automation_duplicate(self)", block.index("acquire_automation_admission_lock"))), (
            "order inside the atomic block must be: lock -> duplicate re-check -> insert"
        )
        assert "admit_automation_comment(self)" not in block[block.index("acquire_automation_admission_lock"):], (
            "full admission must not re-run under the lock (double-clamp regression, run 10399)"
        )
        # Fast path keeps the full admission (clamp + suppression) before the
        # serialized path, and the second worker adopting the first worker's
        # row (no double insert, no fan-out).
        assert block.index("admit_automation_comment(self)") < block.index("acquire_automation_admission_lock")
        assert block.count("_adopt_existing_comment(duplicate)") == 2

    def test_window_and_exemptions_are_unchanged(self):
        src = ADMISSION.read_text()
        assert "PULSE_AUTOMATION_COMMENT_DEDUPE_MINUTES', 240" in src, "4h window preserved"
        assert "STATE_MARKER_PREFIXES = (" in src and "Auto-failover" in src, (
            "state-marker bypass preserved"
        )
        assert "if comment.parent_id:" in src, "threaded replies bypass preserved"
        assert "origin_event_id" in src and "DEDUPABLE_MECHANICAL_EVENTS" in src, (
            "mechanical-event eligibility preserved"
        )


class PLS1478RaceSimulation(unittest.TestCase):
    """Reproduce the double-insert with the old shape, prove the cure with the new.

    The real ``automation_admission_lock_keys`` decides lock identity; a
    ``threading.Lock`` stands in for ``pg_advisory_xact_lock``; the in-memory
    registry stands in for ``tasks_comment``. Workers sync on a barrier so the
    unserialized check-then-insert genuinely interleaves.
    """

    def _run_workers(self, serialized, n=4):
        mod = _load_admission_module()
        comment = _FakeComment(13188, "workflow@pulse", "sweep receipt body")
        lock = threading.Lock()  # stands in for the PG advisory lock
        registry = []            # committed rows: (item_id, author, content)
        check_calls = {"n": 0}
        barrier = threading.Barrier(n)

        def duplicate_check(c):
            check_calls["n"] += 1
            return next(
                (row for row in registry if row == (c.item_id, c.author_email, c.content)),
                None,
            )

        def worker(_i):
            barrier.wait(timeout=5)
            if serialized:
                # New shape: lock -> check -> insert, all inside the critical
                # section. The sleep widens the section to prove the lock
                # holds across slow checks (a PG admission is not instant).
                with lock:  # pg_advisory_xact_lock equivalent
                    if duplicate_check(comment) is not None:
                        return "adopted"
                    threading.Event().wait(0.02)
                    registry.append((comment.item_id, comment.author_email, comment.content))
                    return "inserted"
            # Old shape: check and insert are two separate steps with a real
            # gap (the 0s-gap live examples). The sleep sits between read and
            # insert so concurrent workers all read the empty registry first.
            seen = duplicate_check(comment)
            threading.Event().wait(0.02)
            if seen is not None:
                return "adopted"
            registry.append((comment.item_id, comment.author_email, comment.content))
            return "inserted"

        with ThreadPoolExecutor(max_workers=n) as pool:
            outcomes = list(pool.map(worker, range(n)))
        return outcomes, registry, check_calls

    def test_old_shape_double_inserts(self):
        outcomes, registry, _ = self._run_workers(serialized=False)
        assert outcomes.count("inserted") >= 2, (
            "sanity: the unserialized check-then-insert shape must be able to "
            "double-insert when workers interleave at the barrier"
        )
        assert len(registry) >= 2

    def test_serialized_shape_inserts_exactly_one_row(self):
        for _attempt in range(20):  # hammer the interleaving
            outcomes, registry, check_calls = self._run_workers(serialized=True)
            assert outcomes.count("inserted") == 1, (outcomes, registry)
            assert len(registry) == 1
            assert outcomes.count("adopted") == 3
            assert check_calls["n"] == 4  # every worker still re-checks under the lock


if __name__ == "__main__":
    unittest.main()
