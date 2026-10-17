"""PLAT-5928 — strict batch acceptance (AC1/AC2) + durable episode/phase join.

The webhook entry performs strict parse + full-batch validation BEFORE any
transaction. Only an all-valid batch enters :func:`accept_batch`, one Postgres
transaction that durably:

- creates/joins one ``AlertIngressReceiptV1`` per validated envelope
  (replay joins the same receipt; a differing envelope is recorded
  ``payload_conflict`` and never overwrites the first body);
- creates/joins one ``AlertEpisodeV1`` per closed episode key;
- creates ``AlertRecipientPhaseV1`` rows for the plan-time authorized
  recipients, plus one due ``AlertDeliveryOutboxV1`` per phase.

A commit failure returns 503 and leaves zero accepted state; the caller must
map non-2xx appropriately. Exact replay returns the same acceptance IDs.
"""
from __future__ import annotations

import uuid
from hashlib import sha256
from typing import Any

from django.db import transaction
from django.utils import timezone

from .ingest import (
    _canonical_bytes,
    build_envelope,
    envelope_digest,
    ingest_identity,
    normalize_starts_at,
    strict_json_loads,
)
from .models import (
    AlertDeliveryOutboxV1,
    AlertEpisodeV1,
    AlertIngressReceiptV1,
    AlertRecipientPhaseV1,
)


def phase_delivery_id(recipient_episode_digest: str, phase: str) -> str:
    """Deterministic namespaced UUID over (recipient episode digest, phase)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'{recipient_episode_digest}|{phase}'))


def phase_client_message_id(recipient_episode_digest: str, phase: str) -> str:
    """Deterministic client_message_id; stable across lease/worker/retry."""
    raw = sha256(f'{recipient_episode_digest}|{phase}'.encode('utf-8')).hexdigest()
    return f'pulse-alert-{raw}'[:64]


class BadBatchError(ValueError):
    """Batch-wide rejection (HTTP 400). Carries a bounded reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DecodedEnvelope:
    """One validated AlertEnvelopeV1 with derived numbers ready for persist."""

    __slots__ = (
        'envelope', 'envelope_digest', 'org_id', 'producer_lineage_id',
        'fingerprint', 'starts_at_seconds', 'starts_at_nanos', 'starts_at_text',
        'status', 'severity',
    )

    def __init__(self, envelope, fingerprint, starts_at_seconds, starts_at_nanos,
                 starts_at_text, status, severity, envelope_digest):
        self.envelope = envelope
        self.envelope_digest = envelope_digest
        self.org_id = int(envelope['authority']['organization_id'])
        self.producer_lineage_id = int(envelope['source']['webhook_key_id'])
        self.fingerprint = fingerprint
        self.starts_at_seconds = starts_at_seconds
        self.starts_at_nanos = starts_at_nanos
        self.starts_at_text = starts_at_text
        self.status = status
        self.severity = severity


