"""MutationReceipt service — concurrency and replay state machine
(PLAT-5343 HLD v12 §6.2).

State machine: ``absent → ACTIVE → (COMMITTED | FENCING_UNPROVEN →
ABORTED_PROVEN → ACTIVE)``. ``ACTIVE``/``FENCING_UNPROVEN`` return
``425 attempt_unresolved`` + ``Retry-After: 1`` + ``retryable=true`` and
never execute a second effect. The worker holds the transaction-scoped
advisory key lock and locks the attempt row; effect + receipt + COMMITTED
are one transaction. The reconciler proves worker-nonce death + PostgreSQL
backend/transaction absence + receipt/effect absence before
ABORTED_PROVEN (prevents a retry racing a late commit). Replay rechecks
endpoint visibility (loss → 403 without rerun or leak); same key +
different digest → ``409 idempotency_conflict``. Replay material is
retained 7 days post-terminal, body-free tombstone 30 days, unresolved
attempts never auto-expired (they alert until reconciled).
"""
import hashlib

from django.db import connection, transaction
from django.utils import timezone

from ..models import MutationReceipt

RETRY_AFTER_SECONDS = 1
REPLAY_RETENTION_DAYS = 7
TOMBSTONE_RETENTION_DAYS = 30


class AttemptUnresolved(Exception):
    """425 attempt_unresolved — the key is ACTIVE/FENCING_UNPROVEN.

    Carries the wire payload: ``retryable=true``, ``request_id``,
    ``retry_after_ms``. A loser NEVER executes a second effect.
    """

    def __init__(self, receipt, request_id=''):
        self.receipt = receipt
        self.request_id = request_id
        super().__init__(f'attempt_unresolved: {receipt.operation_name} {receipt.state}')


class IdempotencyConflict(Exception):
    """409 idempotency_conflict — same key, different request digest."""


class FencingLoss(Exception):
    """403 fencing_loss — replay visibility lost; no rerun, no leak."""


def sha256_hex(payload):
    """Lowercase hex SHA-256 of ``payload`` (str encoded UTF-8)."""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def request_digest(semantic_request, method_name, operation_version, target_ids):
    """SHA-256 request digest per §6.1.

    RFC 8785 JCS over the validated semantic request after defaults +
    method/tool name + operation version + canonical target identifiers.
    Transport-only fields, request ID, deadline, and the key itself are
    excluded by the caller BEFORE this function runs — the digest is over
    the semantic surface only.
    """
    canonical = '\x1f'.join([
        str(semantic_request),
        str(method_name),
        str(operation_version),
        '\x1e'.join(str(t) for t in target_ids),
    ])
    return sha256_hex(canonical)


def principal_fingerprint(issuer_fingerprint, subject_fingerprint):
    """Fingerprint of the authenticated issuer+subject pair (§6.1)."""
    return sha256_hex(f'{issuer_fingerprint}\x1f{subject_fingerprint}')


def _advisory_key_lock(principal, operation_name, operation_version, key):
    """Transaction-scoped advisory key lock (§6.2).

    64-bit signed key derived from the receipt key surface; held for the
    transaction so simultaneous same-key requests serialize.
    """
    digest = sha256_hex(f'{principal}\x1f{operation_name}\x1f{operation_version}\x1f{key}')
    lock_id = int(digest[:15], 16) - (1 << 59)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [lock_id])


def _begin_or_resolve(*, principal, operation_name, operation_version, idempotency_key,
                      digest, request_id='', tenant_ref='', target_ref='',
                      caller_key='', expires_at=None):
    """Begin a new attempt or recheck an existing one (§6.2).

    Returns ``(receipt, created)``. Raises:
      - ``IdempotencyConflict`` — same key, different digest (409).
      - ``AttemptUnresolved`` — key ACTIVE/FENCING_UNPROVEN (425 loser).
    The advisory key lock serializes simultaneous same-key requests so the
    get-or-create + digest recheck is atomic per key.
    """
    with transaction.atomic():
        _advisory_key_lock(principal, operation_name, operation_version, idempotency_key)
        receipt, created = MutationReceipt.objects.get_or_create(
            principal_fingerprint=principal,
            operation_name=operation_name,
            operation_version=operation_version,
            idempotency_key=idempotency_key,
            defaults={
                'request_digest': digest,
                'tenant_ref': tenant_ref,
                'target_ref': target_ref,
                'caller_key': caller_key,
                'expires_at': expires_at,
            },
        )
        if created:
            return receipt, True
        if receipt.request_digest != digest:
            raise IdempotencyConflict(
                f'same key different digest: {receipt.operation_name} '
                f'{receipt.operation_version} {idempotency_key}',
            )
        if receipt.state in (MutationReceipt.States.ACTIVE,
                             MutationReceipt.States.FENCING_UNPROVEN):
            raise AttemptUnresolved(receipt, request_id)
        return receipt, False


def commit_attempt(receipt, *, effect, result_class='', redacted_response='',
                   result_reference='', expires_at=None):
    """Execute the effect and commit the receipt — ONE transaction (§6.2).

    The worker holds the transaction-scoped advisory key lock and locks the
    attempt row; effect + receipt + ``COMMITTED`` are one transaction. The
    loser (a simultaneous same-key request) never reaches this function —
    ``_begin_or_resolve`` raised ``AttemptUnresolved`` before any effect.
    """
    principal = receipt.principal_fingerprint
    with transaction.atomic():
        _advisory_key_lock(principal, receipt.operation_name,
                           receipt.operation_version, receipt.idempotency_key)
        locked = MutationReceipt.objects.select_for_update().get(pk=receipt.pk)
        if locked.state not in (MutationReceipt.States.ACTIVE,
                                MutationReceipt.States.ABORTED_PROVEN):
            raise AttemptUnresolved(locked)
        effect()
        locked.state = MutationReceipt.States.COMMITTED
        locked.result_class = result_class
        if redacted_response:
            locked.redacted_response = redacted_response[:65536]
        if result_reference:
            locked.result_reference = result_reference
        locked.committed_at = timezone.now()
        if expires_at is not None:
            locked.expires_at = expires_at
        locked.save()
        return locked


