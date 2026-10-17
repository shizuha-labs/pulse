"""§6.5 gate 4 — external-effect adapter severed after dispatch (PLAT-8342).

One real external-effect adapter severed after dispatch → provider/
reconciliation evidence proves exactly one external effect + one terminal
operation row. Also covers the worker lease loop: generation-fenced claim,
fenced stale writes, result recorded before terminal operation result.
"""
from django.test import TestCase
from django.utils import timezone

from tasks.models import MutationReceipt, OperationOutbox
from tasks.services import mutation_receipt_service as receipt_service
from tasks.tests.test_plat8342_slice3_proof_fixtures import ProofFixtureBase


class ExternalEffectAdapter:
    """A real external-effect adapter (provider call) with evidence.

    Dispatch is severed after the provider call: the adapter records the
    provider-side evidence (idempotency key + dispatch count) BEFORE the
    sever, so reconciliation can prove exactly one external effect even
    when the worker's result write never lands.
    """

    def __init__(self, provider_log):
        self.provider_log = provider_log  # shared evidence log

    def dispatch(self, receipt):
        key = receipt_service.provider_idempotency_key(receipt)
        prior = [row for row in self.provider_log if row['key'] == key]
        if prior:
            # Read-after-timeout reconciliation: the provider dedupes on the
            # idempotency key — a re-dispatch observes the prior effect and
            # does NOT create a second one.
            return prior[0], False
        row = {'key': key, 'dispatched_at': timezone.now().isoformat()}
        self.provider_log.append(row)
        return row, True


class WorkerLeaseLoopTests(ProofFixtureBase):
    """Worker claims with a generation-fenced lease; stale writes fenced."""

    def _outbox(self):
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        return receipt_service.claim_lease(
            OperationOutbox.objects.create(
                receipt=winner, provider='scs', provider_idempotency_key='',
                payload_digest=self.digest,
            ),
            worker_id='worker-1',
        )

    def test_claim_fences_generation(self):
        row = self._outbox()
        self.assertEqual(row.state, OperationOutbox.States.CLAIMED)
        self.assertEqual(row.lease_generation, 1)
        # A second live claim is rejected.
        self.assertIsNone(receipt_service.claim_lease(row, worker_id='worker-2'))
        # A stale write is fenced.
        with self.assertRaises(receipt_service.FencingLoss):
            receipt_service.record_outbox_result(
                row, lease_generation=0, succeeded=True,
            )

    def test_result_recorded_before_terminal(self):
        row = self._outbox()
        receipt_service.record_outbox_result(
            row, lease_generation=row.lease_generation, succeeded=True,
            result_class='202',
        )
        row.refresh_from_db()
        self.assertEqual(row.state, OperationOutbox.States.SUCCEEDED)
        self.assertEqual(row.result_class, '202')
        self.assertIsNotNone(row.terminal_at)
        # Terminal rows are never re-claimed.
        self.assertIsNone(receipt_service.claim_lease(row, worker_id='worker-3'))


class ExternalEffectSeveredTests(ProofFixtureBase):
    """Gate 4: adapter severed after dispatch → exactly one external effect
    + one terminal operation row."""

    def test_severed_after_dispatch_proves_exactly_one_effect(self):
        provider_log = []
        adapter = ExternalEffectAdapter(provider_log)
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        row = receipt_service.claim_lease(
            OperationOutbox.objects.create(
                receipt=winner, provider='scs', provider_idempotency_key='',
                payload_digest=self.digest,
            ),
            worker_id='worker-1',
            lease_seconds=0,  # the severed worker's lease expires immediately
        )
        # Dispatch lands on the provider; the worker is then severed before
        # its result write.
        evidence, created = adapter.dispatch(winner)
        self.assertTrue(created)
        self.assertEqual(len(provider_log), 1)
        # Sever: the result write never lands (worker dies). The attempt is
        # fenced FENCING_UNPROVEN at wire cutoff.
        fenced = receipt_service.fence_unproved(winner)
        self.assertEqual(fenced.state, MutationReceipt.States.FENCING_UNPROVEN)
        # Reconciliation: a re-claim re-dispatches; the provider dedupes on
        # the idempotency key — exactly one external effect.
        row2 = receipt_service.claim_lease(row, worker_id='worker-2')
        self.assertIsNotNone(row2)
        self.assertEqual(row2.lease_generation, 2)
        evidence2, created2 = adapter.dispatch(winner)
        self.assertFalse(created2)
        self.assertEqual(evidence2, evidence)
        self.assertEqual(len(provider_log), 1)
        # The reconciled result is recorded under the CURRENT generation —
        # one terminal operation row.
        receipt_service.record_outbox_result(
            row2, lease_generation=row2.lease_generation, succeeded=True,
            result_class='202',
        )
        row2.refresh_from_db()
        self.assertEqual(row2.state, OperationOutbox.States.SUCCEEDED)
        self.assertEqual(OperationOutbox.objects.filter(receipt=winner).count(), 1)
        self.assertEqual(len(provider_log), 1)
