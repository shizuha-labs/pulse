"""PLAT-5928 durable Alertmanager acceptance + VerifiedAlertDelivery (slice 3).

Postgres is the single authority for pending/terminal truth, attempt/backoff,
short SKIP LOCKED leases, fencing epochs, tombstones and accepted→terminal
telemetry. Broker publish and Redis are wake/lookup accelerators only.

Invariant snapshot (HLD §8.4.2A):
- ``AlertIngressReceiptV1``: one per validated AlertEnvelopeV1; deterministic
  ingest identity over (schema-domain, organization_id, producer_lineage_id,
  envelope_digest). Exact replay joins the same row; a differing envelope is
  recorded as a conflict and never overwrites the first body.
- ``AlertEpisodeV1``: per (organization_id, producer_lineage_id, identity,
  starts_at_seconds, starts_at_nanos). Status is not part of the episode key.
- ``AlertRecipientPhaseV1``: per (recipient episode, firing|resolved).
  Monotonic firing_pending → firing_delivered → resolved_pending →
  resolved_delivered, with bounded terminal suppressions.
- ``AlertDeliveryOutboxV1``: the transactional outbox row the relay drains.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class ProducerLineageV1(models.Model):
    """Immutable producer lineage tuple (write-once, never-reused lineage id).

    At initial key creation one transaction fixes
    ``(producer_lineage_id, organization_id, producer_kind, source_name)``;
    later non-lineage configuration may only create monotonic revisions under
    the same lineage id. An update that changes org/producer/source identity
    rejects atomically.
    """

    producer_lineage_id = models.PositiveBigIntegerField(
        primary_key=True, editable=False,
        help_text='Positive, never-reused lineage id — in V1 the webhook_key_id.',
    )
    organization_id = models.PositiveBigIntegerField()
    producer_kind = models.CharField(max_length=32, default='alertmanager')
    logical_upstream_identity = models.CharField(max_length=255, default='')
    source_name = models.CharField(max_length=255, default='')
    reviewed_ha_replica_set = models.CharField(max_length=255, default='', blank=True)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'alertdelivery_producer_lineage_v1'
        constraints = [
            models.UniqueConstraint(
                fields=['producer_lineage_id'],
                name='uniq_alertdelivery_producer_lineage',
            ),
        ]


class AlertIngressReceiptV1(models.Model):
    """Durable, deterministic acceptance receipt for one validated envelope."""

    STATE_CHOICES = [
        ('pending', 'pending'),
        ('delivering', 'delivering'),
        ('terminal', 'terminal'),
    ]
    OUTCOME_CHOICES = [
        ('', ''),
        ('accepted', 'accepted'),
        ('payload_conflict', 'payload_conflict'),
        ('terminal_delivered_all', 'terminal_delivered_all'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingest_identity = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text='b64url sha256 over the literal FRAME(INGRESS_DOMAIN, JCS).',
    )
    organization_id = models.PositiveBigIntegerField(db_index=True)
    producer_lineage_id = models.PositiveBigIntegerField(db_index=True)
    webhook_key_revision = models.PositiveBigIntegerField(default=1)
    envelope_digest = models.CharField(max_length=43, blank=True, default='')
    envelope = models.JSONField(default=dict)
    failure_reason = models.CharField(max_length=64, blank=True, default='')
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default='pending', db_index=True)
    outcome = models.CharField(max_length=40, choices=OUTCOME_CHOICES, blank=True, default='')
    accepted_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    fencing_epoch = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'alertdelivery_ingress_receipt_v1'
        indexes = [
            models.Index(fields=['state', 'next_attempt_at'], name='adr_receipt_due_idx'),
        ]


class AlertEpisodeV1(models.Model):
    """Immutable per-organization episode for one alert identity + start."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    episode_digest = models.CharField(max_length=64, unique=True, db_index=True)
    organization_id = models.PositiveBigIntegerField(db_index=True)
    producer_lineage_id = models.PositiveBigIntegerField(db_index=True)
    alert_identity_source = models.CharField(
        max_length=16, default='upstream',
        help_text='upstream | semantic-v1',
    )
    alert_identity = models.CharField(max_length=64, blank=True, default='')
    starts_at_seconds = models.PositiveBigIntegerField()
    starts_at_nanos = models.PositiveIntegerField(default=0)
    ingestion_receipt = models.ForeignKey(
        'AlertIngressReceiptV1', on_delete=models.PROTECT, related_name='episodes',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'alertdelivery_episode_v1'
        constraints = [
            models.UniqueConstraint(
                fields=['organization_id', 'producer_lineage_id',
                        'alert_identity_source', 'alert_identity',
                        'starts_at_seconds', 'starts_at_nanos'],
                name='uniq_alertdelivery_episode',
            ),
        ]


class AlertRecipientPhaseV1(models.Model):
    """Per-recipient monotonic phase for one episode (exact-org, frozen)."""

    PHASE_CHOICES = [
        ('firing_pending', 'firing_pending'),
        ('firing_delivered', 'firing_delivered'),
        ('resolved_pending', 'resolved_pending'),
        ('resolved_delivered', 'resolved_delivered'),
        ('firing_suppressed_pre_delivery', 'firing_suppressed_pre_delivery'),
        ('resolved_suppressed_no_firing_effect', 'resolved_suppressed_no_firing_effect'),
        ('resolved_suppressed_ineligible', 'resolved_suppressed_ineligible'),
        ('ineligible', 'ineligible'),
        ('unrouted', 'unrouted'),
        ('identity_invalid', 'identity_invalid'),
        ('orphan_resolved', 'orphan_resolved'),
        ('phase_regression', 'phase_regression'),
        ('contract_conflict', 'contract_conflict'),
    ]
    TERMINAL_SUPPRESSED = {
        'firing_suppressed_pre_delivery', 'resolved_suppressed_no_firing_effect',
        'resolved_suppressed_ineligible', 'ineligible', 'unrouted',
        'identity_invalid', 'orphan_resolved', 'phase_regression',
        'contract_conflict',
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    episode = models.ForeignKey(
        'AlertEpisodeV1', on_delete=models.CASCADE, related_name='recipient_phases',
    )
    recipient_id = models.PositiveBigIntegerField(db_index=True)
    recipient_episode_digest = models.CharField(max_length=64, db_index=True)
    phase = models.CharField(max_length=13, db_index=True)  # 'firing'|'resolved'
    state = models.CharField(
        max_length=48, choices=PHASE_CHOICES, default='firing_pending', db_index=True,
    )
    delivery_id = models.CharField(max_length=40, unique=True, db_index=True)
    client_message_id = models.CharField(max_length=64, unique=True, db_index=True)
    request_digest = models.CharField(max_length=43, blank=True, default='')
    request_digest_frozen = models.BooleanField(default=False)
    firing_delivered_at = models.DateTimeField(null=True, blank=True)
    resolved_delivered_at = models.DateTimeField(null=True, blank=True)
    terminal_reason = models.CharField(max_length=64, blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    fencing_epoch = models.PositiveBigIntegerField(default=0)
    connect_receipt = models.JSONField(default=dict, blank=True)
    organization_id = models.PositiveBigIntegerField()
    team = models.CharField(max_length=128, default='', blank=True)
    delivery_lane = models.CharField(max_length=64, default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'alertdelivery_recipient_phase_v1'
        constraints = [
            models.UniqueConstraint(
                fields=['episode', 'recipient_id', 'phase'],
                name='uniq_alertdelivery_recipient_phase',
            ),
        ]
        indexes = [
            models.Index(fields=['state', 'next_attempt_at'], name='adr_phase_due_idx'),
            models.Index(fields=['episode', 'recipient_id'], name='adr_episode_recipient_idx'),
        ]


class AlertDeliveryOutboxV1(models.Model):
    """Transactional outbox row: one due attempt per recipient phase."""

    DISPOSITION_CHOICES = [
        ('pending', 'Pending'),
        ('in_flight', 'In Flight'),
        ('term', 'Terminal'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phase = models.OneToOneField(
        'AlertRecipientPhaseV1', on_delete=models.CASCADE, related_name='outbox_row',
    )
    disposition = models.CharField(
        max_length=16, choices=DISPOSITION_CHOICES, default='pending', db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    in_flight_until = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    fencing_epoch = models.PositiveBigIntegerField(default=0)
    tombstone = models.BooleanField(default=False, db_index=True)
    request_digest = models.CharField(max_length=43, blank=True, default='')
    terminal_outcome = models.CharField(max_length=40, blank=True, default='')
    last_backoff_seconds = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'alertdelivery_delivery_outbox_v1'
        indexes = [
            models.Index(
                fields=['disposition', 'tombstone', 'next_attempt_at'],
                name='adr_outbox_due_idx',
            ),
        ]
