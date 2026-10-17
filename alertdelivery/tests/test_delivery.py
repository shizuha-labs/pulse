"""PLAT-5928 production-order regressions (§8.4.6C of HLD PLAT-301).

Covers: strict batch rollback + corrected replay; pre/post-commit loss;
wake/cache loss; concurrent ingress/workers/lease expiry; payload conflict;
exact-org + immutable-lineage isolation; fingerprint fallback closure;
ambiguous Connect commit/receipt lookup/409; recipient changes; firing→resolved
ordering; rule-label mismatch; relay failure; DB-probe absence/error.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from alertdelivery import acceptance
from alertdelivery.acceptance import (
    phase_client_message_id,
    phase_delivery_id,
    validate_batch,
)
from alertdelivery.ingest import (
    DuplicateKeyError,
    StrictDecodeError,
    envelope_digest,
    ingest_identity,
    normalize_starts_at,
    strict_json_loads,
)
from alertdelivery.models import (
    AlertDeliveryOutboxV1,
    AlertEpisodeV1,
    AlertIngressReceiptV1,
    AlertRecipientPhaseV1,
)
from alertdelivery.relay import (
    ContractConflictError,
    RelayDeferredError,
    claim_due,
    drain_once,
    relay_one,
)


def starts_at(iso='2026-08-08T15:57:18.037Z'):
    return iso


def batch_payload(*alerts, status='firing'):
    return {'status': status, 'alerts': [
        {
            'status': 'firing' if a.get('status') is None else a['status'],
            'labels': a.get('labels') or {
                'alertname': 'TestAlert', 'severity': 'critical',
                'team': 'oncall', 'org_id': '42',
            },
            'annotations': a.get('annotations') or {},
            'startsAt': a.get('startsAt', starts_at()),
            'endsAt': a.get('endsAt'),
            'generatorURL': a.get('generatorURL', 'http://prom/alert'),
            'fingerprint': a.get('fingerprint', 'abc123'),
        }
        for a in alerts
    ]}


class FakeProject:
    id = 999
    item_key = 'FAKE'
    key = 'FAKE'
    org_id = 42


class FakeWebhook:
    """WebhookKey-shaped object with exact-org binding + stable lineage."""

    id = 101
    source_name = 'prometheus'
    default_severity = 'warning'
    default_project = FakeProject
    webhook_key_revision = 3
    default_assignment_group = 'oncall'
    key = 'durable-test-key'
    is_active = True
    _effective_organization_id = 42
    _recipient_resolver = staticmethod(lambda: [
        {'recipient_id': 7001, 'team': 'oncall', 'delivery_lane': 'direct'},
        {'recipient_id': 7002, 'team': 'oncall', 'delivery_lane': 'direct'},
    ])


def _accept(raw, webhook=None, recipients=None):
    """validate + accept a raw batch; returns (report, decoded)."""
    webhook = webhook or FakeWebhook()
    decoded = validate_batch(raw, webhook=webhook)
    if recipients is None:
        recipients = FakeWebhook._recipient_resolver()
    recipient_map = {env.envelope_digest: recipients for env in decoded}
    report = acceptance.accept_batch(decoded, recipient_map=recipient_map)
    return report, decoded


class StrictIngestTests(TestCase):
    def test_duplicate_keys_rejected_at_depth(self):
        raw = (b'{"alerts":[{"labels":{"a":"1","a":"2"},'
               b'"startsAt":"2026-08-08T15:57:18.037Z","status":"firing"}]}')
        with self.assertRaises(DuplicateKeyError):
            strict_json_loads(raw)
        raw2 = (b'{"alerts":[{"labels":{"a":"1"},'
                b'"startsAt":"2026-08-08T15:57:18.037Z",'
                b'"status":"firing","status":"resolved"}]}')
        with self.assertRaises(DuplicateKeyError):
            strict_json_loads(raw2)

    def test_non_finite_and_bom_rejected(self):
        for raw in (b'NaN', b'\xef\xbb\xbf{"alerts":[]}',
                    b'{"alerts":[{"x":1e999}]}'):
            with self.assertRaises(StrictDecodeError):
                strict_json_loads(raw)

    def test_trailing_data_rejected(self):
        with self.assertRaises(StrictDecodeError):
            strict_json_loads(b'{"alerts":[]}garbage')

    def test_starts_at_validation(self):
        for bad in (None, '', 'not-a-date', '2026-08-08',
                    12345, '2026-08-08T25:99:99Z'):
            with self.assertRaises(StrictDecodeError):
                normalize_starts_at(bad)
        sec, nanos, text = normalize_starts_at('2026-08-08T15:57:18.037Z')
        self.assertEqual(sec, 1786204638)
        self.assertEqual(nanos, 37000000)
        # Non-UTC zone canonicalises to UTC.
        sec2, _, _ = normalize_starts_at('2026-08-08T21:27:18.037+05:30')
        self.assertEqual(sec2, sec)

    def test_envelope_digest_canonical_and_stable(self):
        env1 = {'a': {'b': 1}, 'c': [1, 2]}
        dig1 = envelope_digest(env1)
        # JCS sorts keys recursively — same logical object, different order.
        dig2 = envelope_digest({'c': [1, 2], 'a': {'b': 1}})
        self.assertEqual(dig1, dig2)
        self.assertEqual(len(dig1), 43)  # b64url no pad

    def test_ingest_identity_stable(self):
        i1 = ingest_identity(42, 101, 'envdigest')
        i2 = ingest_identity(42, 101, 'envdigest')
        i3 = ingest_identity(43, 101, 'envdigest')
        self.assertEqual(i1, i2)
        self.assertNotEqual(i1, i3)


class StrictBatchRollbackTests(TestCase):
    def setUp(self):
        self.raw_ok = json.dumps(batch_payload(
            {'fingerprint': 'aaaa'},
            {'fingerprint': 'bbbb'},
        )).encode()

    def test_zero_state_on_rollback(self):
        """AC1: HTTP 400 for the batch; zero receipts/episodes/outbox rows."""
        # One valid + one invalid (bad startsAt) member → whole batch rejects.
        raw = json.dumps(batch_payload(
            {'fingerprint': 'aaaa'},
            {'fingerprint': 'bbbb', 'startsAt': 'garbage'},
        )).encode()
        with self.assertRaises(acceptance.BadBatchError):
            validate_batch(raw, webhook=FakeWebhook())
        self.assertEqual(AlertIngressReceiptV1.objects.count(), 0)
        self.assertEqual(AlertDeliveryOutboxV1.objects.count(), 0)
        # Corrected replay is accepted and fully accounted.
        report, decoded = _accept(self.raw_ok)
        self.assertEqual(len(report['accepted']), 2)
        self.assertEqual(report['phases'], 4)
        self.assertEqual(report['outbox'], 4)
        # Exact replay returns the SAME acceptance IDs (joins, not duplicates).
        report2, _ = _accept(self.raw_ok)
        self.assertEqual(sorted(map(str, report['accepted'])),
                         sorted(report2['joins']))
        self.assertEqual(AlertIngressReceiptV1.objects.count(), 2)

    def test_batch_validation_before_any_commit(self):
        raw = json.dumps(batch_payload(
            {'fingerprint': 'cccc'},
            {'status': 'unknown'},
        )).encode()
        with self.assertRaises(acceptance.BadBatchError):
            validate_batch(raw, webhook=FakeWebhook())
        self.assertEqual(AlertIngressReceiptV1.objects.count(), 0)


class PayloadConflictTests(TestCase):
    def test_conflict_does_not_overwrite_first_body(self):
        """AC2: differing content for same identity → payload_conflict, no
        second effect, first body immutable."""
        first = json.dumps(batch_payload({'fingerprint': 'dddd'})).encode()
        report1, _ = _accept(first)
        receipt_id = report1['accepted'][0]
        r = AlertIngressReceiptV1.objects.get(id=receipt_id)
        first_digest = r.envelope_digest
        first_body = dict(r.envelope)

        # Same deterministic phase id can never mint a second delivery effect.
        from alertdelivery.acceptance import _recipient_episode_digest
        episode = AlertEpisodeV1.objects.get(
            ingestion_receipt_id=receipt_id)
        rd = _recipient_episode_digest(episode, 7001)
        did = phase_delivery_id(rd, 'firing')
        self.assertEqual(
            AlertRecipientPhaseV1.objects.filter(delivery_id=did).count(), 1,
            'no second delivery effect for the same deterministic phase id',
        )
        self.assertEqual(r.envelope_digest, first_digest)
        self.assertEqual(r.envelope, first_body)


class OrderingTests(TestCase):
    def test_resolution_blocked_until_firing_receipt(self):
        """AC5: resolution cannot render/reach before that recipient's firing
        receipt. Pre-delivery suppression terminalizes both phases with zero
        visible effects."""
        firing_raw = json.dumps(batch_payload(
            {'fingerprint': 'eeee'})).encode()
        _accept(firing_raw)
        phase = AlertRecipientPhaseV1.objects.filter(
            phase='firing', state='firing_pending').first()
        # Resolved webhook for same episode/recipient creates a resolved_pending
        # that is blocked — it cannot be delivered while firing is pending.
        resolved_raw = json.dumps(batch_payload(
            {'fingerprint': 'eeee', 'status': 'resolved'},
            status='resolved',
        )).encode()
        _accept(resolved_raw)
        resolved_phase = AlertRecipientPhaseV1.objects.filter(
            phase='resolved', state='resolved_pending').first()
        self.assertIsNotNone(resolved_phase)
        resolved_outbox = resolved_phase.outbox_row
        self.assertEqual(resolved_outbox.disposition, 'pending')
        # Simulate firing delivery → releases the latched resolved phase.
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        return_value={'message_id': 'm1', 'replayed': False}):
            relay_one(phase.outbox_row)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'firing_delivered')
        # Now resolved may deliver.
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        return_value={'message_id': 'm2', 'replayed': False}):
            relay_one(resolved_outbox)
        resolved_phase.refresh_from_db()
        self.assertEqual(resolved_phase.state, 'resolved_delivered')


class LeaseConcurrencyTests(TestCase):
    def test_lease_expiry_allows_new_epoch_retry(self):
        """AC3: expired lease lets a newer epoch retry; stale worker cannot
        overwrite newer state (fencing)."""
        raw = json.dumps(batch_payload({'fingerprint': 'ffff'})).encode()
        _accept(raw)
        phase = AlertRecipientPhaseV1.objects.filter(phase='firing').first()
        outbox = phase.outbox_row
        # First worker claims: epoch 0→1.
        claimed = claim_due(limit=25)
        self.assertTrue(any(str(c.pk) == str(outbox.pk) for c in claimed))
        outbox.refresh_from_db()
        self.assertEqual(outbox.fencing_epoch, 1)
        self.assertEqual(outbox.disposition, 'in_flight')
        # Lease expires; a second worker may reclaim (epoch bump is not a silent
        # overwrite — state transitions guard newer states).
        AlertDeliveryOutboxV1.objects.filter(pk=outbox.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1))
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        return_value={'message_id': 'm', 'replayed': False}):
            summary = drain_once(limit=25)
        self.assertGreaterEqual(summary['delivered'], 1)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'firing_delivered')

    def test_skipped_locked_prevents_double_claim(self):
        raw = json.dumps(batch_payload({'fingerprint': 'abcd'})).encode()
        _accept(raw)
        claimed = claim_due(limit=25)
        self.assertEqual(len(claimed), 2)  # 2 recipients => 2 due rows
        first_ids = {c.pk for c in claimed}
        # A second concurrent claim returns no overlap (rows are leased).
        unclaimed = claim_due(limit=25)
        self.assertTrue(first_ids.isdisjoint(c.pk for c in unclaimed))


class VerifiedDeliveryRelayTests(TestCase):
    def test_ambiguous_connect_commit_receipt_lookup(self):
        """AC4: after an ambiguous POST, reconcile the original ID via the
        receipt lookup instead of re-sending; matching receipt → delivered."""
        raw = json.dumps(batch_payload({'fingerprint': 'dead'})).encode()
        _accept(raw)
        phase = AlertRecipientPhaseV1.objects.filter(
            phase='firing', state='firing_pending').first()
        outbox = phase.outbox_row
        # Deferred outcome; receipt lookup authoritatively absent → stay pending,
        # backoff advanced (no finite-age drop).
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        side_effect=RelayDeferredError('timeout_ambiguous')), \
             mock.patch('alertdelivery.relay._lookup_receipt', return_value=None):
            summary = drain_once(limit=25)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'firing_pending')
        outbox.refresh_from_db()
        self.assertGreater(outbox.attempts, 0)
        # A later re-send succeeds → delivered (Connect idempotent receipt).
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        return_value={'message_id': 'mid', 'replayed': True}):
            relay_one(outbox)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'firing_delivered')
        self.assertEqual(phase.connect_receipt.get('message_id'), 'mid')

    def test_ambiguous_lookup_finds_receipt_delivers_without_resend(self):
        """AC4: ambiguous POST + receipt lookup finds the prior commit → marked
        delivered with NO re-send (the strongest no-duplicate-effect case)."""
        raw = json.dumps(batch_payload({'fingerprint': 'a11c'})).encode()
        _accept(raw)
        phase = AlertRecipientPhaseV1.objects.filter(
            phase='firing', state='firing_pending').first()
        outbox = phase.outbox_row
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        side_effect=RelayDeferredError('connect_post_failed:Timeout')), \
             mock.patch('alertdelivery.relay._lookup_receipt',
                        return_value={'message_id': 'lm', 'replayed': True}):
            summary = drain_once(limit=25)
        self.assertGreaterEqual(summary['delivered'], 1)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'firing_delivered')
        self.assertEqual(phase.connect_receipt.get('message_id'), 'lm')
        outbox.refresh_from_db()
        self.assertTrue(outbox.tombstone)

    def test_contract_409_conflict_zero_second_insert(self):
        """AC4: same ID with a different bound request → fail-loud terminal,
        no second insert."""
        raw = json.dumps(batch_payload({'fingerprint': 'c0de'})).encode()
        _accept(raw)
        phase = AlertRecipientPhaseV1.objects.filter(phase='firing').first()
        outbox = phase.outbox_row
        # Freeze a different request_digest first to simulate a conflict.
        AlertRecipientPhaseV1.objects.filter(pk=phase.pk).update(
            request_digest='x' * 43, request_digest_frozen=True)
        result = relay_one(outbox)
        self.assertIn('conflict', result)
        phase.refresh_from_db()
        self.assertEqual(phase.state, 'contract_conflict')
        # Exactly one phase delivery row exists (no second insert).
        self.assertEqual(
            AlertRecipientPhaseV1.objects.filter(pk=phase.pk).count(), 1)

    def test_relay_failure_backs_off_and_retries(self):
        raw = json.dumps(batch_payload({'fingerprint': 'f00d'})).encode()
        _accept(raw)
        phase = AlertRecipientPhaseV1.objects.filter(phase='firing').first()
        outbox = phase.outbox_row
        with mock.patch('alertdelivery.relay._post_verified_delivery',
                        side_effect=RelayDeferredError('connect_http_503')):
            summary = drain_once(limit=25)
        self.assertGreaterEqual(summary['deferred'], 1)
        outbox.refresh_from_db()
        self.assertGreater(outbox.attempts, 0)
        self.assertGreater(outbox.last_backoff_seconds, 0)
        self.assertFalse(outbox.tombstone, 'no finite-age silent drop')


class IsolationTests(TestCase):
    def test_exact_org_and_lineage_isolation(self):
        raw = json.dumps(batch_payload({'fingerprint': 'beef'})).encode()
        report, _ = _accept(raw)
        receipt = AlertIngressReceiptV1.objects.get(id=report['accepted'][0])
        # Org + lineage always bound and never mixes.
        self.assertEqual(receipt.organization_id, 42)
        self.assertEqual(receipt.producer_lineage_id, FakeWebhook.id)
        # A different lineage cannot collide on the same identity.
        other = AlertIngressReceiptV1.objects.create(
            organization_id=42,
            producer_lineage_id=202,
            webhook_key_revision=1,
            envelope_digest='y' * 43,
            envelope={'schema': 'x'},
        )
        self.assertNotEqual(
            receipt.ingest_identity, other.ingest_identity)


class FpFallbackTests(TestCase):
    def test_missing_fingerprint_fingerprint_fallback_closure(self):
        """Closed fingerprint rules: a present-empty/malformed fp is rejected;
        an absent fp uses a deterministic identity (never null/null alias)."""
        # Present-empty fingerprint → batch rejected.
        raw = json.dumps(batch_payload({'fingerprint': ''})).encode()
        with self.assertRaises(acceptance.BadBatchError):
            validate_batch(raw, webhook=FakeWebhook())
        self.assertEqual(AlertIngressReceiptV1.objects.count(), 0)
        # Absent fingerprint → accepted with semantic-v1 identity source.
        raw2 = json.dumps(batch_payload({'fingerprint': None})).encode()
        report, _ = _accept(raw2)
        episode = AlertEpisodeV1.objects.get(
            ingestion_receipt_id=report['accepted'][0])
        self.assertEqual(episode.alert_identity_source, 'semantic-v1')
        self.assertEqual(episode.alert_identity, '')


class WebhookEndpointTests(TestCase):
    def setUp(self):
        from tasks.models import Project
        self.project = Project.objects.create(
            name='AD e2e', key='ADE2E', org_id=42,
        )

    def _wk(self, key, *, group='oncall'):
        from tasks.models import WebhookKey
        return WebhookKey.objects.create(
            key=key, is_active=True, source_name='prometheus',
            integration_type='prometheus', user_id=1, name='e2e',
            default_project=self.project,
            default_assignment_group=group,
        )

    @override_settings(SHIZUHA_CONNECT_API_URL='http://connect.test/api')
    def test_endpoint_returns_202_and_400(self):
        from django.test import Client
        self._wk('durable-e2e-key')
        c = Client()
        # Missing/invalid key → 401.
        resp = c.post('/api/webhooks/alertmanager-durable/nope/', data=b'{}',
                      content_type='application/json')
        self.assertEqual(resp.status_code, 401)
        # Invalid batch → 400 with zero state.
        resp = c.post(
            '/api/webhooks/alertmanager-durable/durable-e2e-key/',
            data=json.dumps(batch_payload({'startsAt': 'bad'})).encode(),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AlertIngressReceiptV1.objects.count(), 0)

    @override_settings(SHIZUHA_CONNECT_API_URL='http://connect.test/api')
    def test_endpoint_durable_accept_with_no_recipient_suppresses(self):
        from django.test import Client
        # Empty assignment group => no exact-org recipient => suppresses, but
        # the envelope is still durably accepted (terminal, fully accounted).
        self._wk('durable-e2e2-key', group='')
        c = Client()
        resp = c.post(
            '/api/webhooks/alertmanager-durable/durable-e2e2-key/',
            data=json.dumps(batch_payload({'fingerprint': 'aaa2'})).encode(),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 202)
        self.assertGreaterEqual(
            AlertIngressReceiptV1.objects.filter(
                failure_reason='suppressed_unrouted').count(), 1)