def validate_batch(raw: bytes, *, webhook) -> list[DecodedEnvelope]:
    """Strict-decode + validate the whole batch. Returns decoded envelopes.

    Raises ``BadBatchError`` on ANY member failure (batch-wide 400). Neither
    this function nor its caller commits any state before the whole batch is
    valid.
    """
    try:
        obj = strict_json_loads(raw)
    except Exception:
        from .ingest import StrictDecodeError as _S  # noqa: F401
        raise BadBatchError('wire_invalid') from None
    if not isinstance(obj, dict):
        raise BadBatchError('batch_not_object')
    alerts = obj.get('alerts')
    if not isinstance(alerts, list) or not alerts:
        raise BadBatchError('no_valid_alerts')
    group_status = str(obj.get('status') or '').strip().lower()

    org_id = getattr(webhook, '_effective_organization_id', None) or 0
    if not org_id:
        raise BadBatchError('webhook_org_unbound')

    lineage_id = int(webhook.id)  # webhook_key_id is the producer lineage in V1
    source_name = getattr(webhook, 'source_name', '') or ''
    revision = int(getattr(webhook, 'webhook_key_revision', 1) or 1)
    project_ref = getattr(webhook, 'default_project', None)
    project_id = getattr(project_ref, 'id', None)
    project_key = getattr(project_ref, 'item_key', None) or getattr(
        project_ref, 'key', None)

    out = []
    for alert in alerts:
        if not isinstance(alert, dict):
            raise BadBatchError('alert_not_object')
        labels = alert.get('labels')
        annotations = alert.get('annotations')
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise BadBatchError('label_annotation_map_invalid')
        for k, v in {**labels, **annotations}.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise BadBatchError('label_annotation_non_text')

        raw_fp = alert.get('fingerprint')
        if raw_fp is not None and not isinstance(raw_fp, str):
            raise BadBatchError('fingerprint_wrong_type')
        if raw_fp is not None and not raw_fp.strip():
            # Present-empty fingerprint is identity_invalid — never falls back.
            raise BadBatchError('fingerprint_present_empty')
        fingerprint = raw_fp or None
        if fingerprint is not None:
            if any(c not in '0123456789abcdefABCDEF' for c in fingerprint):
                raise BadBatchError('fingerprint_malformed')

        try:
            starts_at_seconds, starts_at_nanos, starts_at_text = normalize_starts_at(
                alert.get('startsAt'),
            )
        except Exception:
            raise BadBatchError('starts_at_invalid') from None

        status = str(alert.get('status') or group_status or '').strip().lower()
        if status not in ('firing', 'resolved'):
            raise BadBatchError('status_unknown')
        severity = str(
            labels.get('severity') or getattr(webhook, 'default_severity', 'warning')
        ).strip().lower()

        title = labels.get('alertname', 'Alertmanager Alert')
        annotations_m = annotations or {}
        description = annotations_m.get('description') or annotations_m.get('summary') or ''

        try:
            envelope = build_envelope(
                organization_id=org_id,
                source_name=source_name,
                webhook_key_id=lineage_id,
                webhook_key_revision=revision,
                project_id=project_id,
                project_key=project_key,
                labels=labels,
                annotations=annotations,
                fingerprint=fingerprint,
                starts_at=starts_at_text,
                ends_at=alert.get('endsAt'),
                status=status,
                severity=severity,
                title_input=str(title or ''),
                description_input=str(description or ''),
                source_url=alert.get('generatorURL') or None,
            )
        except Exception:
            raise BadBatchError('envelope_construct_invalid') from None
        out.append(DecodedEnvelope(
            envelope, fingerprint, starts_at_seconds, starts_at_nanos,
            starts_at_text, status, severity, envelope_digest(envelope),
        ))
    return out


def _episode_digest(env: DecodedEnvelope) -> str:
    base = {
        'schema': 'shizuha.alert-episode.v1',
        'organization_id': str(env.org_id),
        'producer_lineage_id': str(env.producer_lineage_id),
        'alert_identity_source': 'upstream' if env.fingerprint else 'semantic-v1',
        'alert_identity': env.fingerprint if env.fingerprint else '',
        'starts_at_seconds': str(env.starts_at_seconds),
        'starts_at_nanos': str(env.starts_at_nanos),
    }
    return sha256(_canonical_bytes(base)).hexdigest()


def _recipient_episode_digest(episode: AlertEpisodeV1, recipient_id: int) -> str:
    base = {
        'episode': episode.episode_digest,
        'recipient_id': str(recipient_id),
        'organization_id': str(episode.organization_id),
    }
    return sha256(_canonical_bytes(base)).hexdigest()


