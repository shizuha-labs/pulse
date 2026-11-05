"""PLAT-11158 surface 2: consume-anchored JIT TTL on escalation-evidence receipts.

Contract (PLAT-11154 comment 1005344):
- The TTL countdown starts at CONSUME, not at approval/mint.
- A receipt is gate-valid while unconsumed, and for
  RECEIPT_POST_CONSUME_WINDOW after its first consume event.
- An unconsumed receipt past RECEIPT_DORMANCY_WINDOW is flagged for
  re-request (revocation with audit).
- Consume events are reconstructible from the receipt trail alone.
"""
import uuid
from types import SimpleNamespace
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.services.escalation_routing import (
    RECEIPT_DORMANCY_WINDOW,
    RECEIPT_POST_CONSUME_WINDOW,
    flag_dormant_receipts,
    mark_receipt_consumed,
    receipt_gate_fields,
)


class ReceiptGateStateTest(SimpleTestCase):
    """Pure gate-state computation (no DB)."""

    def test_unconsumed_receipt_is_valid(self):
        receipt = SimpleNamespace(
            consumed_at=None, consumed_by_transition_id=None,
        )
        fields = receipt_gate_fields(receipt, now=timezone.now())
        self.assertEqual(fields["gate_state"], "unconsumed_valid")
        self.assertIsNone(fields["consumed_at"])

    def test_consumed_within_window_is_valid(self):
        now = timezone.now()
        receipt = SimpleNamespace(
            consumed_at=now - (RECEIPT_POST_CONSUME_WINDOW - timedelta(hours=1)),
            consumed_by_transition_id="8b1c5f2a-0000-4000-8000-000000000001",
        )
        fields = receipt_gate_fields(receipt, now=now)
        self.assertEqual(fields["gate_state"], "consumed_within_window")
        self.assertEqual(
            fields["consumed_by_transition_id"],
            "8b1c5f2a-0000-4000-8000-000000000001",
        )

    def test_consumed_past_window_is_spent(self):
        now = timezone.now()
        receipt = SimpleNamespace(
            consumed_at=now - (RECEIPT_POST_CONSUME_WINDOW + timedelta(hours=1)),
            consumed_by_transition_id=None,
        )
        fields = receipt_gate_fields(receipt, now=now)
        self.assertEqual(fields["gate_state"], "spent")

    def test_boundary_exact_window_is_still_within(self):
        now = timezone.now()
        receipt = SimpleNamespace(consumed_at=now - RECEIPT_POST_CONSUME_WINDOW)
        fields = receipt_gate_fields(receipt, now=now)
        self.assertEqual(fields["gate_state"], "consumed_within_window")


class ReceiptConsumeStampingTest(TestCase):
    """DB-backed consume stamping + dormancy sweep (runs in CI's apiserver DB)."""

    def _make_receipt(self, *, created_days_ago=0, consumed=None):
        from tasks.models import (
            EscalationRoutingDisposition,
            EscalationRoutingReceipt,
            Item,
            Project,
        )
        from django.contrib.auth import get_user_model

        suffix = uuid.uuid4().hex[:10]
        owner = get_user_model().objects.create_user(
            username=f"plat11158-{suffix}", email="saki@shizuha.com",
        )
        project = Project.objects.create(
            name=f"plat11158-{suffix}", key=f"P{suffix[:4]}", org_id=1,
        )
        item = Item.objects.create(
            title="PLAT-11158 fixture", project=project,
            created_by_id=owner.id, created_by_email=owner.email,
        )
        disposition = EscalationRoutingDisposition.objects.create(
            organization_id=project.org_id,
            project=project,
            item=item,
            transition_id="0f3e6c18-0000-4000-8000-000000000004",
            source_generation=1,
            generation=1,
            schema_version="v1",
            classification="operator_request_hygiene",
            outcome="routed",
            reason_code="test",
            disposition_digest="dig-" + ("x" * 32),
        )
        receipt = EscalationRoutingReceipt.objects.create(
            organization_id=project.org_id,
            project=project,
            item=item,
            transition_id="5c2f9f40-0000-4000-8000-000000000002",
            actor_subject_id="agent:saki",
            actor_assertion_digest="ass-" + ("y" * 32),
            primary_request_id="d1a7b3e0-0000-4000-8000-000000000003",
            source_generation=1,
            disposition=disposition,
            classification=disposition.classification,
            outcome=disposition.outcome,
            reason_code=disposition.reason_code,
        )
        if created_days_ago:
            shifted = timezone.now() - timedelta(days=created_days_ago)
            type(receipt).objects.filter(pk=receipt.pk).update(created_at=shifted)
            receipt.refresh_from_db()
        if consumed is not None:
            receipt.consumed_at = consumed
            receipt.save(update_fields=["consumed_at"])
        return receipt

    def test_first_stamp_wins_and_second_is_noop(self):
        receipt = self._make_receipt()
        first = mark_receipt_consumed(
            receipt, transition_id="c47a1d22-0000-4000-8000-0000000000a1",
        )
        self.assertTrue(first)
        stamped_at = receipt.consumed_at
        second = mark_receipt_consumed(
            receipt, transition_id="c47a1d22-0000-4000-8000-0000000000a2",
        )
        self.assertFalse(second)
        receipt.refresh_from_db()
        self.assertEqual(receipt.consumed_at, stamped_at)
        self.assertEqual(
            str(receipt.consumed_by_transition_id),
            "c47a1d22-0000-4000-8000-0000000000a1",
        )

    def test_consume_events_reconstructible_from_receipt_trail(self):
        receipt = self._make_receipt()
        mark_receipt_consumed(
            receipt, transition_id="c47a1d22-0000-4000-8000-0000000000a9",
        )
        receipt.refresh_from_db()
        fields = receipt_gate_fields(receipt)
        self.assertIsNotNone(fields["consumed_at"])
        self.assertEqual(
            fields["consumed_by_transition_id"],
            "c47a1d22-0000-4000-8000-0000000000a9",
        )
        self.assertEqual(fields["gate_state"], "consumed_within_window")

    def test_dormancy_sweep_flags_only_stale_unconsumed(self):
        fresh_unconsumed = self._make_receipt()
        stale_unconsumed = self._make_receipt(
            created_days_ago=int(RECEIPT_DORMANCY_WINDOW.days) + 2,
        )
        consumed_old = self._make_receipt(
            created_days_ago=int(RECEIPT_DORMANCY_WINDOW.days) + 2,
            consumed=timezone.now() - timedelta(days=1),
        )

        count, flagged_pks = flag_dormant_receipts()
        flagged = {str(r.pk) for r in flagged_pks}

        self.assertEqual(count, 1)
        self.assertIn(str(stale_unconsumed.pk), flagged)
        self.assertNotIn(str(fresh_unconsumed.pk), flagged)
        self.assertNotIn(str(consumed_old.pk), flagged)

        # Idempotent: the sweep never re-flags.
        count_again, _ = flag_dormant_receipts()
        self.assertEqual(count_again, 0)
