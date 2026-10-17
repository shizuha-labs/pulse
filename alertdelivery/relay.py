"""PLAT-5928 — Postgres-authoritative fenced leased relay (AC3/AC4/AC5).

Drains the ``AlertDeliveryOutboxV1`` due rows with short ``SKIP LOCKED``
leases and fencing epochs, constructs the closed per-recipient
``VerifiedAlertDelivery`` packet for the deployed Connect destination, POSTs it
with a deterministic ``client_message_id``, reconciles ambiguous results via the
Connect receipt lookup, and advances the per-recipient phase state machine with
exact firing→resolved ordering.

Postgres is authoritative for pending/terminal truth, attempt/backoff, leases,
fencing, tombstones and accepted→terminal telemetry. Redis/broker are only
wake/lookup accelerators. There is no finite-age silent drop: transient
failures back off with capped exponential + jitter indefinitely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AlertDeliveryOutboxV1,
    AlertRecipientPhaseV1,
)

logger = logging.getLogger(__name__)

MAX_BACKOFF = 300
BASE_BACKOFF = 2
LEASE_SECONDS = int(os.environ.get('PULSE_ALERT_LEASE_SECONDS', '20'))


def _backoff(attempts: int) -> int:
    return min(MAX_BACKOFF, BASE_BACKOFF * (2 ** max(0, attempts - 1)))


def _jitter(seconds: int) -> int:
    return int(seconds * (0.5 + random.random()))


def _request_digest(packet: dict) -> str:
    from .ingest import _canonical_bytes
    try:
        return hashlib.sha256(_canonical_bytes(packet)).hexdigest()[:43]
    except Exception:
        return hashlib.sha256(
            json.dumps(packet, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()[:43]


def _connect_base_url():
    from django.conf import settings
    return str(getattr(
        settings, 'SHIZUHA_CONNECT_API_URL',
        'http://shizuha-connect:8019/api',
    )).rstrip('/')


def _verified_delivery_packet(phase: AlertRecipientPhaseV1, episode) -> dict:
    """Closed per-recipient VerifiedAlertDelivery for the Connect destination."""
    firing = phase.phase == 'firing'
    return {
        'schema': 'shizuha.verified-alert-delivery.v1',
        'sender_service': 'shizuha-pulse',
        'organization_id': phase.organization_id,
        'recipient_user_id': phase.recipient_id,
        'client_message_id': phase.client_message_id,
        'episode_digest': phase.recipient_episode_digest,
        'phase': phase.phase,
        'firing_receipt_required': True,
        'content_type': 'alert_phase',
        'alert': {
            'fingerprint': episode.alert_identity or '',
            'starts_at': getattr(episode, 'starts_at_text', ''),
            'status': 'firing' if firing else 'resolved',
        },
    }


def _lookup_receipt(phase: AlertRecipientPhaseV1) -> dict | None:
    """Authenticated read-only Connect receipt lookup by (sender, client_msg_id).

    Returns the opaque matching receipt dict or ``None`` when authoritatively
    absent; raises ``RelayDeferredError`` when the lookup itself is unavailable
    (remain pending; never guess).
    """
    import requests  # local import keeps module import side-effect free
    url = f'{_connect_base_url()}/internal/verified-delivery/receipt/'
    try:
        resp = requests.get(
            url,
            params={
                'sender_service': 'shizuha-pulse',
                'client_message_id': phase.client_message_id,
            },
            timeout=8,
            headers={'X-Internal-Service': 'shizuha-pulse'},
        )
    except Exception as exc:
        raise RelayDeferredError(
            f'receipt_lookup_unavailable:{type(exc).__name__}') from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise RelayDeferredError(f'receipt_lookup_http_{resp.status_code}')


class RelayDeferredError(Exception):
    """A transient/ambiguous failure — the attempt remains pending and retries."""


class ContractConflictError(Exception):
    """A proven schema/request-ID conflict — fail-loud terminal."""


def _post_verified_delivery(phase: AlertRecipientPhaseV1, episode) -> dict:
    """POST the closed packet; reconcile ambiguous outcomes via receipt lookup.

    Returns the Connect receipt dict. Raises ``RelayDeferredError`` for
    transient 429/5xx/timeout/absence-unknown; ``ContractConflictError`` for a
    proven same-ID different-body conflict (zero second insert).
    """
    import requests
    packet = _verified_delivery_packet(phase, episode)
    url = f'{_connect_base_url()}/internal/system-message/'
    digest = _request_digest(packet)
    with transaction.atomic():
        locked = AlertRecipientPhaseV1.objects.select_for_update().get(pk=phase.pk)
        if not locked.request_digest_frozen:
            locked.request_digest = digest
            locked.request_digest_frozen = True
            locked.save(update_fields=['request_digest', 'request_digest_frozen'])
        elif locked.request_digest and locked.request_digest != digest:
            raise ContractConflictError('request_digest_mismatch')
    try:
        resp = requests.post(
            url,
            json=packet,
            timeout=8,
            headers={'X-Internal-Service': 'shizuha-pulse'},
        )
    except Exception as exc:
        raise RelayDeferredError(
            f'connect_post_failed:{type(exc).__name__}') from exc

    if resp.status_code in (201, 200):
        body = resp.json()
        return body if isinstance(body, dict) else {}
    if resp.status_code == 409:
        # Same ID, different bound content — fail-loud terminal conflict.
        raise ContractConflictError('connect_409_conflict')
    if resp.status_code in (404, 410):
        return {}  # authoritative absence — caller may replace under same ID
    raise RelayDeferredError(f'connect_http_{resp.status_code}')


def _advance_after_receipt(phase: AlertRecipientPhaseV1):
    """Apply a durable firing receipt: firing_pending → firing_delivered, and
    release a latched resolved_pending → eligible for its own delivery."""
    now = timezone.now()
    if phase.phase == 'firing' and phase.state == 'firing_pending':
        phase.state = 'firing_delivered'
        phase.firing_delivered_at = now
        phase.save(update_fields=['state', 'firing_delivered_at', 'updated_at'])
    elif phase.phase == 'resolved' and phase.state == 'resolved_pending':
        phase.state = 'resolved_delivered'
        phase.resolved_delivered_at = now
        phase.save(update_fields=['state', 'resolved_delivered_at', 'updated_at'])
    return phase


def _terminalize(row: AlertDeliveryOutboxV1, outcome: str, err: str):
    row.disposition = 'term'
    row.tombstone = True
    row.terminal_outcome = outcome
    if err:
        row.last_error = err[:200]
    row.save(update_fields=['disposition', 'tombstone', 'terminal_outcome',
                            'last_error', 'updated_at'])


def _reconcile_ambiguous(phase: AlertRecipientPhaseV1) -> dict | None:
    """§8.4.2A: after an ambiguous POST, FIRST reconcile the original ID via the
    authenticated receipt lookup before any re-send.

    - matching committed receipt → return it (delivered, no new effect);
    - authoritative absence (None) → caller may re-send under the same ID;
    - lookup unavailable → raises ``RelayDeferredError`` (stay pending, never
      guess).
    """
    return _lookup_receipt(phase)


def relay_one(outbox: AlertDeliveryOutboxV1) -> dict:
    """Process a single leased outbox row (transactional). Returns an outcome.

    Outcomes: ``{'delivered': state}``, ``{'deferred': reason}`` (persisted
    backoff), ``{'conflict': reason}`` (persisted terminal) or
    ``{'error': cls}``. Deferred/conflict saves are committed by the outer
    transaction because we RETURN rather than re-raise through ``atomic()``.
    """
    with transaction.atomic():
        row = AlertDeliveryOutboxV1.objects.select_for_update().get(pk=outbox.pk)
        if row.tombstone or row.disposition == 'term':
            return {'skipped': 'terminal'}
        phase = AlertRecipientPhaseV1.objects.select_for_update().get(pk=row.phase_id)
        episode = phase.episode
        try:
            receipt = _post_verified_delivery(phase, episode)
        except ContractConflictError as exc:
            # Fail-loud terminal: a proven same-ID conflict cannot be redriven
            # under a new identity. Terminalize the phase lineage too.
            phase.state = 'contract_conflict'
            phase.terminal_reason = 'contract_conflict'
            phase.save(update_fields=['state', 'terminal_reason', 'updated_at'])
            _terminalize(row, 'contract_conflict', str(exc))
            return {'conflict': str(exc)}
        except RelayDeferredError as exc:
            # Ambiguous outcome after an explicit POST attempt: FIRST reconcile
            # via the receipt lookup before deciding to retry. A matching receipt
            # proves a prior commit already persisted — mark delivered with NO
            # new effect. Authoritative absence / unavailable lookup → persist
            # backoff and stay pending for a later re-send under the same ID.
            try:
                reconciled = _reconcile_ambiguous(phase)
            except RelayDeferredError:
                reconciled = None
            if reconciled:
                phase.connect_receipt = {
                    'message_id': reconciled.get('message_id', ''),
                    'client_message_id': reconciled.get(
                        'client_message_id', phase.client_message_id),
                    'replayed': reconciled.get('replayed', False),
                }
                phase.save(update_fields=['connect_receipt', 'updated_at'])
                _advance_after_receipt(phase)
                _terminalize(row, 'delivered', '')
                return {'delivered': phase.state}
            row.attempts += 1
            row.last_backoff_seconds = _backoff(row.attempts)
            row.next_attempt_at = timezone.now() + timedelta(
                seconds=_jitter(row.last_backoff_seconds))
            row.last_error = str(exc)[:200]
            row.save(update_fields=[
                'attempts', 'next_attempt_at', 'last_backoff_seconds',
                'last_error', 'updated_at',
            ])
            return {'deferred': str(exc)}
        # Success: record Connect receipt + advance phase monotonic state.
        phase.connect_receipt = {
            'message_id': receipt.get('message_id', ''),
            'client_message_id': receipt.get('client_message_id', phase.client_message_id),
            'replayed': receipt.get('replayed', False),
        }
        phase.save(update_fields=['connect_receipt', 'updated_at'])
        _advance_after_receipt(phase)
        outcome = (
            'delivered'
            if phase.state in ('firing_delivered', 'resolved_delivered')
            else 'pending'
        )
        _terminalize(row, outcome, '')
        return {'delivered': phase.state}


def claim_due(limit: int = 25) -> list[AlertDeliveryOutboxV1]:
    """Claim due outbox rows with SKIP LOCKED + fencing epoch bump.

    Short leases (``lease_until``) mean an **expired** lease lets another
    worker reclaim (AC3); the fencing epoch increment prevents a stale worker
    from overwriting newer state. Rows are due when pending, or when their
    lease has expired (regardless of current in_flight disposition).
    """
    now = timezone.now()
    lease_until = now + timedelta(seconds=LEASE_SECONDS)
    due = (
        Q(disposition='pending') & Q(tombstone=False) & Q(next_attempt_at__lte=now)
    ) | (
        Q(disposition='in_flight') & Q(tombstone=False) &
        Q(lease_until__lte=now) & Q(next_attempt_at__lte=now)
    )
    rows = list(
        AlertDeliveryOutboxV1.objects.filter(due)
        .exclude(lease_until__gt=now)
        .order_by('next_attempt_at')[:limit]
    )
    claimed = []
    for row in rows:
        with transaction.atomic():
            locked = (
                AlertDeliveryOutboxV1.objects.select_for_update(
                    skip_locked=True,
                ).filter(pk=row.pk).filter(due).exclude(lease_until__gt=now).first()
            )
            if locked is None:
                continue
            epoch = locked.fencing_epoch
            AlertDeliveryOutboxV1.objects.filter(pk=locked.pk).update(
                disposition='in_flight',
                lease_until=lease_until,
                in_flight_until=lease_until,
                fencing_epoch=epoch + 1,
                updated_at=now,
            )
            locked.disposition = 'in_flight'
            locked.lease_until = lease_until
            locked.fencing_epoch = epoch + 1
            claimed.append(locked)
    return claimed


def drain_once(limit: int = 25) -> dict:
    """One relay pass: claim due rows and process each (outcomes are counted)."""
    summary = {'claimed': 0, 'delivered': 0, 'deferred': 0, 'conflict': 0, 'errors': 0}
    claimed = claim_due(limit)
    summary['claimed'] = len(claimed)
    for row in claimed:
        try:
            out = relay_one(row)
            if out.get('delivered'):
                summary['delivered'] += 1
            elif out.get('deferred'):
                summary['deferred'] += 1
            elif out.get('conflict'):
                summary['conflict'] += 1
        except Exception as exc:  # noqa: BLE001 — bounded telemetry
            summary['errors'] += 1
            logger.warning('alert relay row error %s', type(exc).__name__)
    return summary
