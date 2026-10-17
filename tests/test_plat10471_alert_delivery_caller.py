"""PLAT-10471 / PLS-1380 — the alert-delivery relay's production caller.

Acceptance pins:
- The celery task exists, is bounded to the carrier's limit=25, and does not
  add celery-level retries on top of the relay's own durable backoff.
- setup_notification_schedules() registers the beat binding at the
  routing-outbox precedent cadence (30s).
- Scope-item 2 of the carrier (connect_receipt persistence on phase rows) is
  recorded as ALREADY IN-TREE via slice-3 — the only missing binding was the
  caller; this test pins that fact so the scope can't silently regress.
"""
import ast
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent / "tasks" / "tasks.py"


def _tree():
    return ast.parse(TASKS.read_text())


def test_drain_task_exists_bounded_and_no_celery_retry():
    tree = _tree()
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "drain_alert_delivery_outbox"),
        None,
    )
    assert fn is not None, "drain_alert_delivery_outbox task missing"
    decs = [ast.unparse(d) for d in fn.decorator_list]
    assert any("shared_task" in d and "max_retries=0" in d for d in decs), decs
    body = ast.unparse(fn)
    assert "drain_once(limit=25)" in body, "carrier binding must be limit=25"
    assert "from alertdelivery.relay import drain_once" in body


def test_beat_binding_registered_at_routing_outbox_cadence():
    src = TASKS.read_text()
    assert "name='Drain Alert Delivery Outbox'" in src
    assert "task='tasks.tasks.drain_alert_delivery_outbox'" in src
    # Cadence: inside the same materializer, adjacent to the 30s routing-outbox
    # precedent (interval_30sec is the only interval in that block).
    anchor = src.index("name='Drain Alert Delivery Outbox'")
    block = src[max(0, anchor - 400): anchor + 400]
    assert "interval_30sec" in block
    assert "enabled=True" in block


def test_connect_receipt_persistence_already_in_tree():
    """Scope-item 2 is satisfied by slice-3 (relay_one persists connect_receipt
    on both the success and ambiguous-reconcile paths). Pin it so the carrier
    scope is understood as caller-only."""
    relay = Path(__file__).resolve().parent.parent / "alertdelivery" / "relay.py"
    src = relay.read_text()
    assert "phase.connect_receipt = {" in src
    assert "_reconcile_ambiguous" in src
    assert "def drain_once(limit: int = 25)" in src
