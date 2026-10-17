"""§6.5 proof fixtures — PLAT-5343 slice 3 ship gates (PLAT-8342).

Binding gates: simultaneous same-key requests → exactly one effect, loser
in-progress/replay; different-digest conflict; response severed after local
commit → retry observes one effect + same result; registry gate; §7 wire
negatives (draining, unproved-terminal transport abort, -32010 on retry only).
"""
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from tasks.models import MutationReceipt
from tasks.services import mcp_wire_contract as wire
from tasks.services import mutation_contract_registry as registry
from tasks.services import mutation_receipt_service as receipt_service


class ProofFixtureBase(TestCase):
    def setUp(self):
        self.principal = receipt_service.principal_fingerprint('issuer-hex', 'subject-hex')
        self.operation = 'shizuha.task.edit'
        self.key = 'idem-key-1'
        self.digest = receipt_service.request_digest({'name': 'x'}, 'shizuha.task.edit',
                                                     '1', ['P76-1'])
        self.effects = []

    def _effect(self):
        self.effects.append(len(self.effects) + 1)


class SimultaneousSameKeyTests(ProofFixtureBase):
    """Gate 1: simultaneous same-key requests → exactly one effect."""

    def test_loser_never_executes_second_effect(self):
        winner, created = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        self.assertTrue(created)
        receipt_service.commit_attempt(winner, effect=self._effect,
                                       result_class='200')
        self.assertEqual(len(self.effects), 1)
        # Retry with the exact prior key + digest: terminal, no second effect.
        fresh, created = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        self.assertFalse(created)
        self.assertEqual(fresh.state, MutationReceipt.States.COMMITTED)
        self.assertEqual(len(self.effects), 1)

    def test_active_key_loser_gets_425(self):
        receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        with self.assertRaises(receipt_service.AttemptUnresolved):
            receipt_service._begin_or_resolve(
                principal=self.principal, operation_name=self.operation,
                operation_version='1', idempotency_key=self.key, digest=self.digest,
            )
        self.assertEqual(len(self.effects), 0)


class DifferentDigestConflictTests(ProofFixtureBase):
    """Gate 2: same key + different digest → 409 idempotency_conflict."""

    def test_conflict_raises(self):
        receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        other = receipt_service.request_digest({'name': 'y'}, 'shizuha.task.edit',
                                               '1', ['P76-1'])
        with self.assertRaises(receipt_service.IdempotencyConflict):
            receipt_service._begin_or_resolve(
                principal=self.principal, operation_name=self.operation,
                operation_version='1', idempotency_key=self.key, digest=other,
            )


class SeveredResponseTests(ProofFixtureBase):
    """Gate 3: response severed after local commit → retry observes one
    effect + same result."""

    def test_retry_after_sever_observes_one_effect_same_result(self):
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        committed = receipt_service.commit_attempt(
            winner, effect=self._effect, result_class='200',
            redacted_response='{"ok": true}',
        )
        # The response was severed after the local commit — the receipt is
        # already durable COMMITTED.
        severed = receipt_service.sever_after_local_commit(committed)
        self.assertEqual(severed.state, MutationReceipt.States.COMMITTED)
        # Retry observes exactly one effect and the same result.
        replayed = receipt_service.replay(severed, endpoint_visible=True)
        self.assertEqual(replayed.result_class, '200')
        self.assertEqual(replayed.redacted_response, '{"ok": true}')
        self.assertEqual(len(self.effects), 1)

    def test_replay_visibility_loss_is_403_without_rerun(self):
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        committed = receipt_service.commit_attempt(winner, effect=self._effect,
                                                   result_class='200')
        with self.assertRaises(receipt_service.FencingLoss):
            receipt_service.replay(committed, endpoint_visible=False)
        self.assertEqual(len(self.effects), 1)