@transaction.atomic
def accept_batch(decoded: list[DecodedEnvelope], *, recipient_map: dict[str, Any],
                 reviewer_email: str = '') -> dict:
    """One durable Postgres transaction: create/join receipts, episodes,
    recipient phases and outbox rows. Returns a JSON-safe acceptance report.

    ``recipient_map`` keys envelopes by their ``envelope_digest``; each value is
    a list of plan-time authorized recipients
    ``{"recipient_id", "team", "delivery_lane"}``. An envelope with no
    authorized recipient yields a terminal ``suppressed_unrouted`` receipt and
    no outbox row.
    """
    now = timezone.now()
    report = {
        'accepted': [],
        'joins': [],
        'conflicts': [],
        'episodes': [],
        'phases': 0,
        'outbox': 0,
        'suppressed_unrouted': [],
    }

    for env in decoded:
        receipt, created, conflict = _join_receipt(env, now)
        if conflict:
            report['conflicts'].append(str(receipt.id))
            continue
        (report['accepted'] if created else report['joins']).append(str(receipt.id))

        episode, _ = _join_episode(env, receipt, now)
        report['episodes'].append(str(episode.id))

        recipients = recipient_map.get(env.envelope_digest, [])
        if not recipients:
            receipt.failure_reason = 'suppressed_unrouted'
            receipt.save(update_fields=['failure_reason', 'updated_at'])
            report['suppressed_unrouted'].append(str(episode.id))
            continue

        for rec in recipients:
            phase, outbox = _create_phase(episode, rec, env, now)
            report['phases'] += 1
            if outbox is not None:
                report['outbox'] += 1

    return report


def _join_receipt(env: DecodedEnvelope, now):
    identity = ingest_identity(env.org_id, env.producer_lineage_id, env.envelope_digest)
    existing = AlertIngressReceiptV1.objects.filter(
        ingest_identity=identity).first()
    if existing:
        if existing.envelope_digest != env.envelope_digest:
            return existing, False, True  # payload conflict — immutable first body
        return existing, False, False
    receipt = AlertIngressReceiptV1.objects.create(
        ingest_identity=identity,
        organization_id=env.org_id,
        producer_lineage_id=env.producer_lineage_id,
        webhook_key_revision=int(env.envelope['source']['webhook_key_revision']),
        envelope_digest=env.envelope_digest,
        envelope=env.envelope,
        state='pending',
        outcome='accepted',
        accepted_at=now,
    )
    return receipt, True, False


def _join_episode(env: DecodedEnvelope, receipt, now):
    digest = _episode_digest(env)
    existing = AlertEpisodeV1.objects.filter(episode_digest=digest).first()
    if existing:
        return existing, False
    episode = AlertEpisodeV1.objects.create(
        episode_digest=digest,
        organization_id=env.org_id,
        producer_lineage_id=env.producer_lineage_id,
        alert_identity_source='upstream' if env.fingerprint else 'semantic-v1',
        alert_identity=env.fingerprint if env.fingerprint else '',
        starts_at_seconds=env.starts_at_seconds,
        starts_at_nanos=env.starts_at_nanos,
        ingestion_receipt=receipt,
    )
    return episode, True


def _create_phase(episode: AlertEpisodeV1, rec: dict, env: DecodedEnvelope, now):
    recipient_id = int(rec['recipient_id'])
    rd = _recipient_episode_digest(episode, recipient_id)
    delivery_id = phase_delivery_id(rd, env.status)
    client_id = phase_client_message_id(rd, env.status)

    existing = AlertRecipientPhaseV1.objects.filter(delivery_id=delivery_id).first()
    if existing:
        return existing, None

    phase = AlertRecipientPhaseV1.objects.create(
        episode=episode,
        recipient_id=recipient_id,
        recipient_episode_digest=rd,
        phase=env.status,
        state='firing_pending' if env.status == 'firing' else 'resolved_pending',
        delivery_id=delivery_id,
        client_message_id=client_id,
        organization_id=episode.organization_id,
        team=str(rec.get('team') or ''),
        delivery_lane=str(rec.get('delivery_lane') or ''),
        next_attempt_at=now,
    )
    outbox = AlertDeliveryOutboxV1.objects.create(
        phase=phase,
        disposition='pending',
        next_attempt_at=now,
    )
    return phase, outbox