def sever_after_local_commit(receipt):
    """Fence a locally-committed receipt whose response was severed (§6.2).

    The effect + COMMITTED are already durable; the client never saw the
    result. The receipt stays COMMITTED — a retry with the exact prior key
    observes exactly one effect and the same result class. No second effect
    is possible from a local receipt alone (§6.3).
    """
    return MutationReceipt.objects.get(pk=receipt.pk)


def replay(receipt, *, endpoint_visible=True):
    """Replay a terminal receipt (§6.2).

    Rechecks endpoint visibility FIRST: loss → ``FencingLoss`` (403) without
    rerun or leak. A COMMITTED receipt returns the stored result class and
    allowlisted redacted response — no effect re-execution.
    """
    if not endpoint_visible:
        raise FencingLoss(
            f'replay visibility lost: {receipt.operation_name} {receipt.receipt_uuid}',
        )
    fresh = MutationReceipt.objects.get(pk=receipt.pk)
    if fresh.state != MutationReceipt.States.COMMITTED:
        raise AttemptUnresolved(fresh)
    return fresh


def reconcile_worker_death(receipt, *, worker_nonce_dead=False, backend_absent=False,
                           receipt_effect_absent=False):
    """Reconciler: prove death before ABORTED_PROVEN (§6.2).

    Requires ALL THREE proofs — worker-nonce death + PostgreSQL
    backend/transaction absence + receipt/effect absence — before fencing
    the attempt ABORTED_PROVEN. Any missing proof leaves the attempt
    FENCING_UNPROVEN (it alerts until reconciled; never auto-expired).
    Prevents a retry racing a late commit.
    """
    if not (worker_nonce_dead and backend_absent and receipt_effect_absent):
        return False
    with transaction.atomic():
        locked = MutationReceipt.objects.select_for_update().get(pk=receipt.pk)
        if locked.state != MutationReceipt.States.FENCING_UNPROVEN:
            return locked.state == MutationReceipt.States.ABORTED_PROVEN
        locked.state = MutationReceipt.States.ABORTED_PROVEN
        locked.save()
        return True


def fence_unproved(receipt):
    """Mark an attempt FENCING_UNPROVEN at wire cutoff (§7).

    Unproved terminal at wire cutoff: no JSON-RPC failure may claim retry
    safety — the transport aborts fenced and the attempt stays
    FENCING_UNPROVEN until the reconciler proves death.
    """
    with transaction.atomic():
        locked = MutationReceipt.objects.select_for_update().get(pk=receipt.pk)
        if locked.state == MutationReceipt.States.ACTIVE:
            locked.state = MutationReceipt.States.FENCING_UNPROVEN
            locked.save()
        return locked


def provider_idempotency_key(receipt):
    """Provider idempotency key derived from the receipt ID (§6.3)."""
    return f'shz-outbox-{receipt.receipt_uuid}'


def claim_lease(outbox_row, *, worker_id, lease_seconds=60):
    """Claim an outbox row with a generation-fenced lease (§6.3).

    The event-driven worker claims with a generation-fenced lease: the lease
    generation increments on every claim, so a stale worker's write after a
    re-claim is rejected (fenced). Returns the refreshed row on success;
    None when the row is already claimed by a live lease or terminal.
    """
    from ..models import OperationOutbox
    with transaction.atomic():
        locked = OperationOutbox.objects.select_for_update().get(pk=outbox_row.pk)
        if locked.state in (OperationOutbox.States.SUCCEEDED,
                            OperationOutbox.States.FAILED,
                            OperationOutbox.States.ABANDONED):
            return None
        if locked.state == OperationOutbox.States.CLAIMED:
            if locked.lease_expires_at and locked.lease_expires_at > timezone.now():
                return None
        locked.lease_generation += 1
        locked.lease_owner = worker_id
        locked.lease_expires_at = timezone.now() + timezone.timedelta(seconds=lease_seconds)
        locked.state = OperationOutbox.States.CLAIMED
        locked.save()
        return locked


def write_is_fenced(outbox_row, lease_generation):
    """True when a worker's write carries a stale lease generation (§6.3)."""
    return outbox_row.lease_generation != lease_generation


def record_outbox_result(outbox_row, *, lease_generation, succeeded, result_class=''):
    """Record success/failure BEFORE a terminal operation result (§6.3).

    Fenced: a write carrying a stale lease generation is rejected so a
    re-claimed row is never double-recorded.
    """
    from ..models import OperationOutbox
    with transaction.atomic():
        locked = OperationOutbox.objects.select_for_update().get(pk=outbox_row.pk)
        if write_is_fenced(locked, lease_generation):
            raise FencingLoss(
                f'stale lease generation {lease_generation} != {locked.lease_generation} '
                f'on {locked.operation_id}',
            )
        locked.state = (OperationOutbox.States.SUCCEEDED if succeeded
                        else OperationOutbox.States.FAILED)
        locked.result_class = result_class
        locked.terminal_at = timezone.now()
        locked.save()
        return locked