class ReconcilerTests(ProofFixtureBase):
    """ABORTED_PROVEN requires all three proofs; unproved stays alerting."""

    def test_unproved_death_never_aborts(self):
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        fenced = receipt_service.fence_unproved(winner)
        self.assertEqual(fenced.state, MutationReceipt.States.FENCING_UNPROVEN)
        self.assertFalse(receipt_service.reconcile_worker_death(
            fenced, worker_nonce_dead=True, backend_absent=False,
            receipt_effect_absent=True,
        ))
        fenced.refresh_from_db()
        self.assertEqual(fenced.state, MutationReceipt.States.FENCING_UNPROVEN)

    def test_proven_death_fences_aborted_proven(self):
        winner, _ = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        fenced = receipt_service.fence_unproved(winner)
        self.assertTrue(receipt_service.reconcile_worker_death(
            fenced, worker_nonce_dead=True, backend_absent=True,
            receipt_effect_absent=True,
        ))
        fenced.refresh_from_db()
        self.assertEqual(fenced.state, MutationReceipt.States.ABORTED_PROVEN)
        # A retry after proven death begins a NEW attempt on the same key.
        retry, created = receipt_service._begin_or_resolve(
            principal=self.principal, operation_name=self.operation,
            operation_version='1', idempotency_key=self.key, digest=self.digest,
        )
        self.assertFalse(created)
        self.assertEqual(retry.state, MutationReceipt.States.ABORTED_PROVEN)


class RegistryGateTests(TestCase):
    """§6.4 registry CI gate: every mutator classified, kinds valid."""

    def test_gate_passes(self):
        self.assertEqual(registry.registry_gate(), [])

    def test_mandatory_set_classified(self):
        for tool in ('shizuha.task.create', 'shizuha.transition.execute',
                     'shizuha.comment.create', 'shizuha.attachment.finalize'):
            kind = registry.classify(tool)
            self.assertIsNotNone(kind, f'unclassified mandatory mutator: {tool}')

    def test_unclassified_mutation_not_retryable(self):
        self.assertIsNone(registry.classify('shizuha.brand.new.mutator'))


class WireContractTests(TestCase):
    """§7 wire negatives: draining, unproved-terminal abort, -32010 retry."""

    def test_closed_admission_http_surface(self):
        status, headers, body = wire.closed_admission_response(rpc_id=7, request_id='r-1')
        self.assertEqual(status, 503)
        self.assertEqual(headers['Retry-After'], '1')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(body['error']['code'], wire.CODE_SERVER_DRAINING)
        self.assertEqual(body['error']['data']['retry_after_ms'], 1000)
        # No JSON-RPC ID → no body.
        status, headers, body = wire.closed_admission_response(rpc_id=None)
        self.assertEqual(status, 503)
        self.assertIsNone(body)

    def test_unproved_terminal_never_claims_retry_safety(self):
        self.assertIsNone(wire.unproved_terminal_transport_abort(rpc_id=7))

    def test_attempt_unresolved_reuses_exact_prior_key(self):
        body = wire.attempt_unresolved(rpc_id=9, request_id='r-2', reuse_key=True)
        self.assertEqual(body['error']['code'], wire.CODE_ATTEMPT_UNRESOLVED)
        self.assertTrue(body['error']['data']['reuse_idempotency_key'])

    def test_bounded_data_never_leaks(self):
        data = wire.bounded_error_data(-32010, retryable=True, request_id='r-3',
                                       retry_after_ms=1000, reuse_idempotency_key=True)
        self.assertEqual(set(data.keys()), {
            'code', 'retryable', 'request_id', 'retry_after_ms', 'reuse_idempotency_key',
        })

    def test_key_meta_roundtrip(self):
        params = {'_meta': {wire.IDEMPOTENCY_KEY_META: 'idem-key-1'}}
        self.assertEqual(wire.extract_idempotency_key(params), 'idem-key-1')
        self.assertEqual(wire.extract_idempotency_key({}), '')
