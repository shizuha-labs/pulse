from tasks.services.service_signing import configured_service_signing_key
from celery import shared_task
from celery.exceptions import Retry
from django.utils import timezone
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q, TextField
from django.db.models.functions import Cast
from datetime import timedelta
import hashlib
import logging
import os
import re
import requests
import time

# PLAT-9452 defect 2: a recurrence-routing HOLD re-arms its timer at this
# backoff instead of leaving the popped past-due boundary to be re-queued
# instantaneously (the held-fire retry spin). Bounded re-evaluation: the hold
# state is retried at most once per window; 300s keeps held items timer-covered
# without hot-looping the scheduler against a routing that needs a human/team
# action (team restore/creation) to lift.
ROUTING_HOLD_RETRY_BACKOFF_SECONDS = 300

# PLS-483: import the in-boundary canary runner so its @shared_task registers
# with Celery (it lives in tasks/services/, which autodiscover does not import).
from tasks.services import attachment_canary_runner  # noqa: F401,E402

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=4, default_retry_delay=30)
def deliver_external_dependency_release(self, event_id):
    """Apply one ADR-0064 release occurrence idempotently."""
    from .services.external_wait import process_release_event

    try:
        result = process_release_event(event_id)
    except Exception as exc:
        raise self.retry(exc=exc)
    if result == 'pending':
        raise self.retry(countdown=300)
    return {'event_id': str(event_id), 'status': result}


@shared_task
def sweep_external_dependency_releases():
    """Recovery relay; provider transitions, not this sweep, create events."""
    from .models import ExternalDependencyReleaseEvent

    ids = list(
        ExternalDependencyReleaseEvent.objects.filter(
            state=ExternalDependencyReleaseEvent.STATE_PENDING,
            next_attempt_at__lte=timezone.now(),
        )
        .order_by('next_attempt_at', 'created_at')
        .values_list('id', flat=True)[:64]
    )
    for event_id in ids:
        deliver_external_dependency_release.delay(str(event_id))
    return {'enqueued': len(ids)}


@shared_task(acks_late=True, reject_on_worker_lost=True)
def execute_deferred_workflow_command(command_id):
    from .services.deferred_workflow import execute
    return execute(command_id)


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def release_blocked_descendants_async(self, blocker_id, batch_size=None):
    """Compatibility for already-enqueued descendant reconciliation jobs.

    New large requests queue execute_deferred_workflow_command before changing
    root state; they cannot commit a terminal root then release its dependents.

    Retained for deliveries produced by older versions. Whole-command
    deferral is required for new requests because PostgreSQL validates active
    blocker ownership before any on_commit callback can run.

    Replayable by construction: dependents already restored (or held by other
    active blockers) are skipped by the status/remaining-blocker checks inside
    _auto_unblock_tasks_blocked_by, so a redelivered batch is a no-op for
    finished work. Each invocation restores at most ``batch_size`` dependents
    and re-enqueues itself while blocked dependents remain. Progress is
    owner-visible in the blocker's ``dependency_release_fanout`` metadata;
    retry exhaustion fails loud with a durable comment (PLS-1286 class) and
    the blocked-item sweep remains the backstop.
    """
    import os

    from .models import Comment, Item
    from .services.workflow_service import WorkflowService

    blocker = Item.objects.filter(pk=blocker_id).first()
    if blocker is None:
        return {'blocker_id': str(blocker_id), 'status': 'missing'}
    if not WorkflowService._status_releases_blocks(
        getattr(blocker, 'status', None),
    ):
        # Replayed after a reopen (PLS-895 re-armed the links): nothing to
        # release; the reopen path owns the graph state now.
        return {'blocker_id': str(blocker_id), 'status': 'not_releasing'}

    size = max(1, int(
        batch_size
        or os.environ.get('PULSE_DEPENDENCY_RELEASE_BATCH_SIZE', '10')
    ))

    try:
        resumed = WorkflowService._auto_unblock_tasks_blocked_by(
            blocker, user=None, require_restore=False, max_restores=size,
        )
    except Exception as exc:
        if self.request.retries >= (self.max_retries or 0):
            # PLS-1286 class: a repeatedly-failing release batch must surface,
            # not strand silently. The durable comment names the blocker and
            # points at the sweep backstop, which keeps retrying the restores.
            Comment.objects.create(
                item=blocker,
                author_id=0,
                author_email='system@pulse',
                content=(
                    "🚨 **Dependency release fan-out failing:** the async "
                    "restore batch exhausted its retries. Some blocked "
                    "dependent(s) may still be linked to this released root. "
                    "The blocked-item sweep remains the backstop and will keep "
                    "retrying; investigate repeated restore failures for this "
                    f"graph (batch_size={size})."
                ),
            )
            logger.exception(
                "PLAT-9069: dependency-release fan-out exhausted retries for "
                "blocker %s",
                getattr(blocker, 'item_key', blocker.pk),
            )
        raise self.retry(exc=exc)

    remaining = len(WorkflowService._blocked_dependent_ids(blocker))
    md = dict(blocker.metadata or {})
    fanout = dict(md.get('dependency_release_fanout') or {})
    restored_total = int(fanout.get('restored') or 0) + len(resumed)
    fanout.update({
        'state': 'running' if remaining else 'complete',
        'total': int(fanout.get('total') or (restored_total + remaining)),
        'restored': restored_total,
        'remaining': remaining,
        'batch_size': size,
        'updated_at': timezone.now().isoformat(),
    })
    md['dependency_release_fanout'] = fanout
    blocker.metadata = md
    blocker.save(update_fields=['metadata'])

    if remaining:
        # Continuation is a FRESH enqueue (retries are reserved for failures),
        # so batch count is not capped by max_retries.
        release_blocked_descendants_async.apply_async(
            kwargs={'blocker_id': blocker_id, 'batch_size': size},
            countdown=2,
        )
    elif len(resumed) and fanout['state'] == 'complete':
        # Completion marker only on the batch that finished the fan-out —
        # a replayed/redelivered batch (resumed empty) must not duplicate it.
        Comment.objects.create(
            item=blocker,
            author_id=0,
            author_email='system@pulse',
            content=(
                f"🔓 **Dependency release fan-out complete:** {restored_total} "
                "blocked dependent(s) restored asynchronously."
            ),
        )
    return {
        'blocker_id': str(blocker_id),
        'restored_this_batch': len(resumed),
        'remaining': remaining,
        'state': fanout['state'],
    }


@shared_task(bind=True, max_retries=4, default_retry_delay=30)
def deliver_external_response_hold_wake(self, wake_id):
    """Reduce one durable branch-3 wake; retry without duplicating effects."""
    from .services.external_wait import process_external_response_wake

    try:
        result = process_external_response_wake(wake_id)
    except Exception as exc:
        raise self.retry(exc=exc)
    if result == 'pending':
        raise self.retry(countdown=300)
    return {'wake_id': str(wake_id), 'status': result}


@shared_task
def sweep_external_response_holds():
    """Checkpoint reconciler + durable outbox relay + owner-routed fail-loud."""
    from .models import ExternalResponseHoldWake, ExternalWaitAuthority
    from .services.external_wait import enqueue_external_response_wake

    now = timezone.now()
    due = list(
        ExternalWaitAuthority.objects.filter(
            branch=ExternalWaitAuthority.BRANCH_EXTERNAL_RESPONSE,
            state=ExternalWaitAuthority.STATE_WAITING,
            checkpoint_at__lte=now,
        )
        .select_related('item')
        .order_by('checkpoint_at', 'created_at')[:64]
    )
    created = 0
    for authority in due:
        result = enqueue_external_response_wake(
            authority.item,
            generation=authority.wait_generation,
            kind=ExternalResponseHoldWake.KIND_CHECKPOINT,
            correlation_key=(
                f'checkpoint:{authority.id}:{authority.wait_generation}'
            ),
            service_identity='pulse-checkpoint-scheduler',
        )
        created += int(not result['replayed'])

    pending_ids = list(
        ExternalResponseHoldWake.objects.filter(
            state=ExternalResponseHoldWake.STATE_PENDING,
            next_attempt_at__lte=now,
        )
        .order_by('next_attempt_at', 'created_at')
        .values_list('id', flat=True)[:64]
    )
    for wake_id in pending_ids:
        deliver_external_response_hold_wake.delay(str(wake_id))

    oldest = (
        ExternalResponseHoldWake.objects.filter(
            state=ExternalResponseHoldWake.STATE_PENDING,
        )
        .select_related('item', 'authority')
        .order_by('created_at')
        .first()
    )
    fail_loud = False
    fail_loud_error = False
    if oldest and oldest.created_at <= now - timedelta(minutes=10):
        from .services.deduplication import AlertDeduplicationService
        try:
            alert, is_new = AlertDeduplicationService.submit_alert({
                'item_type': 'alert.error',
                'title': 'Pulse external-response hold wake is overdue',
                'description': (
                    'A sanitized branch-3 wake exceeded the 10-minute delivery SLA. '
                    f'Carrier={oldest.item.item_key or oldest.item_id}; '
                    f'generation={oldest.wait_generation}. No contact data is included.'
                ),
                'source': 'pulse-external-response-hold-reconciler',
                'source_id': f'external-response-hold:{oldest.authority_id}',
                'severity': 'error',
                'priority': 'high',
                'project_id': oldest.item.project_id,
                'assignment_group': oldest.item.assignment_group,
                'assignee_id': oldest.item.assignee_id,
                'assignee_email': oldest.item.assignee_email,
                'labels': {
                    'mechanism': 'external_response_hold',
                    'reason': 'wake_delivery_overdue',
                },
                'payload': {
                    'receipt': str(oldest.authority.receipt_id),
                    'wait_generation': oldest.wait_generation,
                },
            })
            if is_new:
                transaction.on_commit(
                    lambda alert_id=alert.id: send_alert_notification.delay(alert_id)
                )
            fail_loud = True
        except Exception:
            # The periodic task must remain an observable liveness signal even
            # when its own alert sink is unavailable. Returning a red field also
            # prevents a caller from misreading the exception as no overdue work.
            logger.exception(
                'external-response fail-loud alert persistence failed'
            )
            fail_loud_error = True
    return {
        'checkpoint_wakes_created': created,
        'pending_enqueued': len(pending_ids),
        'fail_loud': fail_loud,
        'fail_loud_error': fail_loud_error,
    }


@shared_task
def archive_external_response_hold_audit():
    """Copy >90-day redacted rows into the seven-year append-only archive."""
    from .models import (
        ExternalResponseHoldAudit,
        ExternalResponseHoldAuditArchive,
    )

    now = timezone.now()
    ids = list(
        ExternalResponseHoldAudit.objects.filter(
            archived_at__isnull=True,
            created_at__lt=now - timedelta(days=90),
        )
        .order_by('created_at')
        .values_list('id', flat=True)[:256]
    )
    archived = 0
    for audit_id in ids:
        with transaction.atomic():
            row = ExternalResponseHoldAudit.objects.select_for_update().get(pk=audit_id)
            archive, _created = ExternalResponseHoldAuditArchive.objects.get_or_create(
                source_audit_id=row.id,
                defaults={
                    'organization_id': row.organization_id,
                    'item_id_snapshot': row.item_id,
                    'envelope': {
                        'authority_id': str(row.authority_id),
                        'wait_generation': row.wait_generation,
                        'event_type': row.event_type,
                        'correlation_key': row.correlation_key,
                        'actor_id': row.actor_id,
                        'actor_email': row.actor_email,
                        'service_identity': row.service_identity,
                        'evidence_digest': row.evidence_digest,
                        'before_state': row.before_state,
                        'after_state': row.after_state,
                        'reason_code': row.reason_code,
                    },
                    'source_created_at': row.created_at,
                    'retain_until': row.created_at + timedelta(days=365 * 7),
                },
            )
            if row.archived_at is None:
                row.archived_at = archive.archived_at
                row.save(update_fields=['archived_at'])
                archived += 1
    return {'considered': len(ids), 'archived': archived}


@shared_task
def revalidate_human_external_waits():
    """Keep quiet human-held waits visible on ADR-0056's bounded 12h SLA."""
    from .models import ExternalWaitAuthority, Item
    from .services.external_wait import reconcile_group_owned_human_waits
    from .services.mechanical_comments import (
        MechanicalCommentFactory,
        deterministic_event_id,
    )

    now = timezone.now()
    authority_ids = list(
        ExternalWaitAuthority.objects.filter(
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_WAITING,
            revalidate_after__lte=now,
        )
        .order_by('revalidate_after', 'created_at')
        .values_list('id', 'item_id')[:64]
    )
    reminded = 0
    for authority_id, item_id in authority_ids:
        # A failed identity lookup can park a staffed human team without a
        # concrete owner. Humans emit no agent-recovery event and an unchanged
        # roster emits no roster event. Retry this exact due carrier before
        # reminding its holder; preserve the external wait and its blockers.
        # Do this before taking the authority lock: the canonical reconciler
        # locks Item then Authority, so nesting it below would invert that order.
        reconcile_group_owned_human_waits(item_id=item_id)
        with transaction.atomic():
            authority = (
                ExternalWaitAuthority.objects.select_for_update()
                .select_related('item')
                .filter(pk=authority_id)
                .first()
            )
            if not authority or authority.state != ExternalWaitAuthority.STATE_WAITING:
                continue
            item = Item.objects.select_for_update().get(pk=authority.item_id)
            if item.external_wait_generation != authority.wait_generation:
                continue
            if item.status != 'waiting_external':
                # The park is retired. A seated root drops the retry clock.
                # An unseated group-owned root stays runnable and retries
                # once per 12h until a roster member is elected.
                if item.assignee_id and (item.assignee_email or '').strip():
                    authority.state = ExternalWaitAuthority.STATE_RELEASED
                    authority.released_at = now
                    authority.save(update_fields=[
                        'state', 'released_at', 'updated_at',
                    ])
                else:
                    authority.last_revalidated_at = now
                    authority.revalidate_after = now + timedelta(hours=12)
                    authority.save(update_fields=[
                        'last_revalidated_at', 'revalidate_after', 'updated_at',
                    ])
                continue
            evidence = authority.evidence if isinstance(authority.evidence, dict) else {}
            holder = str(evidence.get('holder_ref') or 'recorded external holder')[:200]
            subject = str(evidence.get('requested_subject') or item.title)[:300]
            receipt = str(authority.receipt_id)
            boundary = authority.revalidate_after.isoformat()
            MechanicalCommentFactory.create(
                item=item,
                origin_kind='WORKFLOW_LIFECYCLE',
                origin_event='external_wait.human_revalidation',
                template_version=1,
                structured_payload={
                    'receipt': receipt,
                    'holder': holder,
                    'subject': subject,
                },
                origin_event_id=deterministic_event_id(
                    'external_wait.human_revalidation', item, receipt, boundary,
                ),
            )
            authority.last_revalidated_at = now
            authority.revalidate_after = now + timedelta(hours=12)
            authority.save(update_fields=[
                'last_revalidated_at', 'revalidate_after', 'updated_at',
            ])
            reminded += 1
    return {'checked': len(authority_ids), 'reminded': reminded}


@shared_task(bind=True, max_retries=2)
def deliver_escalation_routing_outbox(self, outbox_id):
    """Deliver one PLAT-4609 owner-attention receipt at least once.

    The committed row is the authority. Duplicate task delivery coalesces under
    a row lock; failure becomes loud after three attempts or five minutes.
    """
    from datetime import timedelta
    from .models import EscalationRoutingNotificationOutbox
    from .services.notifications import notification_service

    now = timezone.now()
    with transaction.atomic():
        row = EscalationRoutingNotificationOutbox.objects.select_for_update().filter(
            pk=outbox_id
        ).first()
        if not row:
            return {'status': 'missing', 'outbox_id': str(outbox_id)}
        if row.delivered_at or row.failed_at:
            return {
                'status': 'delivered' if row.delivered_at else 'failed',
                'outbox_id': str(row.id),
            }
        if row.in_flight and row.in_flight_until and row.in_flight_until > now:
            return {'status': 'in_flight', 'outbox_id': str(row.id)}
        row.in_flight = True
        row.in_flight_until = now + timedelta(seconds=30)
        row.attempts += 1
        row.save(update_fields=['in_flight', 'in_flight_until', 'attempts'])
        attempt = row.attempts
        payload = dict(row.payload or {})
        recipients = list(row.recipients or [])
        item = row.item

    try:
        if not recipients:
            raise RuntimeError('escalation_notice_has_no_recipients')
        title = f"{payload.get('item_key') or item.item_key}: typed escalation {payload.get('outcome')}"
        final = payload.get('final_state') or {}
        message = (
            f"Classification: {payload.get('classification')}\n"
            f"Reason: {payload.get('reason_code')}\n"
            f"Generation: {payload.get('generation')}\n"
            f"Final owner: {final.get('status')} / {final.get('assignment_group')} / "
            f"{final.get('assignee_email') or final.get('assignee_id')}"
        )
        direct_ids = {int(r['user_id']) for r in recipients if r.get('user_id') is not None}
        for recipient in recipients:
            notification_service.notify_user(
                user_id=int(recipient['user_id']),
                title=title,
                message=message,
                event_type='system',
                severity='error' if payload.get('outcome') == 'held' else 'info',
                item=item,
                exclude_delegate_user_ids=direct_ids,
            )
    except Exception as exc:
        now = timezone.now()
        with transaction.atomic():
            row = EscalationRoutingNotificationOutbox.objects.select_for_update().get(pk=outbox_id)
            row.in_flight = False
            row.in_flight_until = None
            row.last_error = str(exc)[:2000]
            deadline_hit = now >= row.created_at + timedelta(minutes=5)
            if row.attempts >= 3 or deadline_hit:
                row.failed_at = now
                row.save(update_fields=[
                    'in_flight', 'in_flight_until', 'last_error', 'failed_at',
                ])
                logger.error(
                    'PLAT-4609 escalation notification FAILED LOUD outbox=%s item=%s '
                    'attempts=%s age_seconds=%s error=%s',
                    row.id, row.item_id, row.attempts,
                    int((now - row.created_at).total_seconds()), exc,
                )
                return {'status': 'failed_loud', 'outbox_id': str(row.id)}
            row.next_attempt_at = now + timedelta(seconds=min(60, 5 * (2 ** attempt)))
            row.save(update_fields=[
                'in_flight', 'in_flight_until', 'last_error', 'next_attempt_at',
            ])
        raise self.retry(exc=exc, countdown=min(60, 5 * (2 ** attempt)))

    with transaction.atomic():
        row = EscalationRoutingNotificationOutbox.objects.select_for_update().get(pk=outbox_id)
        row.delivered_at = timezone.now()
        row.in_flight = False
        row.in_flight_until = None
        row.last_error = ''
        row.save(update_fields=[
            'delivered_at', 'in_flight', 'in_flight_until', 'last_error',
        ])
    return {'status': 'delivered', 'outbox_id': str(outbox_id)}


@shared_task(bind=True, max_retries=0)
def sweep_escalation_routing_outbox(self):
    """Narrow recovery backstop for committed rows whose on-commit enqueue died."""
    from .models import EscalationRoutingNotificationOutbox

    now = timezone.now()
    ids = list(EscalationRoutingNotificationOutbox.objects.filter(
        delivered_at__isnull=True,
        failed_at__isnull=True,
        next_attempt_at__lte=now,
    ).filter(
        Q(in_flight=False) | Q(in_flight_until__lte=now)
    ).order_by('next_attempt_at', 'created_at').values_list('id', flat=True)[:32])
    for outbox_id in ids:
        deliver_escalation_routing_outbox.delay(str(outbox_id))
    return {'enqueued': len(ids)}


def _send_gate_owner_dm(recipient_user_id, content):
    """Deliver one deployment-gate owner/team DM via the Connect system-message
    boundary (the same authenticated path recurring-cycle + escalation use)."""
    from tasks.services.connect_origin import system_message_headers
    connect_url = f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/"
    body = {
        'recipient_user_id': int(recipient_user_id),
        'content': content,
        'sender_name': 'Pulse Deployment Gate',
        'sender_service': 'shizuha-pulse',
    }
    resp = requests.post(connect_url, json=body, headers=system_message_headers(body), timeout=5)
    resp.raise_for_status()


def _deployment_gate_recipients(item):
    """Resolve {user_id: label} for a deployment-gate HOLD notice: the item
    owner/assignee plus the item's assignment-group lead (best effort)."""
    recipients = {}
    for uid, label in ((getattr(item, 'assignee_id', None), 'assignee'),
                       (getattr(item, 'created_by_id', None), 'owner')):
        if uid:
            recipients.setdefault(int(uid), label)
    try:
        from .services.workflow_service import WorkflowService as W
        from .user_service import user_service
        group = getattr(item, 'assignment_group', '') or ''
        lead_email = W._resolve_team_lead_email(group, item=item) if group else ''
        if lead_email:
            user = user_service.get_user_by_email(lead_email)
            uid = user.get('id') if user else None
            if uid:
                recipients.setdefault(int(uid), f'{group}-lead')
    except Exception:
        pass  # team-lead resolution is best-effort; the owner/assignee still gets it
    return recipients


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def deliver_deployment_gate_outbox(self, outbox_id):
    """PLS-521 P2: fail-loud delivery of one deployed-evidence gate outbox row.

    A durable in-flight lease coalesces concurrent workers before external I/O;
    `published_at` is the completed-delivery authority. On a HOLD verdict the
    item owner/assignee + assignment-group lead are DM'd so a stuck
    Merge->Verification gate is never silent; pass verdicts are published without
    a DM. Delivery becomes LOUD in the log once Celery retries exhaust, so a
    broken Connect boundary is observable rather than silently swallowed.
    """
    from .models import DeploymentEvidenceGateOutbox
    now = timezone.now()
    with transaction.atomic():
        row = DeploymentEvidenceGateOutbox.objects.select_for_update().filter(pk=outbox_id).first()
        if not row:
            return {'status': 'missing', 'outbox_id': str(outbox_id)}
        if row.published_at:
            return {'status': 'published', 'outbox_id': str(row.id)}
        if row.in_flight and row.in_flight_until and row.in_flight_until > now:
            return {'status': 'in_flight', 'outbox_id': str(row.id)}
        row.in_flight = True
        row.in_flight_until = now + timedelta(seconds=30)
        row.attempts += 1
        row.save(update_fields=['in_flight', 'in_flight_until', 'attempts'])
        payload = dict(row.payload or {})
        item = row.verdict.item
        attempt = row.attempts
    try:
        if (payload.get('outcome') or '') == 'hold':
            recipients = _deployment_gate_recipients(item)
            item_key = payload.get('item_key') or item.item_key
            content = (
                f"[Deployment Gate HELD] {item_key}: {item.title}\n"
                f"Merge->Verification is blocked: {payload.get('reason')} "
                f"(requirement gen {payload.get('requirement_generation')}, "
                f"evidence gen {payload.get('evidence_generation')}).\n"
                f"Produce/refresh deployed-evidence for this generation, or invalidate "
                f"stale evidence, then retry.\npulse_get_task(\"{item_key}\")"
            )
            if not recipients:
                raise RuntimeError('deployment_gate_hold_has_no_resolvable_recipient')
            for uid in recipients:
                _send_gate_owner_dm(uid, content)
    except Exception as exc:
        with transaction.atomic():
            row = DeploymentEvidenceGateOutbox.objects.select_for_update().get(pk=outbox_id)
            row.in_flight = False
            row.in_flight_until = None
            row.last_error = str(exc)[:2000]
            row.next_attempt_at = timezone.now() + timedelta(
                seconds=min(60, 5 * (2 ** max(0, attempt - 1))),
            )
            row.save(update_fields=[
                'in_flight', 'in_flight_until', 'last_error', 'next_attempt_at',
            ])
        attempts = self.request.retries + 1
        if attempts > self.max_retries:
            logger.error('PLS-521 deployment-gate outbox FAILED LOUD outbox=%s item=%s attempts=%s error=%s',
                         row.id, item.pk, attempts, exc)
            return {'status': 'failed_loud', 'outbox_id': str(row.id)}
        logger.warning('PLS-521 deployment-gate outbox delivery attempt %s failed outbox=%s: %s',
                       attempts, row.id, exc)
        raise self.retry(exc=exc)
    with transaction.atomic():
        row = DeploymentEvidenceGateOutbox.objects.select_for_update().get(pk=outbox_id)
        if not row.published_at:
            row.published_at = timezone.now()
            row.in_flight = False
            row.in_flight_until = None
            row.last_error = ''
            row.save(update_fields=[
                'published_at', 'in_flight', 'in_flight_until', 'last_error',
            ])
    logger.info('PLS-521 deployment-gate outbox delivered outbox=%s item=%s outcome=%s',
                row.id, item.pk, payload.get('outcome'))
    return {'status': 'delivered', 'outbox_id': str(outbox_id)}


@shared_task(bind=True, max_retries=0)
def sweep_deployment_gate_outbox(self):
    """Recovery backstop + liveness signal: re-enqueue any unpublished
    deployment-gate outbox rows (whose on-commit enqueue was lost) and emit the
    pending-count / oldest-age liveness log (LOUD when the oldest exceeds 5 min).
    """
    from .models import DeploymentEvidenceGateOutbox
    now = timezone.now()
    pending = list(DeploymentEvidenceGateOutbox.objects.filter(
        published_at__isnull=True,
        next_attempt_at__lte=now,
    ).filter(
        Q(in_flight=False) | Q(in_flight_until__lte=now)
    ).order_by('created_at').values_list('id', 'created_at')[:64])
    for outbox_id, _created in pending:
        deliver_deployment_gate_outbox.delay(str(outbox_id))
    if pending:
        oldest_age = int((now - pending[0][1]).total_seconds())
        (logger.error if oldest_age > 300 else logger.info)(
            'PLS-521 deployment-gate outbox liveness pending=%s oldest_age_seconds=%s',
            len(pending), oldest_age)
    return {'enqueued': len(pending)}
@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def deliver_publication_outbox(self, outbox_id):
    """Publish one PLS-482 immutable tuple exactly once to Pulse in-app UI.

    Notification creation and the delivered marker share one transaction.  A
    crash before commit creates neither; a replay after commit sees the linked
    notification and returns without a second visible effect.
    """
    from .models import Notification, PublicationOutbox

    # P2 #1 (PLAT-1254): exactly ONE attempt counted per delivery execution —
    # incremented up front in its own transaction so neither the delivery
    # transaction's rollback nor the failure handler can double-count it.
    updated = PublicationOutbox.objects.filter(
        pk=outbox_id, delivered_at__isnull=True
    ).update(attempts=F('attempts') + 1)
    if not updated:
        return {'status': 'delivered', 'outbox_id': str(outbox_id)}

    try:
        with transaction.atomic():
            row = (
                PublicationOutbox.objects.select_for_update()
                .select_related('binding__item', 'binding__attempt')
                .get(pk=outbox_id)
            )
            if row.delivered_at:
                return {'status': 'delivered', 'outbox_id': str(row.id)}
            item = row.binding.item
            payload = dict(row.payload or {})
            if row.notification_id is None:
                pages = payload.get('pages') or []
                versions = ', '.join(
                    f"{str(page.get('page_id'))[:8]}@v{page.get('version')}"
                    for page in pages
                )
                notification = Notification.objects.create(
                    user_id=row.recipient_user_id,
                    item=item,
                    notification_type='status_change',
                    title=f"{item.item_key or item.pk}: immutable publication handoff",
                    message=(
                        f"Snapshot {payload.get('publication_snapshot_id')} ({versions}); "
                        f"review round {payload.get('review_round_id')}; "
                        f"result {payload.get('publication_result')}; "
                        f"artifact authority {payload.get('artifact_authority_ref')}."
                    ),
                )
                row.notification = notification
            row.delivered_at = timezone.now()
            row.last_error = ''
            row.save(update_fields=[
                'notification', 'delivered_at', 'last_error',
            ])
            attempt = row.binding.attempt
            if attempt.state == 'published':
                attempt.state = 'delivered'
                attempt.save(update_fields=['state', 'updated_at'])
        return {'status': 'delivered', 'outbox_id': str(outbox_id)}
    except Exception as exc:
        # P2 #1 (PLAT-1254 fail-loud): after the retry budget the failure must
        # surface to a specific owner, not rot in last_error. Invariant under
        # probe: every committed binding produces exactly one logical delivery
        # (sweep_publication_outbox is the liveness backstop and re-probes it).
        now = timezone.now()
        row = PublicationOutbox.objects.filter(pk=outbox_id).select_related(
            'binding__item'
        ).first()
        attempts_so_far = row.attempts if row else 0
        PublicationOutbox.objects.filter(pk=outbox_id).update(
            last_error=str(exc)[:2000],
            next_attempt_at=now + timedelta(seconds=30),
        )
        if row is not None and attempts_so_far >= 3:
            logger.error(
                'PLS-482 publication delivery FAILED LOUD outbox=%s item=%s '
                'attempts=%s error=%s',
                outbox_id, row.binding.item_id, attempts_so_far, exc,
            )
            # Reika P2 (PLAT-1254 owner-DM leg): the failure must reach a
            # specific person through their ACTIVE channels (Connect system-DM /
            # push / in-app via notification_service), not only an in-app row
            # that goes unseen exactly when delivery is broken. DM once, at the
            # threshold crossing, so retries don't spam the owner.
            if attempts_so_far == 3:
                try:
                    from .services.notifications import notification_service
                    item = row.binding.item
                    notification_service.notify_user(
                        user_id=item.assignee_id or row.recipient_user_id,
                        title=f"{item.item_key or item.pk}: publication delivery FAILING",
                        message=(
                            f"Publication outbox {outbox_id} has failed {attempts_so_far} "
                            f"delivery attempts (last error: {str(exc)[:300]}). The immutable "
                            "binding is committed but its notification is NOT delivered — "
                            "investigate before assuming the handoff surfaced."
                        ),
                        event_type='alert',
                        severity='critical',
                        item=item,
                    )
                except Exception:
                    logger.exception(
                        'PLS-482 could not deliver fail-loud owner DM for outbox=%s',
                        outbox_id,
                    )
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=0)
def sweep_publication_outbox(self):
    """Recover committed publication rows whose on-commit enqueue was lost.

    P2 #1 (PLAT-1254): this periodic sweep is the demonstrated LIVENESS signal
    for publication delivery, and it probes the §17 invariant that every
    committed binding yields exactly one logical delivery — any row still
    undelivered past the deadline is logged loud, not silently retried forever.
    """
    from .models import PublicationOutbox

    now = timezone.now()
    ids = list(
        PublicationOutbox.objects.filter(
            delivered_at__isnull=True,
            next_attempt_at__lte=now,
        ).order_by('next_attempt_at', 'created_at').values_list('id', flat=True)[:32]
    )
    for outbox_id in ids:
        deliver_publication_outbox.delay(str(outbox_id))
    stuck = PublicationOutbox.objects.filter(
        delivered_at__isnull=True,
        created_at__lte=now - timedelta(minutes=15),
    ).count()
    if stuck:
        logger.error(
            'PLS-482 publication outbox INVARIANT PROBE: %s committed binding(s) '
            'undelivered for >15m — binding-implies-one-delivery is not holding.',
            stuck,
        )
    return {'enqueued': len(ids), 'stuck_over_deadline': stuck}


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def deliver_qa_verification_outbox(self, outbox_id):
    """Deliver one matured PLS-593 demand intent with version fencing."""
    from .models import QaVerificationOutbox
    from .services.qa_verification_demand import materialize_qa_verification_outbox

    row = QaVerificationOutbox.objects.filter(pk=outbox_id).first()
    if row is None:
        return {'status': 'missing', 'outbox_id': str(outbox_id)}
    if row.delivered_at:
        return {'status': 'delivered', 'outbox_id': str(outbox_id)}
    now = timezone.now()
    if row.available_at > now:
        countdown = max(1, int((row.available_at - now).total_seconds()))
        raise self.retry(countdown=countdown)
    try:
        return materialize_qa_verification_outbox(outbox_id, now=now)
    except Exception as exc:
        QaVerificationOutbox.objects.filter(pk=outbox_id).update(
            last_error=str(exc)[:2000],
        )
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=0)
def sweep_qa_verification_outbox(self):
    """Crash-recovery backstop over committed intents, never task-history scan."""
    from .models import QaVerificationOutbox

    now = timezone.now()
    ids = list(
        QaVerificationOutbox.objects.filter(
            delivered_at__isnull=True,
            available_at__lte=now,
        ).order_by('available_at', 'created_at').values_list('id', flat=True)[:32]
    )
    for outbox_id in ids:
        deliver_qa_verification_outbox.delay(str(outbox_id))
    return {'enqueued': len(ids)}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def generate_ai_recap(self, run_id):
    """Generate a bounded citation-backed recap from a queued AiGenerationRun.

    The API creates the run synchronously, then this Celery task performs the
    source snapshot/read and writes status/output/citations back to the same
    row.  Keeping generation behind a task prevents request threads from doing
    unbounded model/context work and gives operators an auditable retry point.
    """
    try:
        from .services.ai_recaps import generate_recap_run
        generate_recap_run(run_id)
    except Exception as exc:
        logger.exception('AI recap generation failed for run %s', run_id)
        raise self.retry(exc=exc)
    return {'run_id': str(run_id), 'status': 'completed'}


@shared_task(bind=True, max_retries=1)
def sweep_stale_ai_recaps(self):
    """Fail-loud liveness probe for the async AI-recap mechanism (PLS-307, kei P2).

    Invariant: every `AiGenerationRun` created (`queued`) reaches a terminal
    state (`completed`/`failed`) within a bounded time. The async generation
    runs behind Celery; if the worker stops consuming or crashes mid-run, a row
    can sit `queued`/`running` forever with no visible error and no DM — a silent
    stall. This sweep is the named fail-loud path: it detects runs stuck past a
    threshold and files a deduped task onto the owning **engineering** team's
    queue (surfaced in-product on the owner's heartbeat), and it also surfaces a
    burst of terminal `failed` runs. Self-resolves when the signal clears.
    Idempotent (dedup via metadata.ai_recap_signal); never raises into beat.
    """
    import os
    from datetime import timedelta
    try:
        from .models import AiGenerationRun, Item, Comment, Project
        from .services.workflow_service import WorkflowService as W
    except Exception as e:
        logger.warning(f"sweep_stale_ai_recaps: import failed: {e}")
        return {'error': 'import'}

    now = timezone.now()
    TEAM = 'engineering'
    SIGNAL = 'ai-recap-stall'
    stuck_minutes = int(os.environ.get('PULSE_AI_RECAP_STUCK_MINUTES', '30'))
    fail_window_min = int(os.environ.get('PULSE_AI_RECAP_FAIL_WINDOW_MINUTES', '60'))
    fail_threshold = int(os.environ.get('PULSE_AI_RECAP_FAIL_THRESHOLD', '5'))
    stuck_cutoff = now - timedelta(minutes=stuck_minutes)

    finding = None
    try:
        stuck = list(AiGenerationRun.objects.filter(
            status__in=['queued', 'running'], updated_at__lt=stuck_cutoff,
        ).order_by('updated_at')[:20])
        recent_failed = AiGenerationRun.objects.filter(
            status='failed', updated_at__gte=now - timedelta(minutes=fail_window_min),
        ).count()
        if stuck or recent_failed >= fail_threshold:
            oldest = stuck[0] if stuck else None
            oldest_age_m = int((now - oldest.updated_at).total_seconds() // 60) if oldest else 0
            samples = '; '.join(
                f"{r.project.key if r.project_id else '?'} {r.type} run={str(r.id)[:8]} "
                f"{r.status} {int((now - r.updated_at).total_seconds() // 60)}m"
                for r in stuck[:10]
            ) or 'none'
            body = (
                f"AI recap async generation liveness probe tripped (PLS-307).\n\n"
                f"- Runs stuck in queued/running > {stuck_minutes}m: **{len(stuck)}** "
                f"(oldest {oldest_age_m}m).\n"
                f"- Terminal `failed` runs in the last {fail_window_min}m: "
                f"**{recent_failed}** (threshold {fail_threshold}).\n\n"
                f"Stuck samples: {samples}\n\n"
                f"Invariant: every `AiGenerationRun` must reach completed/failed within "
                f"{stuck_minutes}m. If runs are stuck, the Celery worker likely stopped "
                f"consuming or is crashing mid-`generate_ai_recap` — check worker health "
                f"and logs. If failures spiked, inspect `error_message` on the failed rows. "
                f"This signal self-resolves when no run is stuck and the failure rate drops "
                f"below threshold. [PLS-307 fail-loud mechanism]"
            )
            title = (f"[AI Recaps] Generation stall — {len(stuck)} stuck run(s), "
                     f"{recent_failed} recent failure(s)")
            finding = (SIGNAL, 'high', title, body)
    except Exception as e:
        logger.warning(f"sweep_stale_ai_recaps: detection failed: {e}")
        return {'error': 'detect'}

    # ── reconcile finding → engineering-team task (file new, resolve cleared) ──
    filed = resolved = 0
    try:
        existing = None
        deferred = False
        for it in Item.objects.filter(assignment_group=TEAM):
            if (it.metadata or {}).get('ai_recap_signal') != SIGNAL:
                continue
            if (it.status or '').lower() == 'deferred':
                deferred = True
            elif not _is_terminal_status(it.status):
                existing = it
                break
        if finding and not existing and not deferred:
            _sig, prio, title, body = finding
            proj = Project.objects.filter(key='PLS').first() or Project.objects.order_by('id').first()
            try:
                uid, email = W._resolve_team_assignee(TEAM, exclude_email=None, enforce_cap=False)
            except Exception:
                uid, email = None, None
            key = proj.get_next_item_key() if proj else None
            it = Item.objects.create(
                project=proj, item_key=key, title=title[:200], mode='task', status='open',
                workflow_name='autonomous-dev', assignment_group=TEAM, priority=prio,
                created_by_email='system@pulse', created_by_id=0, description=body,
                metadata={'ai_recap_signal': SIGNAL, 'auto_filed': now.isoformat()},
            )
            if uid or email:
                it.assign({'id': uid, 'email': email}, save=True)
            filed += 1
            logger.warning(f"sweep_stale_ai_recaps: FILED {it.item_key} → {TEAM} ({email or 'unrouted'})")
        elif existing and not finding:
            Comment.objects.create(item=existing, author_id=0, author_email='system@pulse',
                content='[ai-recap] stall signal cleared — no stuck runs and failure rate normal; auto-resolving.')
            # Close the fail-loud finding with a terminal status that is valid
            # for its workflow. `resolved` is a legacy terminal slug, but modern
            # workflow-backed items (e.g. autonomous-dev) may not include it; a
            # blind save would be coerced back to the workflow's initial state.
            existing.status = _terminal_status_for_workflow(existing)
            try:
                existing._activity_actor = (0, 'system@pulse')
            except Exception:
                pass
            existing.save(update_fields=['status', 'updated_at'])
            resolved += 1
    except Exception as e:
        logger.warning(f"sweep_stale_ai_recaps: reconcile failed: {e}")

    return {'stuck_or_failing': bool(finding), 'filed': filed, 'resolved': resolved}


# PLAT-337: the canonical set of terminal status slugs for "should we still
# (re-)dispatch work for this task?" decisions. Mirrors views.TERMINAL_STATUS_SLUGS
# (kept local here because views imports tasks — importing it back would be a
# circular import). NOTE: scheduler.TERMINAL_STATUSES is NOT sufficient — it omits
# the workflow `done` slug, which is exactly the CTX-13 case.
_TERMINAL_ENUM_SLUGS = frozenset({
    'completed', 'cancelled', 'canceled', 'closed', 'done',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'expired', 'deferred',
    # PLAT-940: 'applied' is the terminal final state for change-request workflow;
    # must not trigger failover re-routing when primary agent goes offline.
    'applied',
})


def _terminal_status_for_workflow(item):
    """Return a terminal status slug that will survive Item.save() coercion.

    Legacy code used the literal `resolved`, but workflow-backed tasks only keep
    slugs that are members of their workflow. Prefer human-readable terminal
    slugs, then fall back to any `done`-category status in the workflow.
    """
    try:
        from .models import Workflow
        wf = Workflow.resolve(item.workflow_name)
        if wf:
            valid = set(wf.workflow_statuses.values_list('status__slug', flat=True))
            for slug in ('resolved', 'done', 'completed', 'closed'):
                if slug in valid:
                    return slug
            done_status = (
                wf.workflow_statuses
                .filter(status__category='done')
                .order_by('position')
                .values_list('status__slug', flat=True)
                .first()
            )
            if done_status:
                return done_status
    except Exception:
        pass
    try:
        from .models import Status
        for slug in ('resolved', 'done', 'completed', 'closed'):
            if Status.objects.filter(slug=slug).exists():
                return slug
    except Exception:
        pass
    return 'resolved'

def _is_terminal_status(status) -> bool:
    """True if `status` is a terminal status — the legacy enum set OR any custom
    workflow status whose Status.category is 'done'."""
    if not status:
        return False
    if status in _TERMINAL_ENUM_SLUGS:
        return True
    try:
        from .models import Status
        return Status.objects.filter(slug=status, category='done').exists()
    except Exception:
        return False


# PLAT-9181/9182: finding-identity dedup for the rebalance capacity-pull.
# The 09-18 incident: capacity rebalance re-pulled already-triaged duplicate
# findings (PLAT-9143/9144 — the same CVE re-filed) onto a security seat,
# burning two redundant disposition cycles. The pull had no notion of
# "this finding identity already has a record".
_FINDING_IDENTITY_LABEL_KEYS = ('cve', 'pkg', 'repo')


def _item_label_strings(item) -> list[str]:
    """Flatten an Item's labels (dual shape: task string-array or alert dict)
    plus its comma-separated tags into a list of 'key:value' strings."""
    out: list[str] = []
    labels = getattr(item, 'labels', None)
    if isinstance(labels, dict):
        for k, v in labels.items():
            if isinstance(v, list):
                out.extend(str(x) for x in v if isinstance(x, str))
            elif v is not None:
                out.append(f'{k}:{v}')
    elif isinstance(labels, list):
        out.extend(str(x) for x in labels if isinstance(x, str))
    tags = getattr(item, 'tags', None)
    if tags:
        out.extend(t.strip() for t in str(tags).split(',') if t.strip())
    return out


def _finding_identity_from_item(item) -> tuple[str, str, str] | None:
    """Extract the finding identity (repo, package, cve) from an item's
    labels/tags convention (`cve:<id>`, `pkg:<name>`, `repo:<slug>`).

    Returns None unless a CVE token is present — the CVE is what makes a
    task a FINDING identity; items without one are not dedup candidates
    (fail-open: the validator must never block a legitimate non-finding
    pull)."""
    cve = pkg = repo = ''
    for blob in _item_label_strings(item):
        key, _, val = blob.partition(':')
        key = key.strip().lower()
        val = val.strip()
        if key == 'cve' and not cve:
            cve = val
        elif key == 'pkg' and not pkg:
            pkg = val
        elif key == 'repo' and not repo:
            repo = val
    if not cve:
        return None
    return (repo, pkg, cve)


def _finding_identity_has_record(item, identity) -> bool:
    """True if ANOTHER item with the same finding identity (repo+package+CVE)
    already has an active or terminal record — i.e. any same-org item whose
    status is not 'backlog' (backlog = owned-but-not-active roadmap, not a
    triage record). The candidate itself is excluded. Identity comparison is
    on the CVE (the discriminating token) plus repo/pkg when both sides
    carry them."""
    from .models import Item

    repo, pkg, cve = identity
    if not cve:
        return False
    try:
        # Org scope rides the project (Item has no direct org field; Project
        # carries org_id as a reference to the shizuha-admin organization).
        org_id = Item._project_organization_id(item.project_id)
        base = Item.objects.exclude(pk=item.pk)
        if org_id is not None:
            base = base.filter(project__org_id=org_id)
        # Prefilter on the CVE token (labels::text / tags cast), then verify
        # the exact identity in Python — the GIN-indexed exact-JSON lookups
        # cannot express the dual label shape.
        # Cast the JSON labels to text for the prefilter — portable across
        # Postgres (prod) and SQLite (test harness); the GIN index cannot
        # express the dual label shape anyway, and this query runs once per
        # pulled finding item, not per tick.
        candidates = (
            base.annotate(labels_text=Cast('labels', TextField()))
            .filter(Q(labels_text__icontains=cve) | Q(tags__icontains=cve))
            .only('pk', 'labels', 'tags', 'status')
        )
        for other in candidates:
            if str(other.status or '').strip().lower() == 'backlog':
                continue
            other_identity = _finding_identity_from_item(other)
            if other_identity is None:
                continue
            o_repo, o_pkg, o_cve = other_identity
            if o_cve.upper() != cve.upper():
                continue
            # CVE matches; repo/pkg narrow the identity when both sides carry them.
            if repo and o_repo and repo != o_repo:
                continue
            if pkg and o_pkg and pkg != o_pkg:
                continue
            return True
    except Exception as exc:
        # Fail-open: a dedup-guard lookup error must never break the
        # rebalance pull (the guard exists to save disposition cycles, not
        # to gate routing).
        logger.warning('plat9181: finding-identity dedup lookup failed; failing open: %r', exc)
        return False
    return False


_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE = 'pulse.zero_member_team_detector'
_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE_ID = (
    'zero-member-team-detector-failure'
)
_ZERO_MEMBER_TEAM_DETECTOR_INCIDENT_LOCK = (
    f'{_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE}:'
    f'{_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE_ID}'
)


def _lock_zero_member_team_detector_incident():
    """Serialize the detector-wide incident even before its first row exists.

    ``select_for_update()`` cannot serialize two first writers when both see an
    empty active-alert queryset.  The stable detector identity is nevertheless
    durable, so PostgreSQL can lock it transactionally without requiring a
    pre-existing row.  Every create/re-fire/recovery path takes this lock before
    touching the active incident.
    """
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            [_ZERO_MEMBER_TEAM_DETECTOR_INCIDENT_LOCK],
        )


def _zero_member_team_signal_org_id(signal):
    """Return the exact organization encoded by a detector signal, if any."""
    signal_parts = str(signal or '').strip().split(':', 2)
    if len(signal_parts) != 3 or signal_parts[0] != 'zero-member-team':
        return None
    try:
        return int(signal_parts[1])
    except (TypeError, ValueError):
        return None


def _zero_member_team_detector_alert_project_id(payload):
    """Resolve a real project boundary for Engineering alert delivery.

    Alert recipient resolution is intentionally exact-org and therefore cannot
    operate on a projectless Item.  Once the signal contains an organization,
    every candidate (including an explicit caller-supplied project) is fenced
    to that organization before it may become routing authority.  The global
    platform/first-project fallback is reserved for detector-wide failures that
    occur before an exact organization can be read.
    """
    from .models import Item, Project

    signal = str(payload.get('signal') or '').strip()
    org_id = _zero_member_team_signal_org_id(signal)
    explicit = payload.get('project_id')

    if org_id is not None:
        if explicit:
            project_id = (
                Project.objects.filter(
                    pk=explicit,
                    org_id=org_id,
                    is_active=True,
                    is_archived=False,
                ).values_list('id', flat=True).first()
            )
            if project_id:
                return project_id

        item_key = str(payload.get('item_key') or '').strip()
        if item_key:
            project_id = (
                Item.objects.filter(
                    item_key=item_key,
                    project__org_id=org_id,
                    project__is_active=True,
                    project__is_archived=False,
                ).exclude(project__isnull=True)
                .values_list('project_id', flat=True).first()
            )
            if project_id:
                return project_id

        project_id = (
            Item.objects.filter(
                metadata__org_health_signal=signal,
                project__org_id=org_id,
                project__is_active=True,
                project__is_archived=False,
            ).exclude(project__isnull=True)
            .order_by('created_at', 'id')
            .values_list('project_id', flat=True).first()
        )
        if project_id:
            return project_id
        # A PLAT key is unique only inside an organization.  Never let the
        # first global PLAT row become another tenant's routing boundary.
        return (
            Project.objects.filter(
                org_id=org_id,
                key='PLAT',
                is_active=True,
                is_archived=False,
            ).order_by('id').values_list('id', flat=True).first()
            or Project.objects.filter(
                org_id=org_id,
                is_active=True,
                is_archived=False,
            ).order_by('id').values_list('id', flat=True).first()
        )

    # Unknown-scope failures have no exact tenant authority yet.  Preserve the
    # detector-wide fallback, while still preferring concrete caller context.
    if explicit:
        project_id = (
            Project.objects.filter(pk=explicit, is_active=True, is_archived=False)
            .values_list('id', flat=True).first()
        )
        if project_id:
            return project_id

    item_key = str(payload.get('item_key') or '').strip()
    if item_key:
        project_id = (
            Item.objects.filter(item_key=item_key, project__isnull=False)
            .values_list('project_id', flat=True).first()
        )
        if project_id:
            return project_id

    return (
        Project.objects.filter(
            key='PLAT', is_active=True, is_archived=False,
        ).order_by('id').values_list('id', flat=True).first()
        or Project.objects.filter(
            is_active=True, is_archived=False,
        ).order_by('id').values_list('id', flat=True).first()
    )


def _record_zero_member_team_detector_health(
    *,
    state,
    signal='',
    trigger='',
    item_key='',
    project_id=None,
    error='',
    complete_scope=False,
):
    """Persist one fail-loud outcome for the zero-member detector.

    The heartbeat is deliberately success-only: a failed event or sweep must
    not refresh the timestamp and make the mechanism look healthy.  The latest
    attempt plus latest success/failure remain independently observable through
    ``/statusz`` even when the surrounding assignment/sweep continues.
    """
    from django.core.cache import cache
    from . import liveness

    if isinstance(error, BaseException):
        error_text = f'{type(error).__name__}: {error}'
    else:
        error_text = str(error or '')
    payload = {
        'state': str(state),
        'signal': str(signal or '')[:200],
        'trigger': str(trigger or '')[:100],
        'item_key': str(item_key or '')[:100],
        'project_id': project_id,
        'error': error_text[:500],
        'at': timezone.now().isoformat(),
        'complete_scope': bool(complete_scope),
        'invariant': (
            'each exact zero-member-team signal has at most one non-terminal '
            'carrier across workflow lanes; detector clears never terminalize '
            'a routed implementation carrier'
        ),
    }
    try:
        if payload['state'] == 'success' and payload['complete_scope']:
            payload['last_success_ts'] = liveness.record_heartbeat(
                liveness.ZERO_MEMBER_TEAM_DETECTOR,
            )
        cache.set('pulse:zero_member_team_detector:last', payload, timeout=None)
        cache.set(
            f"pulse:zero_member_team_detector:last_{payload['state']}",
            payload,
            timeout=None,
        )
    except Exception:
        logger.exception('zero-member-team detector health artifact write failed')
    log = logger.warning if payload['state'] != 'success' else logger.info
    log(
        'metric:zero_member_team_detector_health state=%s signal=%s trigger=%s '
        'item_key=%s error=%s',
        payload['state'], payload['signal'], payload['trigger'],
        payload['item_key'], payload['error'],
    )
    return payload


def _notify_zero_member_team_detector_owner(payload):
    """Create one deduplicated Engineering alert and enqueue its owner DM.

    One detector-wide ``source_id`` bounds repeated failures to one incident.
    Only a complete successful sweep may clear it; exact-signal event successes
    are not detector-wide recovery evidence.
    ``send_alert_notification`` resolves the current exact-org Engineering owner
    from the persisted project and delivers through Connect.
    """
    try:
        from .models import Item, Project
        from .services.deduplication import AlertDeduplicationService

        project_id = _zero_member_team_detector_alert_project_id(payload)
        if not project_id:
            logger.error(
                'zero-member-team detector owner alert has no project boundary; '
                'refusing a projectless, undeliverable incident'
            )
            return None
        target_org_id = Project.objects.filter(pk=project_id).values_list(
            'org_id', flat=True,
        ).first()
        if target_org_id is None:
            logger.error(
                'zero-member-team detector owner alert project %s has no '
                'organization boundary; refusing delivery',
                project_id,
            )
            return None
        with transaction.atomic():
            _lock_zero_member_team_detector_incident()
            # Before PLAT-4937 this stable incident could be projectless or
            # bound to the first global PLAT row. Tenant isolation deliberately
            # forbids adopting that row into a different organization. A valid
            # already-scoped incident, however, is the detector-wide owner latch:
            # later failures from another organization coalesce into it without
            # moving tenant-bound payload or sending another initial DM. Retire
            # legacy/mis-scoped rows and any later race duplicates, then create
            # only when no valid canonical incident remains.
            active_incidents = list(Item.objects.select_for_update().filter(
                mode='alert',
                source=_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE,
                source_id=_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE_ID,
                status__in=['firing', 'acknowledged'],
            ).order_by('created_at', 'id'))
            incident_orgs = dict(Project.objects.filter(
                id__in=[
                    incident.project_id for incident in active_incidents
                    if incident.project_id
                ],
                is_active=True,
                is_archived=False,
            ).values_list('id', 'org_id'))
            canonical = None
            for incident in active_incidents:
                incident_org_id = incident_orgs.get(incident.project_id)
                incident_signal_org_id = _zero_member_team_signal_org_id(
                    (incident.payload or {}).get('signal')
                )
                is_same_scope = incident_org_id == target_org_id
                is_valid_latched_scope = (
                    incident_org_id is not None
                    and incident_signal_org_id == incident_org_id
                    and incident.assignment_group
                    and (incident.assignee_id or incident.assignee_email)
                )
                if canonical is None and (
                    is_same_scope or is_valid_latched_scope
                ):
                    canonical = incident
                    continue
                incident.resolve(
                    resolved_by=(
                        'zero-member-team-detector:duplicate-convergence'
                        if canonical is not None
                        else 'zero-member-team-detector:scoped-replacement'
                    ),
                    note=(
                        'Superseded while converging the detector-wide incident '
                        f'to one valid Engineering owner (signal organization '
                        f'{target_org_id}).'
                    ),
                )
            prior_delivery = None
            if canonical is not None:
                prior_delivery = {
                    'project_id': canonical.project_id,
                    'assignment_group': canonical.assignment_group,
                    'assignee_id': canonical.assignee_id,
                    'assignee_email': canonical.assignee_email,
                }
            was_deliverable = bool(
                prior_delivery
                and prior_delivery['project_id']
                and prior_delivery['assignment_group']
                and (
                    prior_delivery['assignee_id']
                    or prior_delivery['assignee_email']
                )
            )
            alert_data = {
                'item_type': 'alert.error',
                'title': 'Pulse zero-member-team detector failed',
                'description': (
                    'The event-primary/periodic zero-member-team detector raised '
                    'before it could reconcile its exact signal carrier. The '
                    'success heartbeat was not refreshed; the surrounding task '
                    'write may continue, but this detector is unhealthy until a '
                    'complete successful sweep resolves this alert.\n\n'
                    f"- signal: `{payload.get('signal') or 'unknown'}`\n"
                    f"- trigger: `{payload.get('trigger') or 'unknown'}`\n"
                    f"- item: `{payload.get('item_key') or 'n/a'}`\n"
                    f"- error: `{payload.get('error') or 'unknown'}`\n"
                    '- owner: `engineering`\n'
                    '- liveness: `/statusz` → '
                    '`zero_member_team_detector_last_success_ts` and '
                    '`zero_member_team_detector_last_failure`'
                ),
                'source': _ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE,
                'source_id': _ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE_ID,
                'severity': 'error',
                'priority': 'urgent',
                'project_id': project_id,
                'assignment_group': 'engineering',
                'labels': {
                    'mechanism': 'zero_member_team_detector',
                    'owner_group': 'engineering',
                    'failure_scope': payload.get('signal') or 'detector-wide',
                },
                'payload': {
                    'state': payload.get('state'),
                    'signal': payload.get('signal'),
                    'trigger': payload.get('trigger'),
                    'item_key': payload.get('item_key'),
                    'project_id': project_id,
                    'error': payload.get('error'),
                    'failed_at': payload.get('at'),
                },
            }
            if (
                canonical is not None
                and incident_orgs.get(canonical.project_id) != target_org_id
            ):
                # The first valid exact-org incident owns this detector-wide
                # outage until a complete sweep recovers it. Do not overwrite
                # its tenant-bound signal/item payload with another tenant's
                # occurrence. The stable incident still records the re-fire.
                canonical.occurrence_count += 1
                canonical.last_fired_at = timezone.now()
                canonical.save(update_fields=[
                    'occurrence_count', 'last_fired_at', 'updated_at',
                ])
                alert, is_new = canonical, False
            else:
                alert, is_new = AlertDeduplicationService.submit_alert(alert_data)
        is_deliverable = bool(
            alert.project_id
            and alert.assignment_group
            and (alert.assignee_id or alert.assignee_email)
        )
        if is_new or (not was_deliverable and is_deliverable):
            # Event-primary detection commonly runs inside the assignment
            # transaction.  Deliver only after the alert row is committed so
            # the worker cannot race an uncommitted/rolled-back incident. A
            # repaired legacy incident gets the one notification it previously
            # could not deliver; later deduplicated repeats stay silent.
            transaction.on_commit(
                lambda alert_id=alert.id: send_alert_notification.delay(alert_id)
            )
        return alert
    except Exception:
        logger.exception('zero-member-team detector owner alert failed')
        return None


def _resolve_zero_member_team_detector_failure_alert(
    *, recovered_before=None,
):
    """Close the global incident after detector-wide sweep recovery evidence."""
    try:
        from .models import Item

        with transaction.atomic():
            _lock_zero_member_team_detector_incident()
            alerts = Item.objects.select_for_update().filter(
                mode='alert',
                source=_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE,
                source_id=_ZERO_MEMBER_TEAM_DETECTOR_ALERT_SOURCE_ID,
                status__in=['firing', 'acknowledged'],
            )
            if recovered_before is not None:
                alerts = alerts.filter(last_fired_at__lte=recovered_before)
            for alert in alerts:
                # Re-check after the row lock is acquired. Under PostgreSQL READ
                # COMMITTED a concurrent re-fire that wins first is re-evaluated
                # against the cutoff; this explicit guard also prevents a future
                # queryset refactor from restoring the stale check-then-save race.
                if (
                    recovered_before is not None
                    and alert.last_fired_at > recovered_before
                ):
                    continue
                alert.resolve(
                    resolved_by='zero-member-team-detector:complete-sweep',
                )
    except Exception:
        logger.exception('zero-member-team detector recovery alert resolve failed')


def _report_zero_member_team_detector_failure(
    *, signal, trigger, item_key='', project_id=None, error,
):
    """Record failure without refreshing liveness, then notify the owner once."""
    try:
        project_id = _zero_member_team_detector_alert_project_id({
            'signal': signal,
            'item_key': item_key,
            'project_id': project_id,
        })
    except Exception:
        # Reporting is invoked while the detector is already failing. A second
        # persistence/routing read failure must remain fail-loud without
        # replacing the original exception or crashing its caller. For an
        # exact signal, fail closed rather than trusting an unvalidated project
        # supplied by a cross-organization sweep.
        logger.exception(
            'zero-member-team detector failure scope resolution failed'
        )
        if _zero_member_team_signal_org_id(signal) is not None:
            project_id = None
    payload = _record_zero_member_team_detector_health(
        state='failure', signal=signal, trigger=trigger,
        item_key=item_key, project_id=project_id, error=error,
    )
    _notify_zero_member_team_detector_owner(payload)
    return payload


def _record_zero_member_team_detector_success(
    *, signal, trigger, item_key='', project_id=None, complete_scope=False,
):
    """Record success and clear only the incident scope it proves recovered."""
    recovered_before = timezone.now()
    payload = _record_zero_member_team_detector_health(
        state='success', signal=signal, trigger=trigger,
        item_key=item_key, project_id=project_id,
        complete_scope=complete_scope,
    )
    if complete_scope:
        transaction.on_commit(
            lambda cutoff=recovered_before:
            _resolve_zero_member_team_detector_failure_alert(
                recovered_before=cutoff,
            )
        )
    return payload


def _reconcile_org_health_signal_carriers(
    signal,
    *,
    signal_active,
    cleared_detail='condition no longer detected',
    preserve_routed_carrier_on_clear=False,
    filing_group='admin-ops',
):
    """Keep one durable carrier per org-health signal and close stale copies.

    Signal tasks move through Review/QA/DevOps after filing, so their current
    ``assignment_group`` is not their identity.  The durable identity is
    ``metadata.org_health_signal`` across every workflow lane.

    When the signal remains active, preserve the oldest active (or intentionally
    deferred) carrier and terminalize any later active duplicates.  When the
    signal clears, terminalize every active carrier with a status valid for its
    own workflow.  Detectors whose carrier becomes an implementation task may
    explicitly preserve the oldest carrier routed away from ``filing_group``;
    that task remains the recurrence sentinel until its owning workflow closes
    it.  Deferred carriers remain untouched because Deferred is an explicit
    operator decision and already suppresses a re-fire.
    """
    from django.db import transaction
    from .models import Comment, Item

    legacy_signal, legacy_organization_id = _legacy_org_health_signal(signal)
    exact_probe = Item.objects.filter(
        mode='task', metadata__org_health_signal=signal,
    )
    try:
        has_exact = exact_probe.exists()
    except AttributeError:
        # A few DB-free detector contract tests replace the manager with a
        # minimal in-memory queryset.  Preserve that seam while keeping the
        # production query lazy and bounded.
        has_exact = bool(exact_probe)
    has_legacy = False
    if legacy_signal is not None:
        legacy_probe = Item.objects.filter(
            mode='task', metadata__org_health_signal=legacy_signal,
            project__org_id=legacy_organization_id,
        )
        try:
            has_legacy = legacy_probe.exists()
        except AttributeError:
            has_legacy = bool(legacy_probe)
    if not has_exact and not has_legacy:
        return {'carrier': None, 'deferred': [], 'resolved': []}

    resolved = []
    with transaction.atomic():
        _adopt_legacy_org_health_signal_carriers(signal)
        carriers = list(
            Item.objects.select_for_update()
            .filter(mode='task', metadata__org_health_signal=signal)
            .order_by('created_at', 'id')
        )
        deferred = [
            item for item in carriers
            if (item.status or '').strip().lower() == 'deferred'
        ]
        active = [
            item for item in carriers
            if (item.status or '').strip().lower() != 'deferred'
            and not _is_terminal_status(item.status)
        ]

        keeper = None
        if signal_active:
            keeper = deferred[0] if deferred else (active[0] if active else None)
            to_resolve = [
                item for item in active
                if keeper is None or item.pk != keeper.pk
            ]
        elif preserve_routed_carrier_on_clear:
            routed_active = [
                item for item in active
                if (item.assignment_group or '').strip().lower()
                != filing_group.strip().lower()
            ]
            keeper = routed_active[0] if routed_active else None
            to_resolve = [
                item for item in active
                if keeper is None or item.pk != keeper.pk
            ]
        else:
            to_resolve = active

        for item in to_resolve:
            if signal_active:
                keeper_key = (
                    getattr(keeper, 'item_key', None)
                    or getattr(keeper, 'pk', 'deferred carrier')
                )
                content = (
                    f'[org-health] duplicate signal carrier for `{signal}` — '
                    f'canonical carrier is **{keeper_key}**; auto-resolving this '
                    'duplicate without clearing the active signal.'
                )
            else:
                if keeper is not None:
                    keeper_key = (
                        getattr(keeper, 'item_key', None)
                        or getattr(keeper, 'pk', 'routed carrier')
                    )
                    content = (
                        f'[org-health] signal cleared — {cleared_detail}; '
                        f'canonical routed carrier is **{keeper_key}**; '
                        'auto-resolving this duplicate.'
                    )
                else:
                    content = (
                        f'[org-health] signal cleared — {cleared_detail}; '
                        'auto-resolving.'
                    )
            Comment.objects.create(
                item=item,
                author_id=0,
                author_email='system@pulse',
                content=content,
            )
            item.status = _terminal_status_for_workflow(item)
            try:
                item._activity_actor = (0, 'system@pulse')
            except Exception:
                pass
            # Status is sufficient. ``resolution`` is a Resolution FK, not the
            # string slug ``fixed``; assigning that string raises before save.
            item.save(update_fields=['status', 'updated_at'])
            resolved.append(item)

    return {
        'carrier': keeper,
        'deferred': deferred,
        'resolved': resolved,
    }


def _legacy_org_health_signal(signal):
    """Return the pre-exact-org signal key and org id for a scoped key.

    PLAT-4829 changed the durable identity from ``family:team`` to
    ``family:organization_id:team``.  Existing carriers must be adopted before
    the first exact-org detector cycle or a Deferred containment sentinel will
    be orphaned and the alert flood will re-arm.
    """
    parts = str(signal or '').split(':', 2)
    if len(parts) != 3:
        return None, None
    family, raw_org_id, team = parts
    if family not in {
        'capacity-starve', 'zero-member-outage', 'zero-member-team',
    } or not raw_org_id.isdigit() or not team:
        return None, None
    return f'{family}:{team}', int(raw_org_id)


def _adopt_legacy_org_health_signal_carriers(signal):
    """Move attributable legacy carriers onto one exact-org signal identity.

    Adoption is intentionally conservative: an unscoped legacy carrier is
    attributable only when its project belongs to the target organization.
    That lets the historical Shizuha/PLAT sentinel suppress the Shizuha lane
    without letting it suppress a same-named team in another organization.
    Status, comments, assignment and every other metadata field are preserved.
    """
    from django.db import transaction
    from .models import Item

    legacy_signal, organization_id = _legacy_org_health_signal(signal)
    if legacy_signal is None:
        return []

    probe = Item.objects.filter(
        mode='task', metadata__org_health_signal=legacy_signal,
        project__org_id=organization_id,
    )
    try:
        has_legacy = probe.exists()
    except AttributeError:
        has_legacy = bool(probe)
    if not has_legacy:
        return []

    adopted = []
    with transaction.atomic():
        legacy = list(
            Item.objects.select_for_update()
            .filter(
                mode='task',
                metadata__org_health_signal=legacy_signal,
                project__org_id=organization_id,
            )
            .order_by('created_at', 'id')
        )
        for item in legacy:
            metadata = dict(item.metadata or {})
            metadata['org_health_signal'] = signal
            metadata.setdefault('org_health_signal_legacy', legacy_signal)
            metadata.setdefault('org_health_signal_organization_id', organization_id)
            item.metadata = metadata
            item.save(update_fields=['metadata', 'updated_at'])
            adopted.append(item)
    return adopted


_STALE_STATUS_TIMER_PF = 'stale_status_timer'


def _json_str_list(value):
    """Normalize workflow-config scalar/list fields to lowercase strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]


def _stale_timer_condition_matches(item, condition) -> bool:
    """Evaluate a generic stale-status timer match block against one item.

    The policy terms (groups, statuses, tags, title/text markers) live in the
    workflow transition JSON; this helper only implements the reusable matching
    mechanism.
    """
    if not isinstance(condition, dict):
        return False

    statuses = _json_str_list(condition.get('statuses') or condition.get('status'))
    if statuses and (item.status or '').strip().lower() not in statuses:
        return False

    groups = _json_str_list(
        condition.get('assignment_groups') or condition.get('assignment_group')
    )
    if groups and (item.assignment_group or '').strip().lower() not in groups:
        return False

    tags_text = (item.tags or '').lower()
    title_text = (item.title or '').lower()
    body_text = f"{title_text}\n{item.description or ''}".lower()

    tags_any = _json_str_list(condition.get('tags_any'))
    if tags_any and not any(marker in tags_text for marker in tags_any):
        return False
    tags_all = _json_str_list(condition.get('tags_all'))
    if tags_all and not all(marker in tags_text for marker in tags_all):
        return False

    title_any = _json_str_list(condition.get('title_markers_any'))
    if title_any and not any(marker in title_text for marker in title_any):
        return False
    title_not = _json_str_list(condition.get('title_markers_not_any'))
    if title_not and any(marker in title_text for marker in title_not):
        return False

    text_any = _json_str_list(condition.get('text_markers_any'))
    if text_any and not any(marker in body_text for marker in text_any):
        return False
    text_not = _json_str_list(condition.get('text_markers_not_any'))
    if text_not and any(marker in body_text for marker in text_not):
        return False

    return True


def _stale_timer_item_matches(item, trigger) -> bool:
    match_any = trigger.get('match_any')
    if isinstance(match_any, list) and match_any:
        return any(_stale_timer_condition_matches(item, cond) for cond in match_any)
    return _stale_timer_condition_matches(item, trigger)


def _stale_timer_hours(trigger) -> float:
    import os

    env_name = (trigger.get('after_hours_env') or '').strip()
    raw = os.environ.get(env_name) if env_name else None
    if raw is None:
        raw = trigger.get('after_hours', trigger.get('hours', 24))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 24.0


def _run_workflow_stale_timers(now=None):
    """Run workflow-configured stale-status timers.

    A timer is a WorkflowTransition containing a post-function object:

        {"type": "stale_status_timer", ...match config...}

    When an item matches that config and has aged past the configured threshold,
    the transition's remaining post-functions are executed in place. This keeps
    organisation-specific escalation policy in workflow data while the engine
    supplies only the generic timer mechanism.
    """
    try:
        from .models import Item, Workflow, WorkflowTransition
        from .services.workflow_service import WorkflowService as W
        from .workflow_json_fields import normalize_json_list_field
    except Exception as e:
        logger.warning(f"workflow stale timers: import failed: {e}")
        return []

    now = now or timezone.now()
    marked = []
    workflow_cache = {}

    def item_workflow_id(item):
        name = (item.workflow_name or '').strip()
        if name:
            cache_key = ('workflow_name', name.lower())
            if cache_key not in workflow_cache:
                workflow_cache[cache_key] = Workflow.resolve(name)
            wf = workflow_cache[cache_key]
            return getattr(wf, 'id', None)
        if item.project_id:
            cache_key = ('project_item', item.project_id, item.item_type or '')
            if cache_key not in workflow_cache:
                try:
                    project_workflow = W.get_workflow_for_project(item.project)
                    wf = (
                        project_workflow.workflow_scheme.get_workflow_for_item_type(item.item_type)
                        if project_workflow and project_workflow.has_workflow_scheme
                        else None
                    )
                except Exception:
                    wf = None
                workflow_cache[cache_key] = wf
            return getattr(workflow_cache[cache_key], 'id', None)
        return None

    transitions = WorkflowTransition.objects.filter(workflow__isnull=False)
    for transition in transitions.iterator():
        pfs = normalize_json_list_field(
            transition.post_functions, 'post_functions',
            item_type='dict', reject_malformed=False,
        )
        triggers = [pf for pf in pfs if pf.get('type') == _STALE_STATUS_TIMER_PF]
        if not triggers:
            continue
        trigger = triggers[0]
        metadata_key = (
            trigger.get('metadata_key')
            or f"stale_status_timer:{transition.id}"
        )
        hours = _stale_timer_hours(trigger)
        age_field = trigger.get('age_field') or 'updated_at'
        if age_field not in ('created_at', 'updated_at', 'due_date'):
            age_field = 'updated_at'

        groups = _json_str_list(
            trigger.get('assignment_groups') or trigger.get('assignment_group')
        )
        statuses = _json_str_list(trigger.get('statuses') or trigger.get('status'))
        qs = Item.objects.filter(mode='task', is_archived=False)
        if groups:
            qs = qs.filter(assignment_group__in=groups)
        if statuses:
            qs = qs.filter(status__in=statuses)
        qs = qs.select_related('project')

        for item in qs.iterator():
            if _is_terminal_status(item.status):
                continue
            if item_workflow_id(item) != transition.workflow_id:
                continue
            metadata = dict(item.metadata or {})
            if metadata.get(metadata_key):
                continue
            if not _stale_timer_item_matches(item, trigger):
                continue
            age_source = getattr(item, age_field, None) or item.updated_at or item.created_at
            try:
                age_h = (now - age_source).total_seconds() / 3600.0
            except Exception:
                continue
            if age_h < hours:
                continue

            try:
                item._original_status = item.status
                item._activity_actor = (0, 'system@pulse')
                metadata[metadata_key] = now.isoformat()
                timer_audit = metadata.get('stale_status_timers')
                if not isinstance(timer_audit, dict):
                    timer_audit = {}
                    metadata['stale_status_timers'] = timer_audit
                timer_audit[str(transition.id)] = {
                    'transition': transition.name,
                    'marked_at': now.isoformat(),
                    'age_hours': round(age_h, 2),
                    'age_field': age_field,
                }
                item.metadata = metadata
                item.save(update_fields=['metadata', 'updated_at'])
                W.execute_post_functions(
                    transition,
                    item,
                    user=None,
                    screen_data={
                        'timer_age_hours': f"{age_h:.0f}",
                        'timer_threshold_hours': f"{hours:.0f}",
                    },
                )
                marked.append(item.item_key or str(item.id))
            except Exception as e:
                logger.warning(
                    "workflow stale timer failed for %s via %s: %s",
                    item.item_key or item.id,
                    transition.id,
                    e,
                )

    if marked:
        logger.warning("workflow stale timers marked=%s", marked)
    return marked


# PLS-204 + PLS-494: reasons for which a DOWN owner is protected from failover-drain
# *when it still has a recent authenticated Pulse heartbeat*. These are soft/stale
# signals a fresh authenticated request contradicts:
#   - provider_down / capacity_limited: bridge up, model provider exhausted (PLS-204).
#   - offline (PLS-494): the exporter/health sample says the bridge is down, but a
#     fresh `pulse:agent_hb` (set on every authenticated Pulse request) proves the
#     owner is demonstrably reaching Pulse — an authenticated request cannot come from
#     a truly-offline process, so `offline` here is stale health, not hard-off.
# NOT included (still drain even while heartbeating): `wedged` — a wedged process
# keeps polling so its heartbeat stays fresh, so the heartbeat does NOT refute the
# zero-work wedge signal; `runtime_unhealthy` — sustained health.ok=false /
# needs_help / tool-LLM death while the process still heartbeats (Agy-class);
# and operator `disabled`/service-denied/DB-inactive, handled by
# _disabled_agent_emails as the deliberate kill-switch. A down owner with NO recent
# heartbeat (hb TTL-expired) is never protected — that is the truly hard-off path.
_SOFT_UNAVAILABILITY_REASONS = frozenset({'provider_down', 'capacity_limited', 'offline'})

# PLAT-11803: the PLAT-999 Path A named co-signer set — the identity-bound
# authority for privileged/cortex merges. Identity-bound cards fail over only
# within this set (sora ↔ aoi); they are never rerouted to a non-co-signer
# because only a set identity can green `privileged-merge-gate / validate`.
# Configurable via settings (PLAT-999 named co-signers) with this default.
_PLAT_999_NAMED_COSIGNERS = frozenset({
    'sora@agents.shizuha.io',
    'aoi@agents.shizuha.io',
})


def _plat999_named_cosigner_emails():
    from django.conf import settings as _s
    configured = getattr(_s, 'PLAT_999_NAMED_COSIGNERS', None)
    if configured:
        try:
            emails = {str(e).strip().lower() for e in configured}
        except TypeError:
            emails = set()
        if emails:
            return emails
    return _PLAT_999_NAMED_COSIGNERS


def _resolve_plat999_cosigner_failover_target(item, health):
    """Pick a co-signer-set failover target for an identity-bound card.

    PLAT-11803 fix contract: failover only within the named co-signer set.
    Candidates are set members other than the current assignee whose lane is
    `active` per the health map. Returns (uid, email) or (None, None) when no
    set member is active — the caller keeps the assignee and raises a wake
    signal instead (never reroutes to a non-co-signer).
    """
    from .user_service import user_service

    def short(email):
        email = (email or '').strip().lower()
        if email.endswith('@agents.shizuha.io'):
            return email[:-len('@agents.shizuha.io')]
        if email.endswith('@shizuha.com'):
            return email[:-len('@shizuha.com')]
        return email.split('@', 1)[0]

    assignee = _canonical_agent_email(item.assignee_email)
    for member in sorted(_plat999_named_cosigner_emails()):
        if _canonical_agent_email(member) == assignee:
            continue
        h = health.get(short(member)) or {}
        # No fail-open: a member absent from the health map (scrape failure)
        # is NOT a failover candidate — rerouting to a nominally-active
        # co-signer on stale health defeats "stays on assignee".
        if h.get('status') != 'active':
            continue
        try:
            row = user_service.get_user_by_email(member)
        except Exception:
            row = None
        uid = int(row.get('id')) if row and str(row.get('id', '')).isdigit() else None
        # Canonical address invariant (PLS-494): stored agent emails are the
        # canonical @shizuha.com form; @agents.shizuha.io is a marker-lane
        # alias only. The handoff stores this value, so the target must fold
        # before Item.save authorizes the assignee against the roster.
        return uid, _canonical_agent_email(member)
    return None, None


PLAT999_COSIGNER_WAKE_REDIS_KEY = 'pulse:plat999_cosigner_wake'
PLAT999_COSIGNER_WAKE_TTL_S = 3600


def _clear_plat999_cosigner_wake():
    """Best-effort delete of the co-signer wake attestation.

    Called when a within-set failover legitimately moves the card: the
    identity it attests to no longer owns a ready identity-bound card, so a
    stale marker must not seat a hibernated co-signer for work she no longer
    holds. A later sweep re-mints if the case recurs.
    """
    try:
        from .services.workflow_service import WorkflowService
        WorkflowService._redis_client().delete(PLAT999_COSIGNER_WAKE_REDIS_KEY)
    except Exception:
        logger.warning('plat999 co-signer wake clear failed', exc_info=True)


def _canonical_agent_email(email):
    # PLS-494: single shared canonicalization (see tasks/agent_liveness.py) so the
    # marker WRITE at the auth boundary and this READ side fold aliases identically.
    from .agent_liveness import canonical_agent_email
    return canonical_agent_email(email)


def _agent_email_aliases_for_heartbeat(email):
    from .agent_liveness import agent_email_aliases
    return agent_email_aliases(email)


def _agent_unavailability_reason(email):
    """Return the reason sweep_agent_availability published for this down agent.

    ``pulse:agent_availability`` is intentionally still a boolean target-eligibility
    map. PLS-204 needs the failover drain to distinguish soft provider/capacity
    limits from hard-off states, so sweep_agent_availability also publishes this
    sidecar map keyed by canonical/alias-folded agent email.
    """
    try:
        import json
        from .services.workflow_service import WorkflowService
        raw = WorkflowService._redis_client().get('pulse:agent_unavailability_reason')
        data = json.loads(raw) if raw else {}
    except Exception:
        return None
    for alias in _agent_email_aliases_for_heartbeat(email):
        reason = data.get(alias)
        if reason:
            return str(reason).strip().lower()
    return None


def _agent_latest_pulse_heartbeat_ts(email):
    """Return the newest authenticated Pulse heartbeat epoch for an identity.

    The exact observation time matters when a bridge reports itself unavailable:
    the auth heartbeat for that *same* request is written before the durable
    ``AgentAvailability(active=False)`` row.  Only a later heartbeat proves the
    runtime subsequently recovered; a boolean "heartbeat exists" cannot order
    those two events and either strands recovered owners or suppresses a genuine
    failover.
    """
    newest = None
    try:
        from .services.workflow_service import WorkflowService
        rc = WorkflowService._redis_client()
        for alias in _agent_email_aliases_for_heartbeat(email):
            raw = rc.get(f'pulse:agent_hb:{alias}')
            if raw is None:
                continue
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode('utf-8', 'replace')
            try:
                ts = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            newest = ts if newest is None else max(newest, ts)
    except Exception:
        return None
    return newest


def _agent_unavailable_seconds(email, now_ts=None):
    """Seconds this agent has been CONTINUOUSLY unavailable, or None if unknown.

    ``sweep_agent_availability`` stamps ``pulse:agent_unavailable_since`` the first
    sweep an agent goes down and clears it the moment it comes back up, so this
    survives the agent's process restarting (unlike the heartbeat, which the
    restart refreshes).
    """
    import json
    import time
    if now_ts is None:
        now_ts = time.time()
    try:
        from .services.workflow_service import WorkflowService
        raw = WorkflowService._redis_client().get('pulse:agent_unavailable_since')
        data = json.loads(raw) if raw else {}
    except Exception:
        return None
    for alias in _agent_email_aliases_for_heartbeat(email):
        since = data.get(alias)
        if since:
            try:
                return max(0.0, now_ts - float(since))
            except (TypeError, ValueError):
                continue
    return None


def _soft_protection_window_exhausted(email, now_ts=None):
    """True once a soft-unavailable owner has been down too long to keep its work.

    PLS-204's heartbeat grace exists to stop a STALE health sample draining a
    demonstrably-live agent. It was never meant to pin work forever, but that is
    what it did: a provider-exhausted agent that Hive restarts every few minutes
    re-issues one authenticated Pulse request per start, refreshing
    ``pulse:agent_hb`` and resetting the 20-minute grace on every cycle. Observed
    2026-07-27 -- kai (``provider_down``, Codex) flapped up/down ~10x in an hour
    and held 54 items (1 open + 53 in_progress) that could never drain, while two
    healthy Engineering peers had capacity.

    A heartbeat proves the process REACHED Pulse. It does not prove the agent can
    do work. So the grace is now bounded by how long the agent has been
    continuously unavailable: a brief provider blip stays protected, a sustained
    or flapping outage releases its work.
    """
    import os
    try:
        cap = int(os.environ.get('PULSE_SOFT_UNAVAILABLE_PROTECT_MAX_SEC', '900'))
    except (TypeError, ValueError):
        cap = 900
    if cap <= 0:
        return False
    down_for = _agent_unavailable_seconds(email, now_ts=now_ts)
    if down_for is None:
        # No stamp yet (first sweep after deploy, or Redis lost it): keep the
        # pre-existing protection rather than broadening a drain on missing data.
        return False
    return down_for >= cap


def _agent_has_recent_pulse_heartbeat(email, now_ts=None, grace_seconds=None):
    """True when the agent made an authenticated Pulse request recently."""
    import os
    import time
    if now_ts is None:
        now_ts = time.time()
    if grace_seconds is None:
        try:
            grace_seconds = int(os.environ.get('PULSE_FAILOVER_DRAIN_GRACE_SEC', '1200'))
        except (TypeError, ValueError):
            grace_seconds = 1200
    if grace_seconds <= 0:
        return False
    ts = _agent_latest_pulse_heartbeat_ts(email)
    return ts is not None and 0 <= now_ts - ts <= grace_seconds


def _soft_unavailable_owner_protected(email):
    # A durable inactive row is an explicit availability event, not a stale
    # health sample.  Claude bridges publish this on confirmed token-pool
    # exhaustion.  It must win over the Redis ``provider_down`` sidecar so
    # already-owned work drains immediately; otherwise the generic heartbeat
    # grace strands work for up to 20 minutes after the bridge has declared it
    # cannot serve another turn.  Telemetry-only provider samples still retain
    # the PLS-204 false-drain protection below.
    try:
        import json
        from .services.workflow_service import WorkflowService
        aliases = WorkflowService._agent_email_aliases(email)
        from .models import AgentAvailability
        inactive = list(
            AgentAvailability.objects
            .filter(email__in=aliases, active=False)
            # PLS-742: ONE normalised actor comparison (strip+lower via iregex)
            # so `Health-Sweep`/`HEALTH-SWEEP ` rows are excluded here exactly
            # like the canonical `health-sweep` stamp.
            .exclude(AgentAvailability.actor_q(AgentAvailability.ACTOR_HEALTH_SWEEP))
            .only('updated_at', 'updated_by')
        )
        if inactive:
            # Operator intent is a hard stop.  For bridge/automation-owned rows,
            # preserve the immediate self-unavailable drain, but let a *later*
            # authenticated request prove recovery.  The self-unavailable request's
            # own auth marker is older than the row, so it cannot satisfy this gate.
            if any(
                AgentAvailability.norm_actor(row.updated_by)
                == AgentAvailability.ACTOR_OPERATOR
                for row in inactive
            ):
                return False
            latest_hb = _agent_latest_pulse_heartbeat_ts(email)
            latest_inactive = max(
                row.updated_at.timestamp()
                for row in inactive
                if row.updated_at is not None
            )
            if latest_hb is None or latest_hb <= latest_inactive:
                return False
        # Do not call the aggregate ``_disabled_agent_emails`` here: it unions
        # the self-inactive rows above and would discard the event ordering we
        # just established.  The remaining hard sources stay explicit.
        raw_disabled = WorkflowService._redis_client().get(
            WorkflowService._AGENT_DISABLED_KEY
        )
        runtime_disabled = {
            str(value).strip().lower()
            for value in (json.loads(raw_disabled) if raw_disabled else [])
            if str(value).strip()
        }
        if aliases & runtime_disabled:
            return False
        raw_hibernated = WorkflowService._redis_client().get(
            WorkflowService._AGENT_HIBERNATED_KEY
        )
        runtime_hibernated = {
            str(value).strip().lower()
            for value in (json.loads(raw_hibernated) if raw_hibernated else [])
            if str(value).strip()
        }
        if aliases & runtime_hibernated:
            return False
        if aliases & WorkflowService._service_denied_agent_emails():
            return False
        forced_inactive = WorkflowService._configured_email_set(
            'PULSE_INACTIVE_AGENT_EMAILS',
            'PULSE_ABSENT_AGENT_EMAILS',
            'PULSE_UNAVAILABLE_AGENT_EMAILS',
        )
        if aliases & forced_inactive:
            return False
        active_allowlist = WorkflowService._configured_email_set(
            'PULSE_ACTIVE_AGENT_EMAILS'
        )
        if active_allowlist and not (aliases & active_allowlist):
            return False
    except Exception:
        # Preserve the existing fail-safe behavior when durable availability
        # cannot be read: do not broaden a drain from a soft signal alone.
        pass
    reason = _agent_unavailability_reason(email)
    if reason not in _SOFT_UNAVAILABILITY_REASONS:
        return False
    if not _agent_has_recent_pulse_heartbeat(email):
        return False
    # A restart-refreshed heartbeat must not pin work on an agent that has been
    # down for ages -- see _soft_protection_window_exhausted (kai, 2026-07-27).
    return not _soft_protection_window_exhausted(email)


def _agent_unavailability_activity_refutable(email):
    """Whether newer authenticated activity may disprove this source verdict.

    Only the soft/stale reason classes participate in automatic failback.  The
    deliberate kill switches remain absolute: an operator OFF row, the runtime
    disabled set, service denial, and configured inactive/allow-list policy can
    never be overridden merely because the identity can still authenticate.
    Fail closed on an unreadable hard-source boundary; a later explicit healthy
    report can run the same restoration task after those sources recover.
    """
    reason = _agent_unavailability_reason(email)
    if reason not in _SOFT_UNAVAILABILITY_REASONS:
        return False
    try:
        import json

        from .models import AgentAvailability
        from .services.workflow_service import WorkflowService

        aliases = WorkflowService._agent_email_aliases(email)
        # PLS-742: ONE normalised operator actor comparison (strip+lower, same
        # as the Python norm_actor rule) so `Operator`/`OPERATOR ` short-circuit
        # exactly like `operator`. `__iexact` alone is case-insensitive but
        # whitespace-sensitive; actor_q matches both axes.
        if AgentAvailability.objects.filter(
            email__in=aliases,
            active=False,
        ).filter(AgentAvailability.actor_q(AgentAvailability.ACTOR_OPERATOR)).exists():
            return False
        raw_disabled = WorkflowService._redis_client().get(
            WorkflowService._AGENT_DISABLED_KEY
        )
        runtime_disabled = {
            str(value).strip().lower()
            for value in (json.loads(raw_disabled) if raw_disabled else [])
            if str(value).strip()
        }
        if aliases & runtime_disabled:
            return False
        raw_hibernated = WorkflowService._redis_client().get(
            WorkflowService._AGENT_HIBERNATED_KEY
        )
        runtime_hibernated = {
            str(value).strip().lower()
            for value in (json.loads(raw_hibernated) if raw_hibernated else [])
            if str(value).strip()
        }
        if aliases & runtime_hibernated:
            return False
        if aliases & WorkflowService._service_denied_agent_emails():
            return False
        forced_inactive = WorkflowService._configured_email_set(
            'PULSE_INACTIVE_AGENT_EMAILS',
            'PULSE_ABSENT_AGENT_EMAILS',
            'PULSE_UNAVAILABLE_AGENT_EMAILS',
        )
        if aliases & forced_inactive:
            return False
        active_allowlist = WorkflowService._configured_email_set(
            'PULSE_ACTIVE_AGENT_EMAILS'
        )
        if active_allowlist and not (aliases & active_allowlist):
            return False
    except Exception:
        return False
    return True


def _recurring_cycle_state(item):
    # Backwards-compatible import for the PLS-214 backfill command. Runtime
    # mutations live exclusively in recurring_cycle_state.py (PLS-419).
    from .services.recurring_cycle_state import cycle_state
    return cycle_state(item)


def _recurring_owner_team(item):
    meta = item.metadata if isinstance(item.metadata, dict) else {}
    owner = str((meta or {}).get('recurring_owner_team') or '').strip().lower().replace('_', '-')
    return owner


def _workflow_initial_status_slug(item, default='pending'):
    reset_slug = default
    if item.workflow_name:
        try:
            from .models import Workflow
            wf = Workflow.objects.filter(name__iexact=item.workflow_name).first()
            if not wf:
                base = item.workflow_name.lower()
                candidates = {
                    base, base.replace('-', '_'), base.replace('_', '-'),
                    '-'.join(base.split()), '_'.join(base.split()),
                }
                for slug in candidates:
                    wf = Workflow.objects.filter(slug__iexact=slug).first()
                    if wf:
                        break
            if wf and wf.initial_status_id:
                reset_slug = wf.initial_status.slug
        except Exception:
            logger.exception(
                "recurring_cycle: failed to resolve initial status for workflow %r on item %s; falling back to %r",
                item.workflow_name, getattr(item, 'id', '?'), default,
            )
    return reset_slug


def _sync_item_workflow_status(item, slug):
    try:
        from .models import Status
        status = Status.objects.filter(slug=slug).first()
        if status:
            item.workflow_status = status.name
    except Exception:
        pass


def _recurring_cycle_activity(item, action, old_value='', new_value='', field_name='recurring_cycle'):
    from .models import ItemActivity
    ItemActivity.objects.create(
        item=item,
        action=action,
        field_name=field_name,
        old_value=str(old_value or ''),
        new_value=str(new_value or ''),
        user_id=0,
        user_email='scheduler@pulse.shizuha.com',
    )


def _notify_recurring_cycle_team_lead(item, team_name, count):
    if not team_name:
        return False
    try:
        from .services.workflow_service import WorkflowService as W
        from .user_service import user_service
        from tasks.services.connect_origin import system_message_headers
        import requests as req
        lead_email = W._resolve_team_lead_email(team_name, item=item)
        if not lead_email:
            return False
        user = user_service.get_user_by_email(lead_email)
        user_id = user.get('id') if user else None
        if not user_id:
            return False
        item_key = item.item_key or f'#{item.id}'
        from .services.recurring_cycle_state import notification_state_line
        content = (
            f"[Recurring Task Stale] {item_key}: {item.title}\n"
            f"Missed {count} consecutive recurring periods while still {item.status}.\n"
            f"Team: {team_name} | Assignee: {item.assignee_email or item.assignee_id or 'unassigned'}\n"
            f"{notification_state_line(item)}\n"
            f"Use pulse_get_task(\"{item_key}\") and unblock, reroute, or close the cycle."
        )
        connect_url = f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/"
        body = {
            'recipient_user_id': user_id,
            'content': content,
            'sender_name': 'Pulse Scheduler',
            'sender_service': 'shizuha-pulse',
        }
        req.post(connect_url, json=body, headers=system_message_headers(body), timeout=5)
        return True
    except Exception as exc:
        logger.warning("recurring_cycle: lead notification failed for %s team=%s: %s", getattr(item, 'id', '?'), team_name, exc)
        return False


def _notify_recurring_assignment_andon(item, team_name, attempts):
    """Send the bounded no-seat ANDON to the cadence owner's team lead."""
    if not team_name:
        return False
    try:
        from .services.workflow_service import WorkflowService as W
        from .user_service import user_service
        from tasks.services.connect_origin import system_message_headers
        import requests as req

        lead_email = W._resolve_team_lead_email(team_name, item=item)
        user = user_service.get_user_by_email(lead_email) if lead_email else None
        user_id = user.get('id') if user else None
        if not user_id:
            return False
        item_key = item.item_key or f'#{item.id}'
        content = (
            f"[Recurring Assignment ANDON] {item_key}: {item.title}\n"
            f"No compatible concrete seat resolved for team {team_name} after "
            f"{attempts} bounded attempts.\n"
            "The cadence remains group-queued in Scheduled with a live next-boundary "
            "timer; restore a compatible team seat before that boundary.\n"
            f'Use pulse_get_task("{item_key}") for the invariant evidence.'
        )
        body = {
            'recipient_user_id': user_id,
            'content': content,
            'sender_name': 'Pulse Scheduler',
            'sender_service': 'shizuha-pulse',
        }
        response = req.post(
            f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/",
            json=body, headers=system_message_headers(body), timeout=5,
        )
        return 200 <= getattr(response, 'status_code', 0) < 300
    except Exception as exc:
        logger.warning(
            "recurring_assignment_andon: lead notification failed item=%s team=%s: %s",
            getattr(item, 'id', '?'), team_name, exc,
        )
        return False


def send_notification(message, recipient_id, sender_id=None, metadata=None):
    """Helper function to send notification via shizuha-id internal push."""
    if not getattr(settings, 'AUTO_SEND_REMINDER', False):
        return False

    try:
        # Default metadata if not provided
        if metadata is None:
            metadata = {
                "importance": "high",
                "type": "task_reminder",
            }

        payload = {
            "user_id": recipient_id,
            "title": "Task Reminder",
            "body": message,
            "data": {
                "type": metadata.get("type", "task_reminder"),
                "screen": "PulseTab",
                "sender_id": sender_id,
                **metadata,
            },
            "priority": "high",
            "channel_id": "pulse_alerts",
        }

        response = requests.post(
            f"{settings.SHIZUHA_ID_API_URL}/internal/push/send/",
            json=payload,
            headers={
                "X-Internal-Service": "shizuha-pulse",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"Notification sent to user {recipient_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send notification: {str(e)}")
        return False

@shared_task(bind=True, max_retries=3)
def send_task_assignment_notification(self, task_id):
    """
    Send assignment notification for a newly created task.
    This runs asynchronously to avoid blocking task creation.
    """
    from .models import Item
    from .serializers import ItemSerializer

    try:
        task = Item.objects.get(id=task_id, mode='task')

        if not task.assignee_id:
            logger.warning(f"Task {task_id} has no assignee, skipping assignment notification")
            return f"Task {task_id} has no assignee"

        # PLAT-337: defense-in-depth — never fire an assignment notification for a
        # task that is already terminal (a replayed/stale dispatch must not spam
        # '[Task Assigned]' for a done task).
        if _is_terminal_status(task.status):
            logger.info(
                f"PLAT-337: task {task_id} is terminal ({task.status}); skipping assignment notification"
            )
            return f"Task {task_id} is terminal ({task.status}); skipping"

        task_data = ItemSerializer(task).data
        notification_sent = user_service.send_task_notification(
            task_data=task_data,
            notification_type="assignment"
        )

        if notification_sent:
            logger.info(f"Assignment notification sent for task {task_id} to user {task.assignee_id}")

        # Send via Connect so agents receive it in real-time
        try:
            import requests as req
            from tasks.services.connect_origin import system_message_headers
            item_key = getattr(task, 'item_key', f'#{task_id}')
            title = getattr(task, 'title', 'Untitled task')
            priority = getattr(task, 'priority', 'normal')
            status = getattr(task, 'status', 'pending')
            recurring_line = ''
            if getattr(task, 'is_recurring', False):
                from tasks.services.recurring_cycle_state import notification_state_line
                recurring_line = f'\n{notification_state_line(task)}'
            connect_base_url = getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')
            _sm_body = {
                'recipient_user_id': task.assignee_id,
                'content': f'[Task Assigned] {item_key}: {title}\nPriority: {priority} | Status: {status}{recurring_line}\nUse pulse_get_task("{item_key}") for full details.',
                'sender_name': 'Pulse',
                'sender_service': 'shizuha-pulse',
            }
            req.post(f'{connect_base_url}/internal/system-message/', json=_sm_body,
                     headers=system_message_headers(_sm_body), timeout=5)
            logger.info(f"Connect assignment message sent for task {task_id} to user {task.assignee_id}")
        except Exception as e:
            logger.warning(f"Failed to send Connect assignment for task {task_id}: {e}")

        return f"Sent assignment notification for task: {task.title}"

    except Item.DoesNotExist:
        return f"Task {task_id} not found"
    except Exception as exc:
        logger.error(f"Error sending assignment notification for task {task_id}: {str(exc)}")
        raise self.retry(exc=exc, countdown=5)


@shared_task(bind=True, max_retries=3)
def send_reminder(self, task_id):
    from .models import Task  # Import here to avoid circular import
    from .user_service import user_service
    from .serializers import TaskSerializer
    from .services.notifications import notification_service

    try:
        task = Task.objects.get(id=task_id, reminder_sent=False)

        # Check if task has an assignee
        if not task.assignee_id:
            logger.warning(f"Task {task_id} has no assignee, skipping reminder")
            return f"Task {task_id} has no assignee, reminder skipped"

        logger.info(f"Sending reminder for task {task_id} to user {task.assignee_id}")

        # Serialize task data for notification
        task_data = TaskSerializer(task).data

        # Send reminder notification using user_service (shizuha-id internal push)
        notification_sent = user_service.send_task_notification(
            task_data=task_data,
            notification_type="reminder"
        )

        if notification_sent:
            logger.info(f"Reminder notification sent for task {task_id} to user {task.assignee_id}")

        # Send to user's configured notification channels (Discord, Telegram)
        channel_results = notification_service.notify_task_reminder(task)
        if channel_results['success'] > 0:
            logger.info(f"Sent reminder to {channel_results['success']} user channels for task {task_id}")
        if channel_results['failed'] > 0:
            logger.warning(f"Failed to send reminder to {channel_results['failed']} user channels for task {task_id}")

        # Send via Connect (unified messaging) so agents receive it in real-time
        try:
            import requests as req
            from tasks.services.connect_origin import system_message_headers
            connect_url = f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/"
            item_key = getattr(task, 'item_key', f'#{task_id}')
            title = getattr(task, 'title', 'Untitled task')
            priority = getattr(task, 'priority', 'normal')
            msg_content = (
                f"[Task Due] {item_key}: {title}\n"
                f"Priority: {priority} | Status: {task.status}\n"
                f"Use pulse_get_task(\"{item_key}\") for full details."
            )
            _sm_body = {
                'recipient_user_id': task.assignee_id,
                'content': msg_content,
                'sender_name': 'Pulse Scheduler',
                'sender_service': 'shizuha-pulse',
            }
            req.post(connect_url, json=_sm_body,
                     headers=system_message_headers(_sm_body), timeout=5)
            logger.info(f"Sent Connect message for task {task_id} to user {task.assignee_id}")
        except Exception as e:
            logger.warning(f"Failed to send Connect message for task {task_id}: {e}")

        task.reminder_sent = True
        task.reminder_count = (task.reminder_count or 0) + 1
        task.last_reminder_sent = timezone.now()
        task.save()
        return f"Sent reminder for task: {task.title}"

    except Task.DoesNotExist:
        return f"Task {task_id} not found or reminder already sent"
    except Exception as exc:
        logger.error(f"Error sending reminder for task {task_id}: {str(exc)}")
        raise self.retry(exc=exc, countdown=5)


def _release_recurring_dispatch_lease(item_id):
    """Release the per-item fire dispatch lease (PLS-1416).

    The dispatcher takes a SETNX lease before publishing fire_recurring_due;
    this task releases it when it exits so the next boundary re-arms promptly
    after a completed fire instead of waiting out the lease TTL. Best-effort:
    a release failure only means the next pop waits one lease — never silent.
    """
    try:
        from .scheduler import KEY_DISPATCH_LEASE_PREFIX
        import redis as _redis
        client = _redis.Redis.from_url(
            settings.CELERY_BROKER_URL, decode_responses=True,
        )
        client.delete(f"{KEY_DISPATCH_LEASE_PREFIX}:{int(item_id)}")
    except Exception as exc:
        logger.debug('dispatch lease release failed for item %s: %s', item_id, exc)


@shared_task(bind=True, max_retries=3)
def send_evidence_blocker_escalation(self, item_id, expected_generation):
    """Deliver one owner/CoS escalation for the current evidence generation.

    The Redis timer is only a wake-up.  The Item metadata is the durable CAS:
    stale callbacks, duplicate scheduler dispatches, and Celery retries all
    re-check the current generation and per-recipient delivery ledger.
    """
    import requests as req
    from django.conf import settings
    from django.db import transaction

    from .models import Comment, Item
    from .services.connect_origin import system_message_headers
    from .services.evidence_blocker_escalation import (
        COS_EMAIL,
        ESCALATION_STATE_KEY,
        escalation_snapshot,
        reconcile_evidence_blocker,
    )
    from .user_service import user_service

    try:
        with transaction.atomic():
            item = Item.objects.select_for_update().get(pk=item_id, mode='task')
            metadata = dict(item.metadata or {})
            state = metadata.get(ESCALATION_STATE_KEY) or {}
            snapshot = escalation_snapshot(item)
            if (
                not snapshot
                or snapshot['generation'] != expected_generation
                or state.get('generation') != expected_generation
                or state.get('state') != 'scheduled'
            ):
                transaction.on_commit(lambda: reconcile_evidence_blocker(item_id))
                return f"Stale evidence escalation skipped for item {item_id}"

            owner_email = snapshot['owner_email']
            recipient_emails = sorted({owner_email, COS_EMAIL})
            delivered = {
                str(email).strip().lower()
                for email in (state.get('delivered_recipient_emails') or [])
            }
            age = timezone.now() - item.created_at
            age_hours = max(0.0, age.total_seconds() / 3600.0)
            parent_lines = [
                f"- {row['key']}: {row['title']}" for row in snapshot['parents']
            ]
            content = (
                f"[Evidence Blocker Escalation] {item.item_key or item.pk}: {item.title}\n"
                f"Required evidence: {snapshot['required_evidence']}\n"
                f"Evidence owner: {owner_email}\n"
                f"Age: {age_hours:.1f}h | Due: {item.due_date.isoformat()}\n"
                "Unblocks:\n" + "\n".join(parent_lines) + "\n"
                f"Use pulse_get_task(\"{item.item_key or item.pk}\") for full details."
            )
            connect_url = (
                getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api')
                .rstrip('/') + '/internal/system-message/'
            )

            for email in recipient_emails:
                if email in delivered:
                    continue
                user = user_service.get_user_by_email(email)
                user_id = (user or {}).get('id')
                if not user_id:
                    raise RuntimeError(f"Could not resolve evidence escalation recipient {email}")
                body = {
                    'recipient_user_id': int(user_id),
                    'content': content,
                    'sender_name': 'Pulse Evidence Deadline',
                    'sender_service': 'shizuha-pulse',
                }
                response = req.post(
                    connect_url,
                    json=body,
                    headers=system_message_headers(body),
                    timeout=5,
                )
                response.raise_for_status()
                delivered.add(email)
                state['delivered_recipient_emails'] = sorted(delivered)
                metadata[ESCALATION_STATE_KEY] = state
                item.metadata = metadata
                item.save(update_fields=['metadata', 'updated_at'])

            state['state'] = 'sent'
            state['sent_at'] = timezone.now().isoformat()
            state['delivered_recipient_emails'] = sorted(delivered)
            metadata[ESCALATION_STATE_KEY] = state
            item.metadata = metadata
            item.save(update_fields=['metadata', 'updated_at'])
            # PLS-478 / PLAT-1354: mechanical lifecycle comments mint through
            # the closed factory (typed origin + deduped origin_event_id), not
            # a direct Comment.objects.create.
            from .services.mechanical_comments import (
                deterministic_event_id,
                MechanicalCommentFactory,
            )
            MechanicalCommentFactory.create(
                item=item,
                origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                origin_event='evidence_blocker.escalation_sent',
                template_version=1,
                structured_payload={
                    'generation': str(expected_generation),
                    'delivered_recipients': sorted(delivered),
                    'required_evidence': str(snapshot['required_evidence']),
                },
                origin_event_id=deterministic_event_id(
                    'evidence_blocker.escalation_sent', item, expected_generation,
                ),
            )
        return f"Sent evidence escalation for {item.item_key or item_id}"
    except Item.DoesNotExist:
        return f"Evidence blocker {item_id} not found"
    except Exception as exc:
        logger.error(
            "Evidence escalation failed item=%s generation=%s: %s",
            item_id, expected_generation, exc,
        )
        raise self.retry(exc=exc, countdown=30)


@shared_task(bind=True, max_retries=3)
def fire_recurring_due(self, item_id, expected_cycle_revision=None,
                       expected_due_at=None, expected_generation=None,
                       expected_completion_watermark=None):
    """Consume one recurring timer through the row-locked cycle state machine.

    Scheduler deliveries include the revision + boundary observed when Redis
    popped the timer.  Older queued deliveries therefore become no-ops after a
    completion, manual start, skip, or newer timer fire changes the row.

    PLS-1416: exits through the dispatch-lease release, so at most one fire
    per item is in flight regardless of broker backlog.
    """
    try:
        return _fire_recurring_due_body(
            item_id,
            expected_cycle_revision=expected_cycle_revision,
            expected_due_at=expected_due_at,
            expected_generation=expected_generation,
            expected_completion_watermark=expected_completion_watermark,
        )
    finally:
        _release_recurring_dispatch_lease(item_id)


def _fire_recurring_due_body(item_id, expected_cycle_revision=None,
                             expected_due_at=None, expected_generation=None,
                             expected_completion_watermark=None):
    from .models import Item
    from .scheduler import get_timer_manager
    from .services.recurring_cycle_state import (
        fire_due_cycle, notification_state_line,
    )
    import requests as req

    try:
        item = Item.objects.get(id=item_id, mode='task')
    except Item.DoesNotExist:
        logger.info("fire_recurring_due: item %s not found", item_id)
        return f"Item {item_id} not found"

    try:
        result = fire_due_cycle(
            item_id,
            expected_cycle_revision=expected_cycle_revision,
            expected_due_at=expected_due_at,
            expected_generation=expected_generation,
            expected_completion_watermark=expected_completion_watermark,
        )
    except Item.DoesNotExist:
        return f"Item {item_id} not found"

    item = result.item
    if result.reason == 'not_recurring':
        try:
            get_timer_manager().cancel_recurring(str(item_id))
        except Exception:
            logger.exception("Failed to cancel_recurring for non-recurring item %s", item_id)
        return f"Item {item_id} not recurring"
    if result.reason == 'stopped':
        try:
            get_timer_manager().cancel_recurring(str(item_id))
        except Exception:
            logger.exception("Failed to cancel_recurring for %s", item_id)
        return f"Recurring {item_id} stopped ({item.status})"
    # PLS-528: the routing hold surfaces as a distinct fire outcome. (The
    # no-assignee early-return is gone on main — the assignment-retry/andon
    # path inside fire_due_cycle owns that case now.)
    if result.kind == 'routing_hold':
        # PLAT-9452 defect 2: a HOLD must terminate the delivery-retry chain.
        # The hold's item.save() re-arms the timer via the post_save signal at
        # the still-past due_date, so without an explicit re-arm the popped
        # boundary is instantly re-queued and held fires spin through the
        # retry path (~3/sec observed 09-20). Re-arm at a bounded backoff
        # instead: the hold state is re-evaluated at most once per backoff
        # window, and the item stays timer-covered in pulse:timers:recurring
        # (which also keeps the missing-timer invariant at 0 for held items).
        try:
            get_timer_manager().schedule_recurring(
                str(item_id),
                timezone.now() + timedelta(seconds=ROUTING_HOLD_RETRY_BACKOFF_SECONDS),
            )
        except Exception:
            logger.exception(
                "Failed to backoff re-arm recurring timer for held item %s", item_id,
            )
        logger.warning(
            "fire_recurring_due: recurrence-routing HOLD item=%s reason=%s "
            "boundary=%s revision=%s re_arm_backoff=%ss",
            item_id, result.reason, item.due_date, result.revision,
            ROUTING_HOLD_RETRY_BACKOFF_SECONDS,
        )
        return f"Held recurring {item_id} routing ({result.reason})"
    if result.kind == 'stale':
        if item.due_date:
            # PLAT-9452 defect 2 (stale half): NEVER re-add a recurring timer
            # with a past score. A past score is popped by the next scheduler
            # tick (~100ms), so a persistent stale loop on a held item —
            # hold-save mutates the cycle, next fire goes stale, stale re-adds
            # at the still-past due_date, instant re-pop, repeat — spins
            # dispatch at ~3/sec (observed 09-20). Clamp the re-add score to
            # the bounded backoff window; the DB-driven sweep still owns
            # boundary dispatch, so a legitimate overdue re-fire is delayed
            # by at most one backoff window.
            try:
                re_arm_at = max(
                    item.due_date,
                    timezone.now() + timedelta(seconds=ROUTING_HOLD_RETRY_BACKOFF_SECONDS),
                )
                get_timer_manager().schedule_recurring(str(item_id), re_arm_at)
            except Exception:
                logger.exception("Failed to re-add recurring timer for %s", item_id)
        logger.info(
            "fire_recurring_due: stale no-op item=%s reason=%s revision=%s due=%s",
            item_id, result.reason, result.revision, item.due_date,
        )
        return f"Skipped recurring {item_id} (stale fire: {result.reason})"
    if result.reason == 'no_next_due':
        logger.error("fire_recurring_due: could not compute next due for item %s", item_id)
        return f"Could not compute next due for {item_id}"

    if result.kind in {'assignment_retry', 'assignment_andon'}:
        if result.notify_team:
            _notify_recurring_assignment_andon(
                item, result.notify_team, result.skip_count,
            )
        logger.warning(
            "fire_recurring_due: %s item=%s team=%s attempts=%s next_due=%s revision=%s",
            result.kind, item_id, result.notify_team or item.assignment_group,
            result.skip_count, result.next_due, result.revision,
        )
        return (
            f"Recurring {item_id} waiting for owner ({result.kind}); "
            f"attempts={result.skip_count} next_due={result.next_due.isoformat()}"
        )

    if result.kind == 'scheduled_start':
        item_key = item.item_key or f'#{item_id}'
        msg_content = (
            f"[Recurring Task Due] {item_key}: {item.title}\n"
            f"Priority: {item.priority} | Frequency: {item.frequency} | "
            f"Next boundary: {result.next_due.isoformat()}\n"
            f"{notification_state_line(item)}\n"
            f"Use pulse_get_task(\"{item_key}\") for details. This task was "
            f"previously {result.old_status}; generation {result.generation} is now active."
        )
        try:
            from tasks.services.connect_origin import system_message_headers
            connect_url = f"{getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')}/internal/system-message/"
            body = {
                'recipient_user_id': item.assignee_id,
                'content': msg_content,
                'sender_name': 'Pulse Scheduler',
                'sender_service': 'shizuha-pulse',
            }
            req.post(connect_url, json=body, headers=system_message_headers(body), timeout=5)
        except Exception as exc:
            logger.warning("fire_recurring_due: Connect delivery failed for %s: %s", item_id, exc)
        return f"Fired recurring {item_id} ({result.old_status} -> {result.new_status})"

    if result.notify_team:
        _notify_recurring_cycle_team_lead(item, result.notify_team, result.skip_count)
    logger.info(
        "fire_recurring_due: skipped %s (still %s), due=%s revision=%s skips=%s lifted=%s",
        item_id, item.status, result.next_due, result.revision, result.skip_count, result.lifted,
    )
    if result.lifted:
        return f"Lifted recurring {item_id} from backlog to {item.status}; skip_count={result.skip_count}"
    return f"Skipped recurring {item_id} (still {item.status}); skip_count={result.skip_count}"


@shared_task(bind=True)
def sweep_recurring_owner_team_drift(self, apply=False):
    """PLS-301 dry-run-first healer for recurring owner-team drift.

    Repairs only low-risk recurring items with metadata.recurring_owner_team:
    scheduled/backlog/todo/open/pending future-due waiting work. Ambiguous active
    work (in_progress/in_review/blocked or due-now active items) is refused so a
    human can inspect per-item evidence.
    """
    from .models import Item
    from .services.workflow_service import WorkflowService as W

    now = timezone.now()
    low_risk_statuses = {'scheduled', 'backlog', 'todo', 'open', 'pending'}
    healed, refused, already_ok = [], [], 0

    for item in Item.objects.filter(mode='task', is_recurring=True):
        owner = _recurring_owner_team(item)
        if not owner:
            continue
        drifted = (item.assignment_group or '').strip().lower() != owner
        # PLS-989: an owner team that is retired or zero-member is unroutable
        # (team_unknown hold) even when assignment_group still matches it —
        # treat it as drift so the healer reroutes to a staffed fallback.
        owner_retired = not W._assignable_team_members(owner)
        if not drifted and not owner_retired:
            already_ok += 1
            continue

        status = (item.status or '').strip().lower()
        future_due = bool(item.due_date and item.due_date > now)
        low_risk = status in low_risk_statuses and (future_due or status == 'scheduled')
        if not low_risk:
            refused.append(item.item_key or str(item.id))
            continue

        if apply:
            W._restore_recurring_owner_team(item, save=True)
        healed.append(item.item_key or str(item.id))

    logger.warning(
        "sweep_recurring_owner_team_drift: %s healed=%s refused=%s already_ok=%s",
        "APPLIED" if apply else "DRYRUN", healed, refused, already_ok,
    )
    return {'applied': bool(apply), 'healed': healed, 'refused': refused, 'already_ok': already_ok}


@shared_task(bind=True, max_retries=3)
def send_overdue_notification(self, task_id):
    """
    Send overdue notification for a single task.
    Fires exactly once per due_date value — the overdue_notified flag
    is reset whenever due_date changes (via pre_save signal).
    """
    from .models import Item
    from .services.notifications import notification_service

    try:
        task = Item.objects.get(id=task_id, mode='task')

        # Already notified for this due_date
        if task.overdue_notified:
            logger.debug(f"Task {task_id} already overdue-notified, skipping")
            return f"Task {task_id} already notified for this due_date"

        # Check if task is still pending/in_progress and actually overdue
        if task.status not in ['pending', 'in_progress']:
            logger.info(f"Task {task_id} status is {task.status}, skipping overdue notification")
            return f"Task {task_id} not in pending/in_progress status"

        if not task.is_overdue():
            logger.info(f"Task {task_id} is not overdue yet, skipping notification")
            return f"Task {task_id} is not overdue"

        if not task.assignee_id:
            logger.warning(f"Task {task_id} has no assignee, skipping overdue notification")
            return f"Task {task_id} has no assignee"

        # Send the notification
        results = notification_service.notify_task_overdue(task)

        # Mark as notified regardless of channel success — the in-app
        # notification record is always created by notify_user().
        task.overdue_notified = True
        task.last_reminder_sent = timezone.now()
        task.reminder_count = (task.reminder_count or 0) + 1
        task.save(update_fields=['overdue_notified', 'last_reminder_sent', 'reminder_count'])
        logger.info(f"Sent overdue notification for task {task_id}")

        return f"Sent overdue notification for task: {task.title}"

    except Item.DoesNotExist:
        return f"Task {task_id} not found"
    except Exception as exc:
        logger.error(f"Error sending overdue notification for task {task_id}: {str(exc)}")
        raise self.retry(exc=exc, countdown=5)


@shared_task(bind=True)
def sweep_missed_notifications(self):
    """
    DB sweep fallback — runs every 60s via celery-beat.

    Finds items whose reminder_time or due_date has passed but are NOT in the
    Redis sorted sets (missed by the scheduler, e.g. after a Redis restart
    before bootstrap completes). Re-adds them to Redis with score=now so the
    scheduler picks them up within 100ms. Does NOT send notifications directly
    — single dispatch path through the scheduler loop.

    Also reconciles the recurring timer set (pulse:timers:recurring): recurring
    timers are armed only at scheduler startup bootstrap and on post_save, so a
    timer lost through any bypass of those paths (bulk update, owner cleared to
    the group queue and restored without a save, Redis restart without a
    scheduler restart) would otherwise strand the recurring item silent until
    the next scheduler start. Re-arming the full active-recurring set here makes
    timer coverage self-heal within one beat tick, matching the invariant used
    by the PulseRecurringMissingTimer alert.
    """
    from .models import Item
    from .scheduler import (
        KEY_REMINDER, KEY_DUE, KEY_RECURRING, TERMINAL_STATUSES,
        get_timer_manager,
    )
    import redis as _redis

    now = timezone.now()
    tm = get_timer_manager()
    r = _redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    re_added = 0

    # PLS-1322: each reconcile section is error-isolated. The recurring
    # reconcile is the self-heal behind the PulseRecurringMissingTimer /
    # PulseRecurringGroupQueueMissingTimer alerts; a transient failure in the
    # reminder or overdue sections (e.g. a Redis blip during a rolling
    # deploy) must not abort the beat and strand recurring timers until the
    # next scheduler bootstrap.
    def _reconcile_section(label, fn):
        nonlocal re_added
        try:
            return fn()
        except Exception:
            logger.exception("sweep_missed_notifications: %s reconcile failed; continuing", label)
            return 0

    # Missed reminders: reminder_time <= now, not sent, not in Redis
    def _reconcile_reminders():
        added = 0
        missed_reminders = Item.objects.filter(
            mode='task',
            reminder_sent=False,
            reminder_time__isnull=False,
            reminder_time__lte=now,
            assignee_id__isnull=False,
        ).exclude(
            status__in=TERMINAL_STATUSES,
        ).values_list('pk', flat=True)

        for pk in missed_reminders:
            if r.zscore(KEY_REMINDER, str(pk)) is None:
                tm.schedule_reminder(str(pk), now)
                added += 1
        return added

    re_added += _reconcile_section('reminder', _reconcile_reminders)

    # Missed overdue: due_date <= now, active, not yet notified for this due_date
    def _reconcile_overdue():
        added = 0
        missed_overdue = Item.objects.filter(
            due_date__isnull=False,
            due_date__lte=now,
            assignee_id__isnull=False,
            overdue_notified=False,
            status__in=['pending', 'in_progress'],
        ).values_list('pk', flat=True)

        for pk in missed_overdue:
            if r.zscore(KEY_DUE, str(pk)) is None:
                tm.schedule_due(str(pk), now)
                added += 1
        return added

    re_added += _reconcile_section('overdue', _reconcile_overdue)

    # Missed recurring timers: active recurring items absent from the
    # pulse:timers:recurring zset. Refresh the full active-recurring set every
    # tick (the same shape the recurring-invariants detector uses) so an item
    # that dropped off the recurring timer (post-save bypass, group-queue
    # owner clear/restore, Redis restart without a scheduler restart) is
    # re-armed automatically instead of staying silent until the scheduler
    # restarts — the PulseRecurringMissingTimer condition this sweep clears.
    def _reconcile_recurring():
        added = 0
        from .services.recurring_invariants import _active_recurring_items
        for item in _active_recurring_items():
            if r.zscore(KEY_RECURRING, str(item.pk)) is None:
                if item.due_date:
                    tm.schedule_recurring(str(item.pk), item.due_date)
                else:
                    tm.schedule_recurring(str(item.pk), now)
                added += 1
        return added

    re_added += _reconcile_section('recurring', _reconcile_recurring)

    # PLAT-9452 defect 3: stamp sweep liveness. The PulseRecurringMissingTimer
    # incident showed "beat dispatching every 60s but healer no-op" is only
    # catchable by hand-running the reconcile — this stamp plus the
    # pulse_sweep_last_success_timestamp_seconds gauge makes a dead or no-op
    # sweep visible to the staleness alert instead of the on-call. Per the
    # defect-3 author's ruling it stamps unconditionally on function
    # completion: the gauge's contract is beat liveness ("did the sweep run at
    # all"), not per-section success — section failures stay visible through
    # their own signals (PLS-1322 isolation logging/counts), and a partially
    # degraded sweep must not look dead and page for the wrong reason.
    try:
        from .services.recurring_invariants import SWEEP_LIVENESS_KEY
        r.set(SWEEP_LIVENESS_KEY, str(now.timestamp()))
        # Keep the stamp bounded: if the sweep stops stamping, the key must not
        # outlive the evidence window indefinitely.
        r.expire(SWEEP_LIVENESS_KEY, 3600)
    except Exception:
        logger.exception("Failed to stamp sweep liveness timestamp")

    # PLS-1416: a completed DB sweep is a fresh sweep stamp — record the
    # mechanism-liveness heartbeat here so `stale_sweep_last_run_ts` on
    # `/statusz` advances with every beat tick. Without this write the
    # residue marker kept reading a nil/20h-stale stamp even while the
    # sweep itself was healthy, holding PulseSweepLivenessStale as a
    # permanent page.
    from .liveness import STALE_SWEEP, record_heartbeat as record_liveness
    record_liveness(STALE_SWEEP)

    if re_added > 0:
        logger.info("Sweep re-added %d missed items to Redis timers", re_added)
        return f"Re-added {re_added} missed items to Redis timers"
    return "No missed notifications found"


@shared_task(bind=True)
def reconcile_recurring_timers(self):
    """PLS-915: durable net — re-schedule active recurring items missing from Redis.

    Recurring timers are only (re)created on scheduler startup
    (``SchedulerLoop.bootstrap_timers``) or Item post_save
    (``TimerManager.schedule_item``). Any active recurring item that becomes
    routable through a path that bypasses post_save — a migration ``.update()``,
    a bulk update, a direct DB write, or a Redis flush between scheduler
    restarts — stays absent from ``pulse:timers:recurring`` until the scheduler
    restarts. That drift is what fires the ``PulseRecurringMissingTimer`` alert.

    This periodic sweep reconciles the DB's active recurring set against Redis
    and re-schedules any missing timers, so coverage is self-healing without a
    scheduler restart. It mirrors the exact active-recurring filter used by
    ``bootstrap_timers`` so the two never disagree on what "should be scheduled"
    means. ``schedule_item`` is idempotent and persists due_date fixes via
    ``.update()`` (no signal recursion), so a re-run is a no-op once covered.
    """
    from .models import Item
    from .scheduler import (
        KEY_RECURRING, RECURRING_STOP_STATUSES, get_timer_manager,
    )
    import redis as _redis

    r = _redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    # Singleton guard: beat may enqueue this to several workers; only one may
    # reconcile at a time. Best-effort — a lost lock just means the next run
    # catches anything this one missed.
    if not r.set('pulse:reconcile:recurring:lock', '1', nx=True, ex=300):
        return "Recurring-timer reconciliation already running"

    try:
        tm = get_timer_manager()
        active_qs = Item.objects.filter(
            mode='task',
            is_recurring=True,
        ).filter(
            Q(assignee_id__isnull=False)
            | (Q(assignment_group__isnull=False) & ~Q(assignment_group=''))
            | (
                Q(metadata__recurring_owner_team__isnull=False)
                & ~Q(metadata__recurring_owner_team='')
            ),
        ).exclude(
            frequency__in=('', 'one_time'),
        ).exclude(
            status__in=RECURRING_STOP_STATUSES,
        ).only(
            'id', 'mode', 'is_recurring', 'frequency', 'recurrence_pattern',
            'due_date', 'status', 'assignee_id', 'assignment_group', 'metadata',
        )

        re_scheduled = []
        for item in active_qs.iterator(chunk_size=200):
            try:
                if r.zscore(KEY_RECURRING, str(item.pk)) is None:
                    tm.schedule_item(item)
                    re_scheduled.append(item.pk)
            except Exception:
                logger.exception(
                    "reconcile_recurring_timers: failed to check/schedule item %s",
                    item.pk,
                )

        if re_scheduled:
            logger.warning(
                "reconcile_recurring_timers: re-scheduled %d active recurring "
                "item(s) missing from Redis: %s",
                len(re_scheduled), re_scheduled,
            )
            return f"Re-scheduled {len(re_scheduled)} missing recurring timer(s): {re_scheduled}"
        return "All active recurring items already have Redis timers"
    finally:
        try:
            r.delete('pulse:reconcile:recurring:lock')
        except _redis.RedisError:
            pass


@shared_task(bind=True, max_retries=3)
def send_item_notification(self, item_id, event_type, context):
    """
    Generic dispatcher for item lifecycle notifications.

    event_type: status_changed | completed | assignment_changed |
                priority_changed | comment_added
    context: dict with keys like old_status, new_status, changed_by,
             comment_text, author_name, exclude_user_id, etc.
    """
    from .models import Item
    from .services.notifications import notification_service

    try:
        item = Item.objects.get(id=item_id)
        exclude_user_id = context.get('exclude_user_id')

        if event_type == 'completed':
            notification_service.notify_task_completed(
                item, completed_by=context.get('changed_by', ''),
                exclude_user_id=exclude_user_id,
            )
        elif event_type == 'status_changed':
            # PLS-50: staleness guard — if the item's current status no longer
            # matches new_status from the dispatch context, the Celery task was
            # queued for a transition the item has since moved past. Delivering
            # it would send an out-of-order "[Task Update] In Progress → In Review"
            # message for an item that is now merged/completed/etc. Skip it.
            dispatched_new_status = context.get('new_status', '')
            if dispatched_new_status and dispatched_new_status != item.status:
                logger.info(
                    "PLS-50: skipping stale status_changed for item %s "
                    "(dispatched for %s→%s, current status=%s)",
                    item_id,
                    context.get('old_status', '?'),
                    dispatched_new_status,
                    item.status,
                )
                return f"Stale status_changed skipped for item {item_id} (now {item.status})"
            stale_tuple_reason = _notification_tuple_staleness(item, context)
            if stale_tuple_reason:
                logger.info(
                    "PLS-474: skipping stale status_changed for item %s (%s)",
                    item_id, stale_tuple_reason,
                )
                return (
                    f"Stale status_changed skipped for item {item_id} "
                    f"({stale_tuple_reason})"
                )
            notification_service.notify_status_changed(
                item, old_status=context.get('old_status', ''),
                new_status=context.get('new_status', ''),
                changed_by=context.get('changed_by', ''),
                exclude_user_id=exclude_user_id,
            )
        elif event_type == 'assignment_changed':
            # PLAT-337: never (re-)fire an assignment notification for a task that
            # has reached a terminal state. The dispatcher can replay a stale
            # assignment event that was queued before the task completed; firing it
            # spams '[Task Assigned]' for a done task (CTX-13 was re-assigned 3x
            # after completion). `item` is freshly fetched above, so its status is
            # current — a since-completed task is skipped here. Re-assigning a
            # terminal task is always wrong, unlike 'completed'/'status_changed'
            # which are legitimately fired AT terminal transition.
            if _is_terminal_status(item.status):
                logger.info(
                    "PLAT-337: skipping stale assignment notification for terminal "
                    "item %s (status=%s)", item_id, item.status,
                )
                return f"Skipped assignment notification for terminal item {item_id}"
            # PLAT-1357: reassignment staleness guard. If the item was reassigned to
            # a different agent after this event was queued, the dispatched assignee
            # no longer matches current reality — delivering it would replay an
            # actionable "[Task Assigned]" for an assignment that no longer holds (the
            # reassignment fired its own event for the current assignee). `item` is
            # freshly fetched above, so item.assignee_id is current. Skip + log (the
            # log line is the audit trail) rather than deliver a stale assignment.
            dispatched_assignee = str(context.get('dispatched_assignee_id') or '')
            if dispatched_assignee and dispatched_assignee != str(item.assignee_id or ''):
                logger.info(
                    "PLAT-1357: skipping stale assignment notification for item %s "
                    "(dispatched for assignee %s, current assignee=%s)",
                    item_id, dispatched_assignee, item.assignee_id,
                )
                return (
                    f"Stale assignment skipped for item {item_id} "
                    f"(reassigned to {item.assignee_id})"
                )
            # PLS-275: active tasks can also replay old assignment events after the
            # item has advanced to a later status while the assignee is unchanged
            # (for example needs_triage -> in_review). A matching assignee alone is
            # not enough: delivering the old assignment would still present an
            # actionable stale queue signal. Dispatch records the status at emit
            # time; delivery suppresses if live status no longer matches.
            dispatched_status = str(context.get('dispatched_status') or '')
            if dispatched_status and dispatched_status != str(item.status or ''):
                logger.info(
                    "PLS-275: skipping stale assignment notification for item %s "
                    "(dispatched status=%s, current status=%s, assignee=%s)",
                    item_id, dispatched_status, item.status, item.assignee_id,
                )
                return (
                    f"Stale assignment skipped for item {item_id} "
                    f"(status now {item.status})"
                )
            stale_tuple_reason = _notification_tuple_staleness(item, context)
            if stale_tuple_reason:
                logger.info(
                    "PLS-474: skipping stale assignment notification for item %s (%s)",
                    item_id, stale_tuple_reason,
                )
                return (
                    f"Stale assignment skipped for item {item_id} "
                    f"({stale_tuple_reason})"
                )
            # PLS-555: delivery-time existence guard (positive validation). The
            # disposable Hive rollback create-permission preflight creates then
            # IMMEDIATELY DELETES a task; this event can be fetched above and
            # then lose a race to that delete, so an actionable '[Task Assigned]'
            # DM fires for an item that no longer exists (HIVE-1054 / HIVE-1061
            # each burned an agent turn; pulse_get_task returned 404 at
            # delivery). PLAT-337 (terminal) and PLAT-1357 (reassigned) never
            # covered a hard-deleted row. Re-check existence right before
            # delivery and treat a vanished row (Item.DoesNotExist / 404) as
            # stale — no DM, no turn.
            if not Item.objects.filter(id=item_id).exists():
                logger.info(
                    "PLS-555: skipping stale assignment notification for item %s "
                    "(row deleted before delivery)", item_id,
                )
                return f"Stale assignment skipped for item {item_id} (deleted before delivery)"
            notification_service.notify_assignment_changed(
                item, assigned_by=context.get('changed_by', ''),
                exclude_user_id=exclude_user_id,
            )
        elif event_type == 'priority_changed':
            notification_service.notify_priority_changed(
                item, old_priority=context.get('old_priority', ''),
                new_priority=context.get('new_priority', ''),
                changed_by=context.get('changed_by', ''),
                exclude_user_id=exclude_user_id,
            )
        elif event_type == 'comment_added':
            notification_service.notify_comment_added(
                item, comment_text=context.get('comment_text', ''),
                author_name=context.get('author_name', ''),
                exclude_user_id=exclude_user_id,
            )
        else:
            logger.warning(f"Unknown item notification event_type: {event_type}")
            return f"Unknown event_type: {event_type}"

        return f"Sent {event_type} notification for item {item_id}"

    except Item.DoesNotExist:
        return f"Item {item_id} not found"
    except Exception as exc:
        logger.error(f"Error sending {event_type} notification for item {item_id}: {exc}")
        raise self.retry(exc=exc, countdown=5)


def _notification_tuple_staleness(item, context):
    """Return a reason when a delayed event no longer names the live tuple.

    PLS-50/PLAT-1357 fenced status and assignee independently.  Review bounces
    also need the implementation team and review generation: two consecutive
    rounds can legitimately end on the same status and same implementer, so an
    old event otherwise looks current and replays actionable work.
    """
    if 'dispatched_assignee_id' in context:
        expected_id = str(context.get('dispatched_assignee_id') or '')
        current_id = str(getattr(item, 'assignee_id', '') or '')
        if expected_id != current_id:
            return f"assignee id changed from {expected_id or 'unassigned'} to {current_id or 'unassigned'}"

    expected_email = str(context.get('dispatched_assignee_email') or '').strip().lower()
    current_email = str(getattr(item, 'assignee_email', '') or '').strip().lower()
    if (
        'dispatched_assignee_email' in context
        and expected_email != current_email
    ):
        return (
            f"assignee email changed from {expected_email} to "
            f"{getattr(item, 'assignee_email', '') or 'unassigned'}"
        )

    expected_group = str(context.get('dispatched_assignment_group') or '').strip().lower()
    current_group = str(getattr(item, 'assignment_group', '') or '').strip().lower()
    if (
        'dispatched_assignment_group' in context
        and expected_group != current_group
    ):
        return (
            f"assignment group changed from {expected_group} to "
            f"{getattr(item, 'assignment_group', '') or 'unrouted'}"
        )

    if 'dispatched_return_generation' in context:
        expected_generation = context.get('dispatched_return_generation')
        metadata = getattr(item, 'metadata', None)
        metadata = metadata if isinstance(metadata, dict) else {}
        generation_candidates = [metadata.get('implementation_return_generation')]
        for key in ('implementation_return_context', 'implementation_return_hold'):
            value = metadata.get(key)
            if isinstance(value, dict):
                generation_candidates.append(value.get('review_generation'))
        current_generation = max(
            (
                value for value in generation_candidates
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            ),
            default=0,
        )
        if expected_generation != current_generation:
            return (
                f"return generation changed from {expected_generation} to "
                f"{current_generation}"
            )
    return ''


def _unassigned_alert_recipient_ids(alert):
    """Resolve fallback recipients for unassigned project/group-routed alerts.

    Alertmanager/Grafana webhook alerts are intentionally routed to a project
    and optional assignment_group without assigning every alert to an individual.
    Mobile push still needs concrete users, so use the group assignee when
    resolvable, then documented project-owner fallbacks.
    """
    from .models import ProjectMembership

    recipient_ids = []

    if alert.assignee_id:
        recipient_ids.append(alert.assignee_id)

    if not recipient_ids and alert.assignment_group:
        try:
            from .services.workflow_service import WorkflowService
            from .services.deduplication import AlertDeduplicationService
            user_id, _email = WorkflowService._resolve_team_assignee(
                alert.assignment_group,
                **AlertDeduplicationService._resolver_kwargs_for_alert(alert),
            )
            if user_id:
                recipient_ids.append(user_id)
        except Exception as exc:
            logger.warning(
                "Could not resolve alert assignment_group '%s' for alert %s: %s",
                alert.assignment_group,
                getattr(alert, 'id', None),
                exc,
            )

    project = getattr(alert, 'project', None)
    if project and not recipient_ids:
        for user_id in (project.default_assignee_id, project.lead_id):
            if user_id:
                recipient_ids.append(user_id)

        # Last project-owner fallback: project admins/managers. This covers
        # project-routed alerts when no concrete team lead/assignee is available.
        from .services.permissions import PermissionService
        admin_ids = PermissionService.get_valid_project_memberships(
            project,
            ProjectMembership.objects.filter(
                project=project,
                user_id__isnull=False,
                role='admin',
            ),
        ).values_list('user_id', flat=True)
        recipient_ids.extend(admin_ids)

    return [uid for uid in dict.fromkeys(recipient_ids) if uid]


@shared_task(bind=True)
def send_alert_notification(self, alert_id):
    """Send notification for a new or escalated alert."""
    from .models import Item
    from .services.notifications import notification_service

    try:
        alert = Item.objects.select_related('project').get(id=alert_id, mode='alert')

        recipient_ids = _unassigned_alert_recipient_ids(alert)
        if not recipient_ids:
            logger.warning(
                "Alert %s has no assignee or project/group fallback recipients; skipping notification",
                alert_id,
            )
            return f"Alert {alert_id} has no notification recipients"

        results = notification_service.notify_alert(alert, user_ids=recipient_ids)

        if results.get('success', 0) > 0 or results.get('connect_success', 0) > 0:
            logger.info(
                "Sent alert notification for alert %s to %s channel(s), %s Connect DM(s) across %s recipient(s)",
                alert_id,
                results.get('success', 0),
                results.get('connect_success', 0),
                len(recipient_ids),
            )

        return f"Sent notification for alert: {alert.title}"

    except Item.DoesNotExist:
        return f"Alert {alert_id} not found"
    except Exception as e:
        logger.error(f"Error sending alert notification for alert {alert_id}: {e}")
        raise


def _alertmanager_persist_items() -> bool:
    """Whether Alertmanager may still create mode=alert Items (default: no)."""
    from django.conf import settings
    return bool(getattr(settings, 'PULSE_ALERTMANAGER_PERSIST_ITEMS', False))


def _alert_dm_cache_key(source: str, fingerprint: str) -> str:
    return f"pulse:alert_dm:{source}:{fingerprint or 'unknown'}"


def _alert_dm_class_key(
    source: str,
    labels: dict | None,
    assignment_group: str | None,
    *,
    status: str = 'firing',
    default_assignment_group: str | None = None,
) -> str:
    """One DM per alert class, not per Alertmanager instance fingerprint.

    Per-pod fingerprints (KubePodCrashLooping × N pods) plus a 1h TTL re-paged
    on-call every hour — 1.2k–5k identical in-app notifications/day (Ni uid 89,
    San uid 123, 2026-09-03). Collapse to (source, firing|resolved, alertname,
    severity, team) so a fleet of the same rule is one page until it has been
    quiet longer than the dedup TTL.
    """
    labels = labels or {}
    alertname = str(labels.get('alertname') or 'unknown').strip() or 'unknown'
    severity = str(labels.get('severity') or 'warning').strip().lower() or 'warning'
    group = str(
        assignment_group or default_assignment_group or 'unassigned'
    ).strip().lower() or 'unassigned'
    kind = 'resolved' if str(status or '').strip().lower() == 'resolved' else 'firing'
    return f"pulse:alert_dm_class:{source}:{kind}:{alertname}:{severity}:{group}"


def _alert_resolved_dm_cache_key(source: str, fingerprint: str, starts_at: str) -> str:
    episode = hashlib.sha256(
        f"{fingerprint or 'unknown'}|{starts_at or 'unknown'}".encode('utf-8')
    ).hexdigest()
    return f"pulse:alert_dm_resolved:{source}:{episode}"


def _touch_alert_dm(cache_key: str, ttl: int) -> None:
    """Refresh a held class reservation so a still-firing rule does not re-page."""
    try:
        _alert_dm_redis_client().expire(cache_key, max(60, int(ttl)))
    except Exception:
        logger.exception(
            "Alertmanager DM TTL refresh failed key_digest=%s",
            hashlib.sha256(cache_key.encode('utf-8')).hexdigest()[:16],
        )


_ALERT_DM_RELEASE_IF_OWNER_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _alert_dm_redis_client():
    """Return the shared Redis authority for cross-pod alert DM idempotency."""
    import os
    import redis

    url = (
        os.environ.get('PULSE_REDIS_URL')
        or getattr(settings, 'CELERY_BROKER_URL', None)
        or 'redis://localhost:6379/0'
    )
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _reserve_alert_dm(cache_key: str, token: str, ttl: int):
    """Atomically reserve one firing/recovery episode across every Pulse pod."""
    client = _alert_dm_redis_client()
    try:
        acquired = client.set(cache_key, token, nx=True, ex=max(60, int(ttl)))
    except Exception:
        logger.exception(
            "Alertmanager shared DM reservation failed key_digest=%s",
            hashlib.sha256(cache_key.encode('utf-8')).hexdigest()[:16],
        )
        raise
    return client if acquired else None


def _release_alert_dm_reservation(
    cache_key: str, token: str, *, client=None,
) -> bool:
    """Atomically release only the shared reservation owned by this attempt."""
    client = client or _alert_dm_redis_client()

    try:
        return bool(client.eval(
            _ALERT_DM_RELEASE_IF_OWNER_LUA, 1, cache_key, token,
        ))
    except Exception:
        logger.exception(
            "Alertmanager shared DM reservation release failed key_digest=%s",
            hashlib.sha256(cache_key.encode('utf-8')).hexdigest()[:16],
        )
        return False


def _clear_alert_dm_reservation(cache_key: str) -> bool:
    """Clear a firing tombstone after resolution so a later episode may notify."""
    try:
        return bool(_alert_dm_redis_client().delete(cache_key))
    except Exception:
        logger.exception(
            "Alertmanager shared firing reservation clear failed key_digest=%s",
            hashlib.sha256(cache_key.encode('utf-8')).hexdigest()[:16],
        )
        return False


def _alert_response_class(labels) -> str:
    """Return the explicit Alertmanager response class, if any.

    Missing and unknown values deliberately retain the legacy actionable path.
    Only the exact Git-owned ``diagnostic`` label suppresses action DMs.
    """
    return str((labels or {}).get('response_class') or '').strip().lower()


def _dm_alertmanager_firing(
    webhook_key, *, title, description, severity, labels, payload, source_url,
    source_id, status='firing', dedupe_key=None, dedupe_ttl=None,
    allow_connect_defer=True,
):
    """DM on-call without creating a Pulse Item (operator doctrine).

    Uses cache-key dedup so Alertmanager re-fires (every few minutes) do not
    spam Connect. Dedup is per alert *class* (alertname + severity + team),
    not per instance fingerprint: a crashlooping DaemonSet is one page, not
    one page per pod. On a fully-resolved group we delete the firing class
    key so a true re-fire can DM again.
    """
    from types import SimpleNamespace
    from django.conf import settings
    from .models import Project
    from .services.notifications import _SEVERITY_PRIORITY, notification_service
    from .views import _routed_default_project_id, _label_assignment_group

    # Class key must not wait on Postgres: Alertmanager re-sends every firing
    # instance every few minutes. owner_group/default_assignment_group is the
    # same identity `_label_assignment_group` would pick when the project is
    # registered; using it here keeps the hot path a Redis SET NX.
    group_hint = (labels or {}).get('owner_group') or getattr(
        webhook_key, 'default_assignment_group', None,
    )
    cache_key = dedupe_key or _alert_dm_class_key(
        webhook_key.source_name,
        labels,
        group_hint,
        status=status,
        default_assignment_group=getattr(webhook_key, 'default_assignment_group', None),
    )
    ttl = int(
        dedupe_ttl
        or getattr(settings, 'PULSE_ALERT_DM_DEDUP_SECONDS', 3600)
        or 3600
    )
    reservation = hashlib.sha256(
        f"{cache_key}|{time.time_ns()}".encode('utf-8')
    ).hexdigest()
    # Redis SET NX EX is atomic and shared by every web/Celery pod. Django's
    # default LocMem cache is process-local and allowed duplicate recovery DMs
    # whenever identical webhooks landed on different Pulse pods.
    reservation_client = _reserve_alert_dm(cache_key, reservation, ttl)
    if reservation_client is None:
        _touch_alert_dm(cache_key, ttl)
        return 'deduped'

    try:
        # Reservation ownership covers the entire routing + packet + delivery
        # path. A malformed webhook-key field or routing lookup must be just as
        # retryable as a Connect transport failure.
        project_id = _routed_default_project_id(webhook_key)
        project = Project.objects.filter(id=project_id).first() if project_id else None
        assignment_group = _label_assignment_group(
            labels,
            webhook_key.default_assignment_group,
            project=project,
        )
        packet = SimpleNamespace(
            id=None,
            item_key=None,
            title=title,
            description=description or '',
            severity=severity or 'warning',
            # PLS-706 (@reika): this map was duplicated verbatim from
            # notifications._SEVERITY_PRIORITY — a second instance of the exact
            # convention-coupling this PR names as the bug class. Import the one
            # definition so the two cannot drift.
            priority=_SEVERITY_PRIORITY.get((severity or 'warning').strip().lower(), 'normal'),
            status=status,
            item_type=f'alert.{severity}',
            source=webhook_key.source_name,
            source_id=source_id,
            source_url=source_url or '',
            labels=labels or {},
            payload=payload or {},
            assignment_group=assignment_group,
            assignee_id=None,
            project=project,
            project_id=project_id,
        )
        recipient_ids = _unassigned_alert_recipient_ids(packet)
        if not recipient_ids:
            logger.warning(
                "Alertmanager DM skipped (no recipients) title=%s group=%s source=%s",
                title, assignment_group, webhook_key.source_name,
            )
            _release_alert_dm_reservation(
                cache_key, reservation, client=reservation_client,
            )
            return 'no_recipients'
        results = notification_service.notify_alert(
            packet,
            user_ids=recipient_ids,
            allow_connect_defer=allow_connect_defer,
        )
    except Exception:
        _release_alert_dm_reservation(
            cache_key, reservation, client=reservation_client,
        )
        raise
    connect_ok = results.get('connect_success', 0)
    channels_ok = results.get('success', 0)
    # PLS-706 AC5 (tightened by @mio from live evidence): `notify_alert` already
    # computes `connect_deferred` (notifications.py:1039) and then throws it away,
    # so entering the DM path and DELIVERING on it print identically. Across 5
    # pods / 48 batches, 61 notifies logged `connect=0 channels=0` 49 times while
    # every batch still reported `failed=0` -- ~80% of alerts reaching nobody was
    # indistinguishable from all-clear. Propagate the suppression so the batch
    # summary can discriminate.
    deferred = results.get('connect_deferred', 0)
    logger.info(
        "Alertmanager DM-only notify title=%s recipients=%s connect=%s channels=%s deferred=%s",
        title, len(recipient_ids), connect_ok, channels_ok, deferred,
    )
    # Only a batch that delivered on NO channel is a suppression. A partial
    # deferral (some recipient deferred, another delivered) still reached
    # someone, so it stays 'notified' -- overcounting suppression would trade
    # one blind spot for a false alarm.
    if not connect_ok and not channels_ok and deferred:
        _release_alert_dm_reservation(
            cache_key, reservation, client=reservation_client,
        )
        return 'deferred'
    if not connect_ok and not channels_ok:
        _release_alert_dm_reservation(
            cache_key, reservation, client=reservation_client,
        )
        return 'failed' if results.get('failed', 0) else 'undelivered'
    return 'notified'


def _dm_alertmanager_resolved(
    webhook_key, *, title, description, severity, labels, payload, source_url,
    source_id, starts_at,
):
    """Deliver one recovery DM for one Alertmanager alert episode."""
    ttl = int(getattr(settings, 'PULSE_ALERT_RESOLVED_DM_DEDUP_SECONDS', 604800) or 604800)
    del starts_at  # class-level recovery DMs do not key on instance startsAt
    return _dm_alertmanager_firing(
        webhook_key,
        title=title,
        description=description,
        severity=severity,
        labels=labels,
        payload=payload,
        source_url=source_url,
        source_id=source_id,
        status='resolved',
        dedupe_ttl=ttl,
        # A recovery signal closes the incident loop and must not be hidden
        # merely because the recipient is still working the firing incident.
        allow_connect_defer=False,
    )


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_alertmanager_alerts(self, webhook_key_id, alerts_data, payload_status=''):
    """PLS-86: process an Alertmanager webhook batch OFF the request path.

    The webhook handler validates the key + payload, enqueues this, and returns
    immediately. Worker processing keeps the HTTP path lock-free under bursts.

    Operator doctrine (CLAUDE / ops): **Alerts NEVER create Pulse tasks** — DM the
    on-call lead. Default behaviour is **DM-only** (no ``mode=alert`` Item rows).
    Historical Item create/dedup remains available only when
    ``PULSE_ALERTMANAGER_PERSIST_ITEMS=true`` (break-glass). Resolved webhooks still
    clear any leftover active alert rows and DM-dedup cache keys.
    """
    from .models import WebhookKey, Item, Project
    from .services.deduplication import AlertDeduplicationService, AlertIdentityError
    # Lazy import: views imports tasks, so importing views at module load would be
    # circular; at task-run time views is fully loaded.
    from .views import fit_source_url, _routed_default_project_id, _label_assignment_group

    try:
        webhook_key = WebhookKey.objects.get(id=webhook_key_id, is_active=True)
    except WebhookKey.DoesNotExist:
        logger.warning("process_alertmanager_alerts: webhook key %s missing/inactive", webhook_key_id)
        return "webhook key missing/inactive"

    payload_status = str(payload_status or '').strip().lower()
    severity_map = {'critical': 'critical', 'error': 'error', 'warning': 'warning', 'info': 'info'}
    created_count = deduplicated_count = resolved_count = failed_count = dm_count = 0
    resolved_dm_count = resolved_dm_deduplicated = 0
    no_recipient_count = contract_failed_count = deferred_count = 0
    diagnostic_suppressed_count = 0
    persist = _alertmanager_persist_items()

    for alert in alerts_data or []:
        try:
            # Alertmanager v2/v4 sends a top-level group status and may omit
            # per-alert status on resolved groups; fall back to the group status
            # before defaulting to firing so a resolved group still clears.
            alert_status = str(alert.get('status') or payload_status or 'firing').strip().lower()
            labels = alert.get('labels', {})
            annotations = alert.get('annotations', {})

            title = labels.get('alertname', 'Alertmanager Alert')
            description = annotations.get('description', annotations.get('summary', ''))
            grafana_severity = labels.get('severity', webhook_key.default_severity)
            severity = severity_map.get(str(grafana_severity).lower(), webhook_key.default_severity)
            merged_labels = {**webhook_key.auto_labels, **labels}
            # Suppression authority belongs to the raw, Git-owned
            # Alertmanager rule label.  Webhook defaults/auto-labels are
            # mutable routing metadata and must never manufacture a
            # diagnostic classification.
            is_alertmanager = webhook_key.source_name == 'k3s-alertmanager'
            is_diagnostic = (
                is_alertmanager
                and _alert_response_class(labels) == 'diagnostic'
            )
            source_id = alert.get('fingerprint')

            if alert_status == 'resolved':
                # Clear DM dedup so a later re-fire can notify again.
                if source_id:
                    _clear_alert_dm_reservation(
                        _alert_dm_cache_key(webhook_key.source_name, source_id)
                    )
                if payload_status == 'resolved':
                    # Whole group recovered — drop the firing class reservation
                    # so a later episode of the same alertname can page again.
                    _clear_alert_dm_reservation(
                        _alert_dm_class_key(
                            webhook_key.source_name,
                            merged_labels,
                            merged_labels.get('owner_group')
                            or webhook_key.default_assignment_group,
                            status='firing',
                            default_assignment_group=webhook_key.default_assignment_group,
                        )
                    )
                resolved_outcome = 'diagnostic' if is_diagnostic else (
                    _dm_alertmanager_resolved(
                        webhook_key,
                        title=title,
                        description=description,
                        severity=severity,
                        labels=merged_labels,
                        payload={
                            'generator_url': alert.get('generatorURL'),
                            'starts_at': alert.get('startsAt'),
                            'ends_at': alert.get('endsAt'),
                            'annotations': annotations,
                        },
                        source_url=fit_source_url(alert.get('generatorURL')),
                        source_id=source_id,
                        starts_at=alert.get('startsAt'),
                    )
                )
                if resolved_outcome == 'notified':
                    resolved_dm_count += 1
                elif resolved_outcome == 'deduped':
                    resolved_dm_deduplicated += 1
                elif resolved_outcome == 'no_recipients':
                    no_recipient_count += 1
                elif resolved_outcome == 'deferred':
                    deferred_count += 1
                elif resolved_outcome == 'diagnostic':
                    diagnostic_suppressed_count += 1
                else:
                    failed_count += 1
                # Resolve any leftover active rows from the pre-DM-only era
                # (or break-glass persist mode). Match by source + fingerprint.
                resolve_q = Item.objects.filter(
                    mode='alert',
                    status__in=['firing', 'acknowledged'],
                    source=webhook_key.source_name,
                )
                if source_id:
                    resolve_q = resolve_q.filter(source_id=source_id)
                else:
                    identity_data = {
                        'item_type': f'alert.{severity}',
                        'title': title,
                        'source': webhook_key.source_name,
                        'labels': merged_labels,
                        'payload': {
                            'generator_url': alert.get('generatorURL'),
                            'starts_at': alert.get('startsAt'),
                            'ends_at': alert.get('endsAt'),
                            'annotations': annotations,
                        },
                    }
                    config = AlertDeduplicationService.get_fingerprint_config(webhook_key.source_name)
                    AlertDeduplicationService.validate_identity(identity_data, config)
                    fingerprint = AlertDeduplicationService.compute_fingerprint(
                        item_type=identity_data['item_type'],
                        source=identity_data['source'],
                        labels=identity_data['labels'],
                        payload=identity_data['payload'],
                        config=config,
                    )
                    resolve_q = resolve_q.filter(fingerprint=fingerprint)
                    _clear_alert_dm_reservation(
                        _alert_dm_cache_key(webhook_key.source_name, fingerprint)
                    )
                for resolved in resolve_q:
                    resolved.resolve(resolved_by=f'alertmanager:{webhook_key.source_name}')
                    resolved_count += 1
            else:
                generator_url = alert.get('generatorURL')
                if not persist:
                    outcome = 'diagnostic' if is_diagnostic else (
                        _dm_alertmanager_firing(
                            webhook_key,
                            title=title,
                            description=description,
                            severity=severity,
                            labels=merged_labels,
                            payload={
                                'generator_url': generator_url,
                                'starts_at': alert.get('startsAt'),
                                'ends_at': alert.get('endsAt'),
                                'annotations': annotations,
                            },
                            source_url=fit_source_url(generator_url),
                            source_id=source_id,
                        )
                    )
                    if outcome == 'notified':
                        dm_count += 1
                    elif outcome == 'deferred':
                        # PLS-706 AC5: the DM path ran and delivered on no
                        # channel because every recipient was deferred. Counted
                        # as a DM attempt (it entered the path) AND separately
                        # as a suppression, so a fully-delivered batch and a
                        # fully-suppressed one cannot print the same summary.
                        # The unbounded deferral itself is PLS-715, not this
                        # ticket -- this only makes it visible.
                        dm_count += 1
                        deferred_count += 1
                    elif outcome == 'deduped':
                        deduplicated_count += 1
                    elif outcome == 'no_recipients':
                        # PLS-706 AC4: previously invisible. A batch with no
                        # eligible recipient is a ROUTING outcome, not a
                        # failure, and must not look like one -- but it must
                        # not be silent either, or "nothing was delivered"
                        # reads as success.
                        no_recipient_count += 1
                    elif outcome == 'diagnostic':
                        diagnostic_suppressed_count += 1
                    elif outcome in {'failed', 'undelivered'}:
                        failed_count += 1
                    continue

                routed_project_id = _routed_default_project_id(webhook_key)
                routed_project = (
                    Project.objects.filter(id=routed_project_id).first()
                    if routed_project_id else None
                )
                alert_data = {
                    'item_type': f'alert.{severity}',
                    'title': title,
                    'description': description,
                    'source': webhook_key.source_name,
                    'source_id': source_id,
                    'source_url': fit_source_url(generator_url),
                    'severity': severity,
                    'priority': 'high' if severity in ['critical', 'error'] else 'normal',
                    'labels': merged_labels,
                    'payload': {
                        'generator_url': generator_url,
                        'starts_at': alert.get('startsAt'),
                        'ends_at': alert.get('endsAt'),
                        'annotations': annotations,
                    },
                    'project_id': routed_project_id,
                    'assignment_group': _label_assignment_group(
                        merged_labels,
                        webhook_key.default_assignment_group,
                        project=routed_project,
                    ),
                }
                item, is_new = AlertDeduplicationService.submit_alert(alert_data)
                if is_new:
                    created_count += 1
                    if is_diagnostic:
                        diagnostic_suppressed_count += 1
                    else:
                        send_alert_notification.delay(item.id)
                else:
                    deduplicated_count += 1
        except AlertIdentityError:
            failed_count += 1
            logger.exception(
                "PLS-293 process_alertmanager_alerts: rejected identity-less alert source=%s title=%s labels=%s",
                getattr(webhook_key, 'source_name', None),
                (alert.get('labels') or {}).get('alertname') if isinstance(alert, dict) else None,
                alert.get('labels') if isinstance(alert, dict) else None,
            )
        except (AttributeError, TypeError):
            # PLS-706 AC4: a packet/consumer CONTRACT mismatch is deterministic
            # and will recur on every alert until code changes -- categorically
            # unlike a transient send failure. Counting both as bare `failed=N`
            # is what let 41 drops (5 pods, 21 fingerprints, including
            # AlertmanagerFailedToSendAlerts itself) pass unnoticed while the
            # Celery task reported `succeeded`. Name it so it cannot hide.
            failed_count += 1
            contract_failed_count += 1
            logger.exception(
                "PLS-706 process_alertmanager_alerts: DELIVERY CONTRACT ERROR on alert "
                "fingerprint=%s -- deterministic, will recur until fixed; not transient",
                alert.get('fingerprint') if isinstance(alert, dict) else None,
            )
        except Exception:
            failed_count += 1
            logger.exception(
                "PLS-86 process_alertmanager_alerts: failed on alert fingerprint=%s; continuing",
                alert.get('fingerprint') if isinstance(alert, dict) else None,
            )

    webhook_key.record_usage()
    logger.info(
        "PLS-86 alertmanager batch (key=%s persist=%s): created=%s dm=%s deduped=%s "
        "resolved=%s resolved_dm=%s resolved_dm_deduped=%s diagnostic_suppressed=%s "
        "failed=%s (contract=%s) no_recipients=%s deferred=%s of %s",
        webhook_key_id, persist, created_count, dm_count, deduplicated_count, resolved_count,
        resolved_dm_count, resolved_dm_deduplicated, diagnostic_suppressed_count, failed_count,
        contract_failed_count, no_recipient_count, deferred_count,
        len(alerts_data or []),
    )
    return {
        'created': created_count, 'dm': dm_count, 'deduplicated': deduplicated_count,
        'resolved': resolved_count, 'failed': failed_count,
        'resolved_dm': resolved_dm_count,
        'resolved_dm_deduplicated': resolved_dm_deduplicated,
        'diagnostic_suppressed': diagnostic_suppressed_count,
        'contract_failed': contract_failed_count, 'no_recipients': no_recipient_count,
        'deferred': deferred_count,
        'persist': persist,
    }


@shared_task(bind=True)
def send_action_notification(self, action_id):
    """Send notification for a pending action."""
    from .models import Item
    from .services.notifications import notification_service

    try:
        action = Item.objects.get(id=action_id, mode='action')

        if not action.assignee_id:
            logger.warning(f"Action {action_id} has no assignee, skipping notification")
            return f"Action {action_id} has no assignee"

        results = notification_service.notify_action_pending(action)

        if results['success'] > 0:
            logger.info(f"Sent action notification for action {action_id} to {results['success']} channels")

        return f"Sent notification for action: {action.title}"

    except Item.DoesNotExist:
        return f"Action {action_id} not found"
    except Exception as e:
        logger.error(f"Error sending action notification for action {action_id}: {e}")
        raise


@shared_task(bind=True)
def qa_verification_sweep(self, now=None, force_tiers=None):
    """CVQA-L2 (PLAT-474 / PLAT-552): generate per-project standing QA tasks.

    Runs daily via the 'QA Verification Sweep' Celery beat schedule. On each run
    it generates QA verification tasks (workflow ``autonomous-dev``, team ``qa``,
    priority ``low``) for whichever risk tiers are DUE that day — T1 daily, T2
    weekly (Mon), T3 bi-weekly (Mon of even ISO weeks), T4 monthly (1st) — and
    deduplicates against any cadence task for the same (tier, service) that is
    still open, so busy windows don't pile up duplicates.

    Each service is filed to its owning Pulse project (by key), falling back to
    the Shizuha Platform project with a ``service:<slug>`` label for sub-services
    that have no project of their own (per PLAT-474 §7).

    Args (mainly for tests / manual runs):
      now: override the reference datetime (defaults to ``timezone.now()``).
      force_tiers: iterable of tier keys (e.g. ``['T2']``) to run regardless of
        whether their cadence boundary falls today.
    """
    from datetime import timedelta

    from django.utils import timezone
    from .models import Item, Project
    from . import qa_cadence

    ref = now or timezone.now()
    tiers = list(force_tiers) if force_tiers else qa_cadence.due_tiers(ref)

    # Resolve the platform fallback project once.
    platform = (
        Project.objects.filter(key__iexact=qa_cadence.PLATFORM_PROJECT_KEY).first()
        or Project.objects.filter(name__iexact=qa_cadence.PLATFORM_PROJECT_NAME).first()
    )

    created, skipped, no_project = [], 0, []
    # One wiki fetch per unique service per sweep — shared by checklist
    # composition (due tiers) and the daily map-freshness check below.
    map_fetches = {}

    def _map_fetch(svc):
        if svc.slug not in map_fetches:
            try:
                map_fetches[svc.slug] = qa_cadence.fetch_target_map_full(svc.map_title)
            except Exception as exc:  # defensive: sweep must never die on wiki I/O
                logger.warning("target-map fetch failed for %s: %s", svc.slug, exc)
                from tasks.qa_target_map import MapFetch
                map_fetches[svc.slug] = MapFetch(error=str(exc))
        return map_fetches[svc.slug]

    for tier_key in tiers:
        for svc in qa_cadence.QA_TIERS[tier_key]['services']:
            project = Project.objects.filter(key__iexact=svc.project_key).first() or platform
            if project is None:
                no_project.append(svc.slug)
                continue

            source_id = qa_cadence.dedup_source_id(tier_key, svc)
            # Dedup (PLAT-552 AC#4 + PLS-925): skip if a cadence task for this
            # (tier, service) is already open, OR a recurring driver exists for
            # it. The recurring driver is the ONE cadence authority per
            # (source, source_id) even while it sits in its terminal/scheduled
            # gap — creating a per-window sibling would collide with the
            # driver's next fire on the uniq_active_qa_cadence_source
            # constraint (PLAT-5169 production storm, 2026-08-09). Only a
            # terminal, non-recurring task from a prior window does not block
            # the next window's task.
            already_open = Item.objects.filter(
                source=qa_cadence.QA_CADENCE_SOURCE,
                source_id=source_id,
            ).exclude(
                status__in=qa_cadence.TERMINAL_STATUSES,
                is_recurring=False,
            ).exists()
            if already_open:
                skipped += 1
                continue

            fields = qa_cadence.build_task_fields(
                tier_key, svc, project, ref, map_fetch=_map_fetch(svc)
            )
            fields['item_key'] = project.get_next_item_key()
            try:
                item = Item.objects.create(**fields)
            except IntegrityError:
                # Another beat/worker won the concurrent create for this
                # (tier, service), OR a recurring driver's next fire collided
                # with a stale sibling on uniq_active_qa_cadence_source. The DB
                # constraint is the source of truth; refuse loudly and bounded
                # (PLS-925) instead of silently retrying every minute forever.
                logger.warning(
                    'metric:qa_cadence_source_uniqueness_refusal PLS-925 '
                    'source=%s source_id=%s tier=%s svc=%s error=%s invariant=one-cadence-authority-per-source',
                    qa_cadence.QA_CADENCE_SOURCE, source_id, tier_key, svc.slug,
                    'uniq_active_qa_cadence_source',
                )
                skipped += 1
                continue
            created.append(item.id)

    # ── CVQA-L7 map maintenance (operator directive 2026-07-14) ──────────
    # The Target Maps in the QA wiki space are the QA team's feature
    # inventory — what needs checking/verifying and what correct means.
    # Checked daily for EVERY registry service (not just due tiers): a map
    # that is missing or stale (>MAP_STALE_DAYS) gets a build/refresh task
    # filed to qa. Wiki fetch errors are skipped (never mass-file tasks on a
    # wiki outage) — the fetch-error banner on cadence tasks already surfaces
    # those loudly.
    map_refresh_created = []
    map_stale_cutoff = ref - timedelta(days=qa_cadence.MAP_STALE_DAYS)
    if timezone.is_naive(map_stale_cutoff):
        map_stale_cutoff = timezone.make_aware(map_stale_cutoff)
    for svc in qa_cadence.all_services():
        fetched = _map_fetch(svc)
        if fetched.error is not None:
            continue
        updated_at = fetched.updated_at
        if updated_at is not None and timezone.is_naive(updated_at):
            updated_at = timezone.make_aware(updated_at)
        if fetched.exists and fetched.rows and (
            updated_at is None or updated_at >= map_stale_cutoff
        ):
            continue
        if not fetched.exists:
            reason = "no Target Map page exists"
        elif not fetched.rows:
            reason = "map page has no parseable F0/F1/F2 rows"
        else:
            reason = (
                f"map stale — last updated {updated_at:%Y-%m-%d}, "
                f"freshness contract is {qa_cadence.MAP_STALE_DAYS}d"
            )

        project = Project.objects.filter(key__iexact=svc.project_key).first() or platform
        if project is None:
            continue
        already_open = Item.objects.filter(
            source=qa_cadence.QA_CADENCE_SOURCE,
            source_id=qa_cadence.map_refresh_dedup_id(svc),
        ).exclude(status__in=qa_cadence.TERMINAL_STATUSES).exists()
        if already_open:
            continue
        fields = qa_cadence.build_map_refresh_task_fields(svc, project, reason)
        fields['item_key'] = project.get_next_item_key()
        try:
            item = Item.objects.create(**fields)
        except IntegrityError:
            continue
        map_refresh_created.append(item.id)

    summary = (
        f"QA verification sweep: tiers={tiers} created={len(created)} "
        f"skipped={skipped} unresolved_project={no_project} "
        f"map_refresh_created={len(map_refresh_created)}"
    )
    logger.info(summary)
    return summary


@shared_task(bind=True, max_retries=0)
def refresh_routing_authority_checkpoints(self):
    """Refresh at most 16 ID authority leases; revocation cuts off locally first."""
    from .services.routing_authority import refresh_identity_checkpoints
    return refresh_identity_checkpoints(limit=16)


@shared_task(bind=True, max_retries=0)
def drain_alert_delivery_outbox(self):
    """PLAT-10471 / PLS-1380: production caller for the alert-delivery relay.

    The PLAT-4571 slice-3 lane (accept_batch -> phases + outbox, PLAT-5928
    relay) was wired but inert: ``drain_once`` had no celery/beat binding, so
    no naturally occurring dispatch ever joined end-to-end. This task is the
    named one-binding increment on that lineage.

    max_retries=0 matches ``relay_routing_authority_outbox``: the relay owns
    its own durable backoff (deferred rows persist ``next_attempt_at``), so a
    celery-level retry would double-drive rows the relay already leased.
    Bounded to the carrier's limit=25 per pass; beat cadence matches the
    routing-outbox precedent (30s).
    """
    from alertdelivery.relay import drain_once

    summary = drain_once(limit=25)
    logger.info("alert delivery drain: %s", summary)
    return summary


@shared_task(bind=True, max_retries=0)
def relay_routing_authority_outbox(self):
    """Bounded durable at-least-once relay for ADR-0041 routing events."""
    import os
    import random
    from datetime import timedelta
    from django.db.models import Q
    from .models import RoutingAuthorityOutbox
    from .services.routing_authority import _record_scope_hold

    endpoint = os.environ.get('PULSE_ROUTING_OUTBOX_URL', '').strip()
    now = timezone.now()
    # One distributed in-flight owner per exact outbox scope, bounded to 16.
    # The claim is committed before I/O so concurrent beat workers coalesce.
    with transaction.atomic():
        candidates = list(RoutingAuthorityOutbox.objects.select_for_update(
            skip_locked=True,
        ).filter(
            acknowledged_at__isnull=True,
            quarantined_at__isnull=True,
            next_attempt_at__lte=now,
        ).filter(
            Q(in_flight=False) | Q(in_flight_until__lte=now),
        ).order_by('next_attempt_at', 'created_at')[:64])
        rows = []
        for row in candidates:
            scope = f'{row.authority_kind}:{row.aggregate_key}'
            from django.db import connection
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))',
                        [scope],
                    )
                    if not cursor.fetchone()[0]:
                        continue
            if RoutingAuthorityOutbox.objects.filter(
                    authority_kind=row.authority_kind,
                    aggregate_key=row.aggregate_key,
                    in_flight=True,
                    in_flight_until__gt=now,
            ).exclude(pk=row.pk).exists():
                continue
            row.in_flight = True
            row.in_flight_until = now + timedelta(seconds=30)
            row.save(update_fields=['in_flight', 'in_flight_until'])
            rows.append(row)
            if len(rows) >= 16:
                break

    def finish(row, *, error='', acknowledged_at=None, quarantined_at=None):
        with transaction.atomic():
            current = RoutingAuthorityOutbox.objects.select_for_update().get(pk=row.pk)
            current.attempts += 1
            current.in_flight = False
            current.in_flight_until = None
            current.last_error = error
            if acknowledged_at:
                current.published_at = current.published_at or acknowledged_at
                current.acknowledged_at = acknowledged_at
            elif quarantined_at:
                current.quarantined_at = quarantined_at
            else:
                cap = min(300, 30 * (2 ** max(0, current.attempts - 1)))
                current.next_attempt_at = timezone.now() + timedelta(
                    seconds=random.uniform(0, cap),
                )
            current.save(update_fields=[
                'attempts', 'in_flight', 'in_flight_until', 'last_error',
                'next_attempt_at', 'published_at', 'acknowledged_at',
                'quarantined_at',
            ])
            return current

    sent = acknowledged = quarantined = failed = 0
    for row in rows:
        if not endpoint:
            _record_scope_hold(
                'routing-outbox', row.aggregate_key, row.version,
                'relay_unconfigured', owner_group='devops',
            )
            finish(row, error='relay_unconfigured')
            failed += 1
            continue
        payload = {
            'origin_event_id': str(row.origin_event_id),
            'authority_kind': row.authority_kind,
            'aggregate_key': row.aggregate_key,
            'version': row.version,
            'digest': row.digest,
            'tombstone': row.tombstone,
            'payload': row.payload,
        }
        try:
            response = requests.post(endpoint, json=payload, timeout=5)
            if 200 <= response.status_code < 300:
                finish(row, acknowledged_at=timezone.now())
                acknowledged += 1
            elif 400 <= response.status_code < 500 and response.status_code != 409:
                error = f'poison_http_{response.status_code}'
                finish(row, error=error, quarantined_at=timezone.now())
                _record_scope_hold(
                    'routing-outbox', row.aggregate_key, row.version,
                    'relay_poison_quarantined', owner_group='devops',
                )
                quarantined += 1
            else:
                error = f'http_{response.status_code}'
                finish(row, error=error)
                _record_scope_hold(
                    'routing-outbox', row.aggregate_key, row.version,
                    'relay_failed', owner_group='devops',
                )
                failed += 1
            sent += 1
        except Exception as exc:
            finish(row, error=type(exc).__name__)
            _record_scope_hold(
                'routing-outbox', row.aggregate_key, row.version,
                'relay_failed', owner_group='devops',
            )
            failed += 1
    return {'claimed': len(rows), 'sent': sent, 'acknowledged': acknowledged,
            'quarantined': quarantined, 'failed': failed,
            'reason': 'relay_unconfigured' if not endpoint else ''}


def _probe_wip_admission_health_once():
    """Exercise one positive-cap lane without depending on real assignment traffic.

    The synthetic item traverses the same capacity-row lock, org-scoped load
    accounting and reservation decision as production assignment/Start.  A
    successful reservation is immediately released; a capacity denial is also
    a healthy result because the authoritative boundary completed.
    """
    from types import SimpleNamespace
    from .models import Project, TeamRoster
    from .services.workflow_service import WorkflowService

    roster = (
        TeamRoster.objects.filter(
            is_active=True,
            wip_enforced=True,
            organization_id__isnull=False,
        ).filter(
            Q(ready_wip_cap__gt=0) | Q(active_wip_cap__gt=0)
        ).order_by('organization_id', 'team_name').first()
    )
    if roster is None:
        raise RuntimeError('no positive-cap WIP roster is configured')
    members = WorkflowService._flat_roster(roster.tiers or [])
    members.extend(roster.junior_emails or [])
    email = next((str(value).strip().lower() for value in members if str(value).strip()), '')
    if not email:
        raise RuntimeError('positive-cap WIP roster has no member to probe')
    project = Project.objects.filter(org_id=roster.organization_id).order_by('pk').first()
    if project is None:
        raise RuntimeError('positive-cap WIP roster has no project in its organization')

    status_slug = 'open' if roster.ready_wip_cap > 0 else 'in_progress'
    probe = SimpleNamespace(
        pk=None,
        item_key='PLS-509-WIP-PROBE',
        mode='task',
        status=status_slug,
        assignment_group=roster.team_name,
        project=project,
    )
    from .services.workflow_service import CAPACITY_UNAVAILABLE
    admitted = WorkflowService._reserve_wip_slot(
        probe,
        None,
        email,
        status_override=status_slug,
    )
    if admitted is None:
        raise RuntimeError('positive-cap WIP probe bypassed the admission boundary')
    released = WorkflowService._release_unconsumed_wip_reservation(probe)
    if admitted is True:
        probe_status = 'admitted'
    elif admitted == CAPACITY_UNAVAILABLE:
        probe_status = 'capacity_unavailable'
    else:
        probe_status = 'capacity_denied'
    return {
        'status': probe_status,
        'organization_id': int(roster.organization_id),
        'team': roster.team_name,
        'slot_kind': 'ready' if status_slug == 'open' else 'active',
        'reservation_released': bool(released),
    }


@shared_task(bind=True, max_retries=0)
def probe_wip_admission_health(self):
    """Idle-safe scheduled health proof for the PLS-509 admission boundary."""
    return _probe_wip_admission_health_once()


def _probe_invariant_validator_health_once():
    """Idle-safe proof that the write-time invariant validator still rejects
    known-bad transitions (PLAT-1256).

    The ``invariant_validator`` liveness heartbeat fires at the top of
    ``WorkflowService.validate_transition`` — i.e. it is EVENT-driven and only
    refreshes when a real transition runs. During quiet periods (no fleet
    transitions for hours) the signal goes stale even though the validator is
    perfectly healthy, and the mechanism-liveness probe pages a dead-validator
    class it cannot distinguish from a healthy-idle one (live instance:
    pulse-invariant-validator stale ~2.5h on an incident night with zero
    transitions). This probe mirrors the PLS-509 pattern: exercise the real
    boundary deterministically, without depending on traffic, and let a
    healthy result refresh the same heartbeat.

    The exercised boundary: the PLS-12 cancel gate rejecting a cancel with no
    reason category — the registered invariant verbatim ("Pulse write-time
    validators reject known-bad workflow/task states"). The probe item is an
    in-memory SimpleNamespace (``pk=None``), so the walk to the cancel gate is
    read-only (Status lookups only) and touches no rows; the gate itself is
    pure in-memory and raises before any fix-evidence DB access.
    """
    from types import SimpleNamespace

    from .services.workflow_service import (
        WorkflowService,
        WorkflowValidationError,
    )

    probe = SimpleNamespace(
        pk=None,
        item_key='PLAT-1256-VALIDATOR-PROBE',
        mode='task',
        status='todo',
        workflow_status='To Do',
        workflow_name='',
        project=None,
        metadata={},
    )
    try:
        WorkflowService.validate_transition(
            probe, 'cancelled', user=None, screen_data={},
        )
    except WorkflowValidationError as exc:
        message = str(exc).lower()
        if 'reason category' not in message:
            raise RuntimeError(
                'validator probe hit an unexpected rejection (expected the '
                'PLS-12 cancel reason-category gate): %s' % exc,
            )
        return {
            'status': 'rejected_known_bad',
            'gate': 'pls12_cancel_reason_category',
        }
    raise RuntimeError(
        'validator probe PASSED a known-bad cancel with no reason category '
        '- the PLS-12 write-time gate did not reject; validator is not '
        'enforcing (PLAT-1256)',
    )


@shared_task(bind=True, max_retries=0)
def probe_invariant_validator_health(self):
    """Idle-safe scheduled health proof for the PLAT-1256 write-time validator."""
    return _probe_invariant_validator_health_once()



def ensure_periodic_task(
    name,
    *,
    task,
    enabled=True,
    interval=None,
    crontab=None,
    args=None,
    kwargs=None,
    expire_seconds=None,
    one_off=None,
    preserve_disabled=False,
    clear_unused_schedule_slots=True,
):
    """Idempotently materialize a django-celery-beat PeriodicTask.

    PLS-794: ``PeriodicTask.objects.update_or_create(..., defaults=...)`` always
    rewrites the row on every call. ``date_changed`` is ``auto_now``, so each
    rewrite bumps it. django-celery-beat's DatabaseScheduler treats
    ``max(date_changed)`` as "schedule changed", reloads, and for never-run
    tasks sets ``last_run_at = date_changed`` (see ModelEntry.__init__). When
    ``TasksConfig.ready()`` re-runs setup on every web/worker/beat process boot
    more often than the task interval, the due baseline is re-seeded forever
    and the task never dispatches (live: Attachment Storage Canary, 5m interval,
    last_run_at=NULL, total_run_count=0 while date_changed kept advancing).

    Only call ``save(update_fields=...)`` when a tracked *code-owned* field
    actually differs from the desired state, under ``select_for_update`` so a
    concurrent Beat ``ModelEntry.save`` of ``last_run_at``/``total_run_count``
    cannot be clobbered by a full-row rewrite. ``preserve_disabled=True`` keeps
    an explicit operational disable across setup (Sync Linked PR Statuses)
    while still repairing cadence/task identity when those drift.
    """
    from django_celery_beat.models import PeriodicTask

    defaults = {
        'task': task,
        'enabled': bool(enabled),
    }
    if interval is not None:
        defaults['interval'] = interval
    if crontab is not None:
        defaults['crontab'] = crontab
    if args is not None:
        defaults['args'] = args
    if kwargs is not None:
        defaults['kwargs'] = kwargs
    if expire_seconds is not None:
        defaults['expire_seconds'] = expire_seconds
    if one_off is not None:
        defaults['one_off'] = one_off

    if clear_unused_schedule_slots:
        if interval is not None:
            defaults.setdefault('crontab', None)
            defaults.setdefault('solar', None)
            defaults.setdefault('clocked', None)
        elif crontab is not None:
            defaults.setdefault('interval', None)
            defaults.setdefault('solar', None)
            defaults.setdefault('clocked', None)

    obj, created = PeriodicTask.objects.get_or_create(name=name, defaults=defaults)
    if created:
        return obj, True

    # Track which *code-owned* fields differ. Never put Beat runtime counters
    # (last_run_at / total_run_count / no_changes) on this list — those are
    # owned by django-celery-beat's ModelEntry.save_fields and must survive a
    # concurrent setup drift-repair (rei P2-1 / PLS-794).
    dirty_fields = []

    def _set_attr(field, value):
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            if field not in dirty_fields:
                dirty_fields.append(field)

    def _set_fk(field, value):
        id_field = f'{field}_id'
        new_id = None if value is None else getattr(value, 'pk', value)
        if getattr(obj, id_field) != new_id:
            setattr(obj, field, value)
            if field not in dirty_fields:
                dirty_fields.append(field)

    if interval is not None:
        _set_fk('interval', interval)
        if clear_unused_schedule_slots:
            _set_fk('crontab', None)
            _set_fk('solar', None)
            _set_fk('clocked', None)
    elif crontab is not None:
        _set_fk('crontab', crontab)
        if clear_unused_schedule_slots:
            _set_fk('interval', None)
            _set_fk('solar', None)
            _set_fk('clocked', None)

    _set_attr('task', task)

    if preserve_disabled and not obj.enabled:
        # Leave an explicit stop-bleed disable intact.
        pass
    else:
        _set_attr('enabled', bool(enabled))

    if args is not None:
        _set_attr('args', args)
    if kwargs is not None:
        _set_attr('kwargs', kwargs)
    if expire_seconds is not None:
        _set_attr('expire_seconds', expire_seconds)
    if one_off is not None:
        _set_attr('one_off', one_off)

    if not dirty_fields:
        return obj, False

    # Snapshot desired code-owned values, then re-apply under row lock onto a
    # fresh DB read. A full-model save() on the pre-lock instance would rewrite
    # Beat-owned last_run_at/total_run_count from a stale snapshot (lost update
    # when Beat reserves between our get_or_create and save). update_fields
    # restricts the WRITE set; select_for_update serializes against concurrent
    # Beat ModelEntry.save on the same row.
    #
    # of=('self',) is required on PostgreSQL: PeriodicTask's schedule FKs
    # (interval/crontab/solar/clocked) are nullable, so select_related would
    # LEFT OUTER JOIN them and PG rejects ``FOR UPDATE`` on the nullable side
    # of an outer join. We only need the PeriodicTask row lock + *_id columns
    # for the re-diff, so locking self alone is both correct and portable.
    desired = {field: getattr(obj, field) for field in dirty_fields}
    fk_fields = {'interval', 'crontab', 'solar', 'clocked'}
    with transaction.atomic():
        locked = (
            PeriodicTask.objects
            .select_for_update(of=('self',))
            .get(pk=obj.pk)
        )
        update_fields = []
        for field, value in desired.items():
            if field in fk_fields:
                id_field = f'{field}_id'
                new_id = None if value is None else getattr(value, 'pk', value)
                if getattr(locked, id_field) != new_id:
                    setattr(locked, field, value)
                    update_fields.append(field)
            elif getattr(locked, field) != value:
                setattr(locked, field, value)
                update_fields.append(field)
        if update_fields:
            # date_changed is auto_now; include it so the stamp advances only on
            # intentional code-owned drift (and Beat can observe the change).
            locked.save(update_fields=update_fields + ['date_changed'])
        return locked, False


def setup_notification_schedules():
    """
    Setup periodic task schedules for notification tasks.
    This function should be called from apps.py ready() method.
    """
    try:
        from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
        from django.utils import timezone as django_timezone

        def _crontab(*, minute, hour, day_of_week='*', day_of_month='*', month_of_year='*'):
            """Return one canonical crontab row, repairing duplicate beat rows.

            django-celery-beat includes timezone in the uniqueness surface. Older
            seed paths omitted it, so restarts could hit MultipleObjectsReturned
            and abort all watchdog registration. Keep the newest canonical row and
            repoint periodic tasks before deleting extras.
            """
            tz = django_timezone.get_current_timezone()
            qs = CrontabSchedule.objects.filter(
                minute=minute,
                hour=hour,
                day_of_week=day_of_week,
                day_of_month=day_of_month,
                month_of_year=month_of_year,
                timezone=tz,
            ).order_by('id')
            first = qs.first()
            if first is None:
                return CrontabSchedule.objects.create(
                    minute=minute,
                    hour=hour,
                    day_of_week=day_of_week,
                    day_of_month=day_of_month,
                    month_of_year=month_of_year,
                    timezone=tz,
                )
            dupes = list(qs[1:])
            if dupes:
                PeriodicTask.objects.filter(crontab__in=dupes).update(crontab=first)
                CrontabSchedule.objects.filter(id__in=[d.id for d in dupes]).delete()
                logger.warning(
                    "setup_notification_schedules: collapsed %d duplicate crontab row(s) for %s %s",
                    len(dupes), minute, hour,
                )
            return first

        # Remove old periodic task if it exists
        PeriodicTask.objects.filter(name='Check Overdue Tasks').delete()

        # Sweep missed notifications every 60 seconds as a DB fallback
        interval_1min, _ = IntervalSchedule.objects.get_or_create(
            every=1,
            period=IntervalSchedule.MINUTES,
        )
        interval_30sec, _ = IntervalSchedule.objects.get_or_create(
            every=30,
            period=IntervalSchedule.SECONDS,
        )
        interval_5min, _ = IntervalSchedule.objects.get_or_create(
            every=5,
            period=IntervalSchedule.MINUTES,
        )

        ensure_periodic_task(
            name='Sweep Missed Notifications',
            interval=interval_1min,
            task='tasks.tasks.sweep_missed_notifications',
            enabled=True,
        )

        # PLS-483: in-boundary attachment-storage detective canary (option-b).
        # Runs inside the existing Pulse worker trust boundary (no standalone
        # exporter, no broad secret injection); the task self-guards with a Redis
        # SET NX EX singleton lock + a hard bounded timeout. `enabled` follows the
        # ATTACHMENT_CANARY_ENABLED gate the task also self-checks.
        # PLS-1412: one_off must be forced off. django-celery-beat disables a
        # one_off row after the first dispatch and clears last_run_at, which
        # re-seeds the due baseline and starves the 5-minute canary until the
        # next process boot. ensure_periodic_task only writes one_off when it
        # is passed, so a stale True survived the PLS-794 no-op save.
        ensure_periodic_task(
            name='Attachment Storage Canary',
            interval=interval_5min,
            task='tasks.attachment_storage_canary',
            enabled=bool(getattr(settings, 'ATTACHMENT_CANARY_ENABLED', True)),
            one_off=False,
        )

        # PLS-915: durable net for the PulseRecurringMissingTimer invariant —
        # re-schedule any active recurring item absent from Redis
        # pulse:timers:recurring (drift from migration/bulk .update() paths that
        # bypass post_save, or a Redis flush between scheduler restarts). Runs on
        # the same 5m cadence as the other timer sweeps; self-guarded by a Redis
        # SET NX EX lock inside the task.
        ensure_periodic_task(
            name='Reconcile Recurring Timers',
            interval=interval_5min,
            task='tasks.tasks.reconcile_recurring_timers',
            enabled=True,
        )

        # PLAT-4609: durable recovery backstop for committed escalation-routing
        # outbox rows whose transaction.on_commit broker enqueue failed. This is
        # the bounded fail-loud path required by the typed escalation contract;
        # without beat invoking it, initial enqueue failure can strand operator
        # attention indefinitely.
        ensure_periodic_task(
            name='Sweep Escalation Routing Outbox',
            interval=interval_1min,
            task='tasks.tasks.sweep_escalation_routing_outbox',
            enabled=True,
        )

        # PLS-521: recover committed deployment-gate verdict outbox rows when
        # the transaction.on_commit broker enqueue is lost.  The same bounded
        # sweep emits the pending-age liveness signal for fail-loud operation.
        ensure_periodic_task(
            name='Sweep Deployment Gate Outbox',
            interval=interval_1min,
            task='tasks.tasks.sweep_deployment_gate_outbox',
            enabled=True,
        )

        # PLS-733: external-wait release delivery + human-wait revalidation.
        # Route through ensure_periodic_task so setup no-ops do not bump
        # date_changed (same PLS-794 canary class of bug).
        ensure_periodic_task(
            name='Sweep External Dependency Releases',
            interval=interval_1min,
            task='tasks.tasks.sweep_external_dependency_releases',
            enabled=True,
        )

        ensure_periodic_task(
            name='Sweep External Response Holds',
            interval=interval_1min,
            task='tasks.tasks.sweep_external_response_holds',
            enabled=True,
        )

        crontab_external_audit_archive = _crontab(minute='37', hour='2')
        ensure_periodic_task(
            name='Archive External Response Hold Audit',
            crontab=crontab_external_audit_archive,
            task='tasks.tasks.archive_external_response_hold_audit',
            enabled=True,
        )

        crontab_external_revalidate = _crontab(minute='17', hour='*/1')
        ensure_periodic_task(
            name='Revalidate Human External Waits',
            crontab=crontab_external_revalidate,
            task='tasks.tasks.revalidate_human_external_waits',
            enabled=True,
        )

        ensure_periodic_task(
            name='Sweep Publication Outbox',
            interval=interval_1min,
            task='tasks.tasks.sweep_publication_outbox',
            enabled=True,
        )

        ensure_periodic_task(
            name='Sweep QA Verification Outbox',
            interval=interval_1min,
            task='tasks.tasks.sweep_qa_verification_outbox',
            enabled=True,
        )

        # ADR-0041: freshness is a short causally-bound lease, refreshed by a
        # bounded worker—not by request handlers.  Outbox relay uses the same
        # recovery cadence and never substitutes for a committed DB row.
        ensure_periodic_task(
            name='Refresh Routing Authority Checkpoints',
            interval=interval_30sec,
            task='tasks.tasks.refresh_routing_authority_checkpoints',
            enabled=True,
        )
        ensure_periodic_task(
            name='Relay Routing Authority Outbox',
            interval=interval_30sec,
            task='tasks.tasks.relay_routing_authority_outbox',
            enabled=True,
        )

        # PLAT-10471 / PLS-1380: the alert-delivery relay's production caller.
        # Same recovery cadence as the routing-outbox relay (30s); the relay's
        # own lease/backoff makes overlapping passes safe (skip_locked claims).
        ensure_periodic_task(
            name='Drain Alert Delivery Outbox',
            interval=interval_30sec,
            task='tasks.tasks.drain_alert_delivery_outbox',
            enabled=True,
        )

        # Frequent terminal archive (every 15 min). Soft-archives completed /
        # cancelled / rejected / deferred / etc. older than ARCHIVE_AFTER_DAYS
        # (default 1). Daily-only left ~7k finished rows unarchived and bloated
        # is_active plans; run often so the unarchived slice stays near the
        # real active queue. Clear crontab so we do not keep the old daily slot.
        interval_15min, _ = IntervalSchedule.objects.get_or_create(
            every=15,
            period=IntervalSchedule.MINUTES,
        )
        ensure_periodic_task(
            name='Sweep Archive Done Items',
            interval=interval_15min,
            task='tasks.tasks.sweep_archive_done_items',
            enabled=True,
        )

        # Hourly backstop: re-drive / escalate in_review items with no movement
        # for >48h (STALE_REVIEW_HOURS) so review work can't orphan silently.
        crontab_hourly = _crontab(minute='23', hour='*')
        ensure_periodic_task(
            name='Sweep Stale Reviews',
            crontab=crontab_hourly,
            task='tasks.tasks.sweep_stale_reviews',
            enabled=True,
        )

        # Hourly: comment on non-blocked tasks stale on offline/unavailable agents
        # so Pulse surfaces agent-native liveness stalls in-product.
        ensure_periodic_task(
            name='Sweep Stale Offline Assignments',
            crontab=crontab_hourly,
            task='tasks.tasks.sweep_stale_offline_assignments',
            enabled=True,
        )

        # Hourly autonomous design->implementation handoff: approved HLD/design tasks
        # with no implementation follow-on get an eng implementation task auto-spawned.
        crontab_h41 = _crontab(minute='41', hour='*')
        ensure_periodic_task(
            name='Sweep Design Handoff',
            crontab=crontab_h41,
            task='tasks.tasks.sweep_design_handoff',
            enabled=True,
        )

        # PLS-32: every 10 min (was 30). The cadence is defined here in code, so a pod
        # restart re-running setup_periodic_schedules() now PRESERVES 10min via
        # update_or_create instead of reverting to 30min — which previously re-opened the
        # PLAT-375 engineering routing-desync / queue-starve window after every restart.
        # Autonomous pod load-balancing so no agent becomes load-bearing while peers
        # starve (catches direct-by-name pileups too).
        crontab_10m = _crontab(minute='*/10', hour='*')
        ensure_periodic_task(
            name='Sweep Rebalance Pods',
            crontab=crontab_10m,
            task='tasks.tasks.sweep_rebalance_pods',
            enabled=True,
        )

        # PLS-509: workload-independent proof that the atomic WIP admission
        # boundary can lock its capacity row and complete org-scoped accounting.
        # Real traffic also refreshes the same heartbeat, but an idle healthy
        # lane must never page merely because nobody requested an assignment.
        ensure_periodic_task(
            name='Probe WIP Admission Health',
            crontab=crontab_10m,
            task='tasks.tasks.probe_wip_admission_health',
            enabled=True,
        )

        # Orphan review-lane sweep (operator directive 2026-09-15): pipeline-
        # created items minted directly into in_review with no assignee have no
        # assignment path (the drain skips review-lane statuses). Hourly, bounce
        # unassigned review-lane items past a 3-day grace window back to a
        # hands-on status so the queue-drain owns them again.
        ensure_periodic_task(
            name='Sweep Orphan Review Lane',
            crontab=_crontab(minute='41', hour='*'),
            task='tasks.tasks.sweep_orphan_review_lane',
            enabled=True,
        )

        # Recurring status-truth sweep (operator directive 2026-09-16): a
        # recurring row waiting for its next occurrence must be `scheduled`
        # everywhere — DB status, workload projection, card. Park future-due
        # recurring rows that still sit in open/todo/pending so the raw status
        # matches the projection (one truth, no discrepancies).
        ensure_periodic_task(
            name='Sweep Recurring Status Truth',
            crontab=_crontab(minute='47', hour='*'),
            task='tasks.tasks.sweep_recurring_status_truth',
            enabled=True,
        )

        # PLAT-1256: the invariant_validator liveness heartbeat is event-driven
        # (fires inside validate_transition), so a fleet-quiet period reads as a
        # dead validator to the mechanism-liveness probe. This probe exercises
        # the PLS-12 cancel gate deterministically every 10 minutes — read-only,
        # in-memory item — refreshing the same heartbeat and proving the
        # validator still REJECTS known-bad states, not merely that the process
        # is up. Mirror of the PLS-509 idle-proof pattern.
        ensure_periodic_task(
            name='Probe Invariant Validator Health',
            crontab=crontab_10m,
            task='tasks.tasks.probe_invariant_validator_health',
            enabled=True,
        )

        # PLS-168: Replenishment / Definition-of-Ready gate — the UPSTREAM complement
        # to the rebalance sweep. Every 15 min, feed each team's ready queue from its
        # undecomposed epics (bounded to the ready deficit, only when an under-cap
        # member is available) so a pull-based team never starves while epics pile in
        # the backlog. Offset from the */10 rebalance so groomed child work exists
        # before the next pull cycle.
        crontab_15m = _crontab(minute='3,18,33,48', hour='*')
        crontab_assign = _crontab(minute='5,15,25,35,45,55', hour='*')
        ensure_periodic_task(
            name='Sweep Assign Group-Queued',
            crontab=crontab_assign,
            task='tasks.tasks.sweep_assign_group_queued',
            enabled=True,
        )

        ensure_periodic_task(
            name='Sweep Replenish Ready',
            crontab=crontab_15m,
            task='tasks.tasks.sweep_replenish_ready',
            enabled=True,
        )

        # Autonomous blocked-task invariant: a `blocked` task with no active linked
        # blocker (and not on the operator's queue) was never legitimately blocked —
        # return it to the ready queue so no agent can hoard blocked work. DRY-RUN until
        # PULSE_BLOCKED_RETRIAGE_APPLY=true (verify the preview first).
        ensure_periodic_task(
            name='Sweep Blocked Re-triage',
            crontab=crontab_10m,
            task='tasks.tasks.sweep_blocked_retriage',
            enabled=True,
        )

        # PLS-256 Backlog Custodian: daily stale-bot sweep at 03:41 UTC (off-peak).
        # Marks non-terminal idle tasks `stale`, then closes them after a grace
        # window. DRY-RUN until PULSE_STALE_SWEEP_APPLY=true (verify the preview
        # first); kill-switch PULSE_STALE_SWEEP_ENABLED; per-run close cap.
        crontab_stale_daily = _crontab(minute='41', hour='3')
        ensure_periodic_task(
            name='Sweep Stale Tasks',
            crontab=crontab_stale_daily,
            task='tasks.tasks.sweep_stale_tasks',
            enabled=True,
        )

        # Every 3 min: publish live agent availability (bridge up/down) to Redis so
        # team routing auto-fails-over from a downed primary (Codex) to its peer.
        crontab_3m = _crontab(minute='*/3', hour='*')
        ensure_periodic_task(
            name='Sweep Agent Availability',
            crontab=crontab_3m,
            task='tasks.tasks.sweep_agent_availability',
            enabled=True,
        )

        # Every 5 min: resume tasks whose linked blockers have all reached a
        # terminal state. Immediate transition hooks handle the normal path; this
        # catches tasks that were already stale before the hook existed.
        crontab_5m = _crontab(minute='*/5', hour='*')
        ensure_periodic_task(
            name='Sweep Resolved Blocked Items',
            crontab=crontab_5m,
            task='tasks.tasks.sweep_resolved_blocked_items',
            enabled=True,
        )
        ensure_periodic_task(
            name='Sweep Active Blocker Links',
            crontab=crontab_5m,
            task='tasks.tasks.sweep_active_blocker_links',
            enabled=True,
        )

        # Every 10 min: Program-Management org-health watchdog (operator 2026-06-13).
        # The always-on outer control loop — turns org-level stalls/bottlenecks into
        # tasks in the program-management team's queue so the CoS fixes them on her
        # heartbeat, no operator reminder. Self-resolves when the signal clears.
        crontab_10m = _crontab(minute='*/10', hour='*')
        ensure_periodic_task(
            name='Sweep Org Health',
            crontab=crontab_10m,
            task='tasks.tasks.sweep_org_health',
            enabled=True,
        )

        # CVQA-L2 (PLAT-474 / PLAT-552): daily QA verification cadence sweep.
        # Fires once a day; internally generates per-project QA tasks for the
        # risk tiers due that day (T1 daily / T2 weekly / T3 bi-weekly /
        # T4 monthly). Converges with the org-health/probe pipeline per the
        # PLAT-474 architecture review. 06:23 UTC = off-peak.
        crontab_qa_daily = _crontab(minute='23', hour='6')
        ensure_periodic_task(
            name='QA Verification Sweep',
            crontab=crontab_qa_daily,
            task='tasks.tasks.qa_verification_sweep',
            enabled=True,
        )

        # PLAT-2962: daily detective metric for QA false-passes. 06:53 UTC is
        # intentionally offset from the 06:23 verification sweep so new QA pass
        # comments/terminal transitions land before the detector runs.
        crontab_false_pass_daily = _crontab(minute='53', hour='6')
        ensure_periodic_task(
            name='QA False-Pass Detector',
            crontab=crontab_false_pass_daily,
            task='tasks.tasks.sweep_false_pass_detector',
            enabled=True,
        )

        # Every 30 min: org-throughput watchdog — alert if zero agent transitions
        # in a rolling 90-min window while >= 10 actionable tasks are assigned.
        # Detects the 2026-06-12 post-reboot 3h fleet-stall pattern (PLS-30).
        crontab_30m_watchdog = _crontab(minute='7,37', hour='*')
        ensure_periodic_task(
            name='Sweep Throughput Watchdog',
            crontab=crontab_30m_watchdog,
            task='tasks.tasks.sweep_throughput_watchdog',
            enabled=True,
        )

        # PLS-49: every 5 min — sync open PR statuses from GitHub for non-terminal items.
        # AC requires merged PRs show 'merged' within 5 min of merge; 15 min failed QA (zen).
        # Catches merged/closed PRs that didn't receive a GitHub webhook (repos where the
        # webhook is not registered or a delivery was missed).
        crontab_5m_pr = _crontab(minute='*/5', hour='*')
        # Preserve an explicit operational disable across Django/Celery process
        # startup (external calls / worker time budget) while still repairing
        # code-owned cadence and task name — without bumping date_changed when
        # nothing changed (PLS-794).
        ensure_periodic_task(
            name='Sync Linked PR Statuses',
            crontab=crontab_5m_pr,
            task='tasks.tasks.sync_linked_pr_statuses',
            enabled=True,
            preserve_disabled=True,
        )

        # PLS-1303: every 30 min — heal the merge→deploy-event correlation leg.
        # The merge-time on_commit schedule is the normal path; this bounded
        # sweep recovers lost schedules and the historical stranded cohort
        # (PLAT-9027) by re-ingesting stored events whose correlation predates
        # the merge attestation. No event for the commit = no action.
        crontab_30m_pls1303 = _crontab(minute='7,37', hour='*')
        ensure_periodic_task(
            name='Backfill Merge-Correlated Deployment Events',
            crontab=crontab_30m_pls1303,
            task='tasks.tasks.backfill_merge_correlated_deployment_events',
            enabled=True,
            preserve_disabled=True,
        )

        # PLAT-724: every 30 min — re-attempt operator assignment for human-ops
        # escalation tasks that were created but never got a concrete assignee
        # (e.g. operator transient unavailability at escalation time).
        crontab_30m_human_ops = _crontab(minute='13,43', hour='*')
        ensure_periodic_task(
            name='Sweep Stale Human-Ops Blockers',
            crontab=crontab_30m_human_ops,
            task='tasks.tasks.sweep_stale_human_ops_blockers',
            enabled=True,
        )

        # PLAT-948: every Friday at 11:30 UTC (17:00 IST) — weekly retrospective
        # prompt posted to the Leadership Retro Connect group conversation.
        crontab_weekly_retro = _crontab(minute='30', hour='11', day_of_week='fri')
        ensure_periodic_task(
            name='Post Weekly Retro Prompt',
            crontab=crontab_weekly_retro,
            task='tasks.tasks.post_weekly_retro_prompt',
            enabled=True,
        )

        # PLAT-967 (HLD PLAT-963 §2.4): every Monday at 04:00 UTC (09:30 IST) —
        # weekly digest of design/HLD/ADR tasks currently in Architecture Review,
        # posted to the Design Review Connect group so open reviews don't languish.
        crontab_weekly_design_digest = _crontab(minute='0', hour='4', day_of_week='mon')
        ensure_periodic_task(
            name='Post Weekly Design-Review Digest',
            crontab=crontab_weekly_design_digest,
            task='tasks.tasks.post_weekly_design_review_digest',
            enabled=True,
        )

        # PLAT-1192: 1st of each month at 05:00 UTC (10:30 IST) — org-health
        # digest with the previous month's learning-loop conversion metric
        # (PLAT-1180 deliverable c), posted to the Leadership Retro group.
        crontab_monthly_org_health = _crontab(minute='0', hour='5', day_of_month='1')
        ensure_periodic_task(
            name='Post Monthly Org-Health Digest',
            crontab=crontab_monthly_org_health,
            task='tasks.tasks.post_monthly_org_health_digest',
            enabled=True,
        )

        # PLS-307: every 15 min — fail-loud liveness probe for the async AI-recap
        # mechanism. Files a task to the engineering queue when recap runs stall
        # (stuck queued/running) or fail in bursts; self-resolves when clear.
        crontab_15m_recaps = _crontab(minute='9,24,39,54', hour='*')
        ensure_periodic_task(
            name='Sweep Stale AI Recaps',
            crontab=crontab_15m_recaps,
            task='tasks.tasks.sweep_stale_ai_recaps',
            enabled=True,
        )

        logger.info("Periodic notification schedules configured (sweep every 60s)")

    except Exception as e:
        logger.warning(f"Could not setup notification schedules: {e}")


def _notify_false_pass_detector_failure(exc: Exception) -> bool:
    """Fail-loud owner notification for the PLAT-2962 recurring detector."""
    try:
        from .services.connect_origin import system_message_headers
        from .services.workflow_service import WorkflowService as W
        from .user_service import user_service

        lead_email = W._resolve_team_lead_email('qa') or 'zen@shizuha.com'
        user = user_service.get_user_by_email(lead_email)
        user_id = user.get('id') if user else None
        if not user_id:
            logger.warning('sweep_false_pass_detector: no QA lead user_id for %s', lead_email)
            return False
        content = (
            "[QA False-Pass Detector Failure]\n"
            "Recurring task `tasks.tasks.sweep_false_pass_detector` failed before completing its report update.\n"
            f"Exception: {type(exc).__name__}: {exc}\n"
            "Owner action: inspect Pulse worker logs and PLAT-2962 / PR #399 before the next QA false-pass audit."
        )
        body = {
            'recipient_user_id': user_id,
            'content': content,
            'sender_name': 'Pulse QA False-Pass Detector',
            'sender_service': 'shizuha-pulse',
        }
        connect_base_url = getattr(settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api').rstrip('/')
        response = requests.post(
            f'{connect_base_url}/internal/system-message/',
            json=body,
            headers=system_message_headers(body),
            timeout=5,
        )
        response.raise_for_status()
        return True
    except Exception as notify_exc:  # pragma: no cover - must not mask original failure
        logger.warning('sweep_false_pass_detector: failure notification failed: %s', notify_exc)
        return False


@shared_task(bind=True, max_retries=1)
def sweep_false_pass_detector(self, window_days: int = 14, lookback_days: int = 14, alert: bool = True):
    """PLAT-2962: rolling QA false-pass detector.

    Computes verifications -> reopen/refile/regression-link pairs, persists the
    report item for PLAT-1149, and DMs the QA lead when the floor is breached.
    """
    from .services.false_pass_detector import run_false_pass_detector, _mark_report_run_failed

    try:
        return run_false_pass_detector(
            window_days=window_days,
            lookback_days=lookback_days,
            alert=alert,
        )
    except Exception as exc:
        # PLS-718: stamp the report item as failed so it never silently serves
        # stale numbers as "no change" — the reader can distinguish a skipped
        # run from an unchanged detector.
        _mark_report_run_failed(exc, window_days=window_days)
        _notify_false_pass_detector_failure(exc)
        logger.exception('sweep_false_pass_detector failed')
        raise


@shared_task(bind=True, max_retries=1)
def sweep_archive_done_items(self, days: int | None = None):
    """Soft-archive terminal / done-category work so default queues stay lean.

    Covers every Status with category ``done`` **and** the hard-coded terminal
    slugs used by the active-queue predicate (completed, cancelled, rejected,
    deferred, closed, …). Operator 2026-07-27: ~6.9k finished rows sat
    unarchived and polluted is_active list/count plans even though the logical
    active queue was only ~1.2k.

    Age gate: ``Coalesce(resolution_date, updated_at, created_at)`` older than
    ``days`` (env ``ARCHIVE_AFTER_DAYS``, default **1**). Preferring
    resolution_date fixes the old bug where a recent ``updated_at`` touch kept
    finished work unarchived forever under a 28-day gate. Pass ``days=0`` to
    archive all matching terminal rows immediately.

    Idempotent bulk UPDATE (triggers skipped via session_replication_role for
    this housekeeping write only).
    """
    import os
    from datetime import timedelta
    from django.db.models.functions import Coalesce
    from .models import Status, Item
    from .services.actionable_items import TERMINAL_STATUS_SLUGS

    if days is not None:
        effective_days = int(days)
    else:
        effective_days = int(os.environ.get('ARCHIVE_AFTER_DAYS', '1'))
    if effective_days < 0:
        logger.warning(f'sweep_archive_done_items: days={effective_days} invalid, skipping')
        return {'archived': 0, 'reason': 'invalid days'}

    done_slugs = set(
        Status.objects.filter(category='done').values_list('slug', flat=True)
    )
    # Union so cancelled/rejected/deferred/completed/etc. archive even if a
    # custom status is mis-categorized or only present in the hard-coded set.
    terminal_slugs = sorted(done_slugs | set(TERMINAL_STATUS_SLUGS))
    if not terminal_slugs:
        return {'archived': 0, 'reason': 'no terminal statuses'}

    base = Item.objects.filter(
        is_archived=False,
        mode='task',
        status__in=terminal_slugs,
    )
    if effective_days == 0:
        # Immediate drain of all terminal/done work (operator one-shot / test).
        target_ids = list(base.values_list('pk', flat=True))
    else:
        cutoff = timezone.now() - timedelta(days=effective_days)
        # resolution_date first so a finished task that later got a metadata
        # touch still ages out; then updated_at / created_at for legacy rows.
        # Materialize pks — QuerySet.update() cannot run on annotated querysets.
        target_ids = list(
            base.annotate(
                _archive_ts=Coalesce('resolution_date', 'updated_at', 'created_at'),
            )
            .filter(_archive_ts__lte=cutoff)
            .values_list('pk', flat=True)
        )

    # PLS-467 fences is_archived and writes an outbox row per UPDATE. Draining
    # multi-thousand terminal rows that way OOMs/timeouts. Soft-archive is
    # housekeeping (hide finished work from default queues), not a live state
    # transition agents must observe — use session_replication_role=replica to
    # skip user triggers for this bulk write only (operator 2026-07-27).
    from django.db import connection, transaction

    now = timezone.now()
    archived = 0
    if target_ids:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '300s'")
                cursor.execute("SET LOCAL session_replication_role = 'replica'")
            # Triggers stay disabled for the rest of this atomic block.
            archived = Item.objects.filter(pk__in=target_ids, is_archived=False).update(
                is_archived=True,
                archived_at=now,
            )
    if archived:
        logger.info(
            'Archived %s terminal/done task(s) (days=%s, statuses=%s, bulk=replica)',
            archived,
            effective_days,
            len(terminal_slugs),
        )
    return {
        'archived': archived,
        'days': effective_days,
        'status_count': len(terminal_slugs),
    }


@shared_task(bind=True, max_retries=1)
def sweep_stale_offline_assignments(self, hours: int | None = None):
    """PLAT-4963: comment when actionable work is stale on an offline agent."""
    import os
    from datetime import timedelta
    from django.utils import timezone
    from .models import Item, Comment
    from .services.actionable_items import (
        WAITING_QUEUE_STATUS_SLUGS,
        filter_active_queue_items,
    )
    try:
        from .views import _scrape_agent_health
    except Exception:
        _scrape_agent_health = None

    effective_hours = int(hours or os.environ.get('PULSE_STALE_OFFLINE_ASSIGNMENT_HOURS', '24'))
    cutoff = timezone.now() - timedelta(hours=effective_hours)
    health = _scrape_agent_health() if _scrape_agent_health else {}

    def short(email):
        email = (email or '').strip().lower()
        if email.endswith('@agents.shizuha.io'):
            return email[:-len('@agents.shizuha.io')]
        if email.endswith('@shizuha.com'):
            return email[:-len('@shizuha.com')]
        return email.split('@', 1)[0]

    alerted = skipped = 0
    # PLS-445: start from the exact same active-queue predicate as
    # pulse_get_my_tasks. An item deliberately suppressed from the assignee's
    # queue (for example an Implementation-Linked RFC with an active named
    # implementation artifact) must never blame that assignee as stale/offline.
    qs = filter_active_queue_items(
        Item.objects.filter(
            mode='task',
            assignee_email__isnull=False,
            updated_at__lt=cutoff,
        ).exclude(assignee_email='')
    ).exclude(status__in=WAITING_QUEUE_STATUS_SLUGS)
    for item in qs.iterator():
        agent = short(item.assignee_email)
        h = health.get(agent) or {}
        if (h.get('status') or 'active') == 'active':
            skipped += 1
            continue
        meta = item.metadata or {}
        key = f'{agent}:{effective_hours}h'
        if meta.get('stale_offline_assignment_alert') == key:
            skipped += 1
            continue
        # PLAT-8169 arm (b): the sweep must judge FIRE-time state, not the
        # selection-time row. A disposition that landed between queryset
        # selection and this iteration (Deploy Verified close, reroute,
        # waiting-park) bumps updated_at / changes assignee / leaves the
        # active band — delivering the alert then would blame an assignee
        # for a task that is already settled (PLAT-7787 fired after its
        # close). Re-run the exact same active-queue predicate against a
        # fresh fetch of this row; anything that moved is suppressed.
        live = (
            filter_active_queue_items(
                Item.objects.filter(mode='task', pk=item.pk).exclude(
                    assignee_email=''
                )
            )
            .filter(updated_at__lt=cutoff, assignee_email=item.assignee_email)
            .exclude(status__in=WAITING_QUEUE_STATUS_SLUGS)
            .first()
        )
        if live is None:
            skipped += 1
            continue
        item = live
        # PLAT-940 mirror / PLS-1353 (sara instance 09-27, HIVE-2703): the
        # static active-queue predicate cannot know every workflow-specific
        # terminal slug (e.g. `resolved_elsewhere`) — re-check terminality on
        # the freshly-fetched row before alerting or failing over. A concrete
        # assignment changer must never target terminal work, and an alert on
        # an already-settled task would blame an assignee for settled work.
        if _is_terminal_status(item.status):
            skipped += 1
            logger.info(
                "PLAT-940 mirror: skipping terminal-status item %s (%s) "
                "from stale-offline sweep",
                getattr(item, 'item_key', item.pk), item.status,
            )
            continue
        note = (
            f'[stale-assignment alert] {item.assignee_email} appears offline/unavailable '
            f'(status={h.get("status") or "unknown"}) and this non-blocked task has had no '
            f'Pulse movement for >{effective_hours}h. Team lead/operator should reroute or recover the agent.'
        )
        Comment.objects.create(item=item, author_id=0, author_email='system@pulse', content=note)

        # Operator directive 2026-09-17 (urgent-blocker guarantee): for
        # urgent/high items, an alert is not enough — the stale offline
        # assignment is FAILED OVER to an active capable seat immediately
        # (PLAT-7421 class: an urgent root blocking 47 tasks sat in_progress
        # on a hibernated seat for a day because its linked PR exempted it
        # from the in-flight failover). Normal-priority items keep the
        # comment-only behavior.
        # PLAT-11803 exception: identity-bound cards (label `plat-999`
        # co-sign, or a generic `identity-bound` marker) must NOT auto-failover
        # to a different identity — the co-signer set is the authority, so a
        # rerouted card lands on a seat that can never green the gate
        # (PLAT-11771: sora → hiro 4 minutes after creation). Keep the
        # assignee and raise a wake signal instead; failover only within the
        # named co-signer set.
        from .label_utils import canonical_label_set
        _labels = canonical_label_set(item.labels)
        is_identity_bound = 'plat-999' in _labels or 'identity-bound' in _labels
        if (item.priority or '').strip().lower() in ('urgent', 'high'):
            try:
                from .services.workflow_service import WorkflowService as _WS
                if is_identity_bound:
                    # PLAT-11803: failover only within the named co-signer set
                    # (sora ↔ aoi). Cross-identity rerouting is forbidden — the
                    # card stays on its co-signer and a wake signal is raised
                    # instead when no set member is active.
                    uid, email = _resolve_plat999_cosigner_failover_target(item, health)
                    if not email:
                        old = item.assignee_email
                        from .services.mechanical_comments import (
                            MechanicalCommentFactory,
                            deterministic_event_id,
                        )
                        MechanicalCommentFactory.create(
                            item=item,
                            origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                            origin_event='assignment.identity_bound_wake',
                            template_version=1,
                            structured_payload={
                                'priority': (item.priority or '').strip(),
                                'prior_email': str(old),
                            },
                            origin_event_id=deterministic_event_id(
                                'assignment.identity_bound_wake', item, old, effective_hours,
                            ),
                        )
                        meta = item.metadata or {}
                        if meta.get('identity_bound_wake_requested') != old:
                            meta['identity_bound_wake_requested'] = old
                            meta['identity_bound_wake_requested_at'] = timezone.now().isoformat()
                            item.metadata = meta
                            item.save(update_fields=['metadata', 'updated_at'])
                        # Mint the wake/lease attestation for real (PLAT-11803
                        # review 6710 secondary (a)): the Hive GLM lease planner
                        # cannot see task labels, so this Redis marker is the
                        # typed Pulse-side attestation that a READY identity-bound
                        # card is owned by exactly this identity. Hive gates its
                        # identity_bound_priority seatel on this payload.
                        import json as _json
                        try:
                            _WS._redis_client().setex(
                                PLAT999_COSIGNER_WAKE_REDIS_KEY,
                                PLAT999_COSIGNER_WAKE_TTL_S,
                                _json.dumps({
                                    'assignee_email': item.assignee_email,
                                    'item_key': getattr(item, 'item_key', str(item.pk)),
                                    'requested_at': timezone.now().isoformat(),
                                }),
                            )
                        except Exception:
                            logger.warning(
                                'plat999 co-signer wake publish failed for %s',
                                getattr(item, 'item_key', item.pk), exc_info=True)
                        logger.warning(
                            'sweep_stale_offline_assignments: %s identity-bound wake requested for %s',
                            getattr(item, 'item_key', item.pk), old)
                else:
                    uid, email = _WS._resolve_team_assignee(
                        item.assignment_group,
                        exclude_email=item.assignee_email,
                        item=item,
                        project_key=(item.project.key if item.project_id else None),
                        enforce_cap=False,
                    )
                if email:
                    old = item.assignee_email
                    item.assignee_id = uid
                    item.assignee_email = email
                    item._activity_actor = (0, 'scheduler@pulse.shizuha.com')
                    item._activity_status_action = 'stale_offline_failover'
                    item.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
                    Comment.objects.create(
                        item=item, author_id=0, author_email='system@pulse',
                        content=(
                            f'[stale-assignment failover] {item.priority} item failed over '
                            f'{str(old).split("@")[0]} -> {str(email).split("@")[0]}: the previous '
                            f'owner was offline and this task had no movement for '
                            f'>{effective_hours}h. Urgent blockers must always have an active owner.'
                        ),
                    )
                    logger.warning(
                        'sweep_stale_offline_assignments: %s failed over %s -> %s',
                        getattr(item, 'item_key', item.pk), old, email)
                    if is_identity_bound:
                        # Within-set failover: the identity the wake marker
                        # attests to no longer owns the ready card — clear it.
                        _clear_plat999_cosigner_wake()
            except Exception:
                logger.exception('sweep_stale_offline_assignments: failover failed for %s',
                                 getattr(item, 'item_key', item.pk))

        # PLS-160: this previously ALSO created a separate task.generic "alert" Item per
        # stale source task. Those alert Items were created without an item_key (especially
        # for project-less source tasks) → pulse_get_task could not resolve them, surfacing
        # as N/A "ghost" tasks; and they were routed by `assignment_group` straight onto
        # CoS's queue, flooding it (206 created, ~80 to CoS). The source-task comment above
        # already surfaces the staleness on the actual stuck task, and the rebalance /
        # availability sweeps already reroute stale work off offline agents — so the
        # separate alert Item was redundant AND harmful, and is removed. (A future routed
        # alert would need a real item_key + a per-cycle volume cap — PLS-160 follow-up.)

        meta['stale_offline_assignment_alert'] = key
        meta['stale_offline_assignment_alerted_at'] = timezone.now().isoformat()
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
        alerted += 1
    logger.warning('sweep_stale_offline_assignments: alerted=%d skipped=%d hours=%d', alerted, skipped, effective_hours)
    return {'alerted': alerted, 'skipped': skipped, 'hours': effective_hours}


@shared_task(bind=True)
def sweep_stale_reviews(self, hours: int | None = None):
    """
    Backstop for orphaned review work: any in_review item with no update in
    `hours` (default 48, override via STALE_REVIEW_HOURS) is re-driven so it
    can't silently rot:
      1) if it has an assignment_group, re-resolve a concrete available
         reviewer in that group (never the operator — a personal-org review
         roster of [CEO] previously elected him as ProjectTeamLead);
      2) if no compatible reviewer resolves, hold group-queued on the review
         lane (PLS-461 starvation), not Admin Ops / HO;
      3) items with no assignment_group escalate to Admin Ops (CoS), never
         the operator.
    Always drops a comment. Idempotent via a metadata flag so it fires once.
    """
    import os
    from datetime import timedelta
    from .models import Item, Comment
    from .services.workflow_service import WorkflowService

    effective_hours = int(hours) if hours is not None else int(os.environ.get('STALE_REVIEW_HOURS', '48'))
    cutoff = timezone.now() - timedelta(hours=effective_hours)
    qs = Item.objects.filter(status='in_review', is_archived=False, updated_at__lte=cutoff)
    healed = escalated = 0
    for item in qs:
        meta = item.metadata or {}
        if meta.get('stale_review_escalated'):
            continue
        # PLAT-992: a stale in_review task whose linked PR is already closed/merged
        # has nothing left to review — re-routing it to a reviewer loops forever
        # (closed PR #128 was re-routed to the review team 5×). Skip the reviewer
        # re-route and surface it once for terminal resolution instead.
        prs = meta.get('pull_requests') or []
        pr_terminal = bool(prs) and all(
            (p.get('state') or '').lower() in ('closed', 'merged') or p.get('merged_at')
            for p in prs
        )
        if pr_terminal:
            if not meta.get('stale_review_pr_terminal'):
                try:
                    merged = any(
                        p.get('merged_at') or (p.get('state') or '').lower() == 'merged'
                        for p in prs
                    )
                    note = (
                        "⏰ Auto: in review with no movement, but the linked PR is "
                        f"{'merged' if merged else 'closed'} — nothing left to review. "
                        "NOT re-routing to a reviewer; move this task to its terminal/"
                        "verification state (a closed-PR review task must not loop in review). [PLAT-992]"
                    )
                    Comment.objects.create(item=item, author_id=0, author_email='system@pulse', content=note)
                    meta['stale_review_pr_terminal'] = True
                    meta['stale_review_pr_terminal_at'] = timezone.now().isoformat()
                    item.metadata = meta
                    item.save(update_fields=['metadata', 'updated_at'])
                except Exception as e:
                    logger.warning(f"sweep_stale_reviews: PLAT-992 closed-PR guard failed on {item.item_key}: {e}")
            continue
        try:
            from .services.routing_authority import clear_assignment_to_group_queue

            uid, email = (None, None)
            item._activity_actor = (0, 'system@pulse')
            if item.assignment_group:
                uid, email = WorkflowService._resolve_team_assignee(
                    item.assignment_group,
                    exclude_email=item.created_by_email,
                    project_key=WorkflowService._item_project_key(item),
                    item=item,
                    allow_operator=False,
                )
            operator_hit = bool(
                email and WorkflowService._is_operator_target(uid, email)
            )
            if uid and email and not operator_hit:
                applied_uid, applied_email = WorkflowService._apply_resolved_assignee(
                    item, uid, email, [], operator_authorized=False,
                )
                if applied_email and not WorkflowService._is_operator_target(
                        applied_uid, applied_email):
                    note = (
                        f"⏰ Auto: in review with no movement for {effective_hours}h — "
                        f"re-routed to {applied_email} (least-loaded in "
                        f"'{item.assignment_group}'). Please review or request changes."
                    )
                    healed += 1
                else:
                    operator_hit = True
            if not uid or not email or operator_hit:
                if item.assignment_group:
                    clear_assignment_to_group_queue(
                        item, reason='review_starvation', save=False,
                    )
                    note = (
                        f"⏰ Auto: in review with no movement for {effective_hours}h and no "
                        f"compatible reviewer resolved — held group-queued on "
                        f"'{item.assignment_group}'. Not routed to the operator. "
                        f"Review lead notified; staffing the exact-org review lane unblocks this."
                    )
                    healed += 1
                else:
                    lead = WorkflowService._resolve_team_lead_email(
                        'admin-ops',
                        item=item,
                    )
                    if lead and not WorkflowService._is_operator_target(None, lead):
                        item.assign({'email': lead}, save=False)
                    note = (
                        f"⏰ Auto-escalation: in review with no movement for {effective_hours}h "
                        f"and no reviewer could be resolved — routed to Admin Ops for a "
                        f"human to unblock."
                    )
                    escalated += 1
            Comment.objects.create(item=item, author_id=0, author_email='system@pulse', content=note)
            meta['stale_review_escalated'] = True
            meta['stale_review_escalated_at'] = timezone.now().isoformat()
            item.metadata = meta
            item.save(update_fields=['assignee_id', 'assignee_email', 'assignment_group', 'metadata', 'updated_at'])
        except Exception as e:
            logger.warning(f"sweep_stale_reviews: failed on {item.item_key}: {e}")
    if healed or escalated:
        logger.info(f"sweep_stale_reviews: re-routed {healed}, escalated {escalated} stale in_review items (>{effective_hours}h)")
    return {'healed': healed, 'escalated': escalated, 'hours': effective_hours}


@shared_task(bind=True, max_retries=1)
def sweep_stale_human_ops_blockers(self, hours: int | None = None):
    """PLAT-724: backstop for human-ops escalation tasks that never got an assignee.

    pulse_create_blocker_task fires /assign/ synchronously, but if the operator
    is transiently unavailable at escalation time the task may sit group-queued
    forever. This sweep finds human-ops tasks in open/pending state with no
    concrete assignee older than `hours` (default 4) and re-attempts the
    operator assignment. Idempotent via metadata flag.
    """
    import os
    from datetime import timedelta
    from .models import Item, Comment
    from .services.workflow_service import WorkflowService

    effective_hours = int(hours) if hours is not None else int(
        os.environ.get('STALE_HUMAN_OPS_HOURS', '4')
    )
    cutoff = timezone.now() - timedelta(hours=effective_hours)

    qs = Item.objects.filter(
        assignment_group__in=['human-ops', 'human_ops'],
        status__in=['open', 'pending'],
        assignee_id=None,
        is_archived=False,
        created_at__lte=cutoff,
    ).select_related('project')

    resolved = skipped = 0
    for item in qs:
        meta = item.metadata or {}
        if meta.get('human_ops_sweep_resolved'):
            skipped += 1
            continue
        try:
            uid, email = WorkflowService._resolve_team_assignee(
                'human-ops', allow_operator=True,
                project_key=WorkflowService._item_project_key(item),
                item=item,
            )
            if uid or email:
                item.assign({'id': uid, 'email': email}, save=False)
                note = (
                    f"⏰ PLAT-724 sweep: human-ops escalation unassigned for "
                    f"{effective_hours}h — re-routed to {email or uid} (operator)."
                )
                resolved += 1
            else:
                note = (
                    f"⏰ PLAT-724 sweep: human-ops escalation unassigned for "
                    f"{effective_hours}h and operator could not be resolved — "
                    f"task remains group-queued. Operator action required."
                )
            Comment.objects.create(item=item, author_id=0, author_email='system@pulse', content=note)
            meta['human_ops_sweep_resolved'] = True
            meta['human_ops_sweep_at'] = timezone.now().isoformat()
            item.metadata = meta
            save_fields = ['assignee_id', 'assignee_email', 'metadata', 'updated_at']
            item.save(update_fields=save_fields)
        except Exception as exc:
            logger.warning('sweep_stale_human_ops_blockers: failed on %s: %s', item.item_key, exc)
    if resolved or skipped:
        logger.info(
            'sweep_stale_human_ops_blockers: resolved=%d skipped=%d (>%dh unassigned human-ops tasks)',
            resolved, skipped, effective_hours,
        )
    return {'resolved': resolved, 'skipped': skipped, 'hours': effective_hours}


@shared_task(bind=True)
def sweep_resolved_blocked_items(self, limit: int = 200):
    """
    Backstop for blocked items with issue-link blockers. If a blocked item has
    at least one blocking link and all linked blockers are terminal, restore it
    to its pre-block working state.

    Comment-only / external blockers are intentionally ignored because Pulse
    cannot prove those are resolved.
    """
    from .models import Item
    from .services.workflow_service import WorkflowService

    from .services.deferred_workflow import relay_pending
    relay_pending(limit=limit)

    resumed = []
    skipped_active = 0
    skipped_unlinked = 0

    qs = (
        Item.objects
        .filter(status='blocked', is_archived=False)
        .order_by('updated_at')[:limit]
    )
    for item in qs:
        try:
            if not WorkflowService._has_blocking_links(item):
                skipped_unlinked += 1
                continue
            active_blockers = WorkflowService._active_blockers_for(item)
            if active_blockers:
                skipped_active += 1
                continue
            if WorkflowService._restore_blocked_item(item, blocker=None, user=None):
                resumed.append(item.item_key or str(item.id))
        except Exception as e:
            logger.warning(f"sweep_resolved_blocked_items: failed on {item.item_key}: {e}")

    if resumed:
        logger.info(f"sweep_resolved_blocked_items: resumed {len(resumed)} blocked items: {resumed}")
    return {
        'resumed': resumed,
        'skipped_active_blockers': skipped_active,
        'skipped_unlinked': skipped_unlinked,
        'limit': limit,
    }


@shared_task(bind=True)
def sweep_active_blocker_links(self, limit: int = 500):
    """
    Backstop for active blocker links whose blocked item is not in blocked.

    Normal link creation now auto-transitions the blocked item through the
    workflow's blocked path. This sweep catches old links and any import/script
    path that bypassed the signal.
    """
    from django.db.models import Q
    from .models import ItemLink
    from .services.workflow_service import WorkflowService

    blocked = []
    skipped = 0
    qs = (
        ItemLink.objects
        .filter(Q(link_type='blocks') | Q(link_type='is_blocked_by'))
        # PLS-158: skip pairs whose blocked item is already blocked — auto_block_from_link
        # would short-circuit anyway, but avoiding the DB round-trip also suppresses the
        # notification that fires before the short-circuit check (race window).
        # PLAT-1041: also skip pairs whose blocked item is already terminal (done, completed,
        # etc.) — auto_block_from_link short-circuits on terminal status but only AFTER
        # firing a spurious notification; exclude them here before they reach that path.
        .exclude(
            Q(link_type='is_blocked_by', source_item__status='blocked')
            | Q(link_type='blocks', target_item__status='blocked')
            | Q(link_type='is_blocked_by', source_item__status__in=_TERMINAL_ENUM_SLUGS)
            | Q(link_type='blocks', target_item__status__in=_TERMINAL_ENUM_SLUGS)
        )
        .select_related('source_item', 'target_item')
        .order_by('created_at')[:limit]
    )
    for link in qs:
        try:
            if WorkflowService.auto_block_from_link(link):
                blocked_item, blocker = WorkflowService._blocked_item_and_blocker_from_link(link)
                blocked.append({
                    'blocked': blocked_item.item_key or str(blocked_item.id),
                    'blocker': blocker.item_key or str(blocker.id),
                })
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"sweep_active_blocker_links: failed on link {link.id}: {e}")

    if blocked:
        logger.info(f"sweep_active_blocker_links: blocked {len(blocked)} items: {blocked}")
    return {
        'blocked': blocked,
        'skipped': skipped,
        'limit': limit,
    }


@shared_task(bind=True)
def sweep_design_handoff(self, days: int | None = None):
    """
    Autonomous design -> implementation handoff. Any recently-completed HLD/LLD/design
    task that has NO implementation follow-on gets an implementation task auto-created
    for the engineering pod, linked back to the design. Closes the silent-stall gap
    where an approved design never reaches engineering (the Hive HLDs sat `done` with
    nothing built). Idempotent via metadata.impl_spawned. Window via DESIGN_HANDOFF_DAYS.
    """
    import os
    import re
    from datetime import timedelta
    from .models import Item, Comment, ItemLink
    from .services.workflow_service import WorkflowService

    days = int(days) if days is not None else int(os.environ.get('DESIGN_HANDOFF_DAYS', '14'))
    cutoff = timezone.now() - timedelta(days=days)
    DONE = ['done', 'closed', 'resolved', 'completed', 'merged']
    spawned = 0
    for d in Item.objects.filter(mode='task', status__in=DONE, updated_at__gte=cutoff):
        title = (d.title or '')
        is_design = bool(re.match(r'^\s*(HLD|LLD)\b', title, re.I)) or (d.item_type or '') in ('design', 'architecture')
        if not is_design:
            continue
        meta = d.metadata or {}
        if meta.get('impl_spawned'):
            continue
        # Already has an implementation follow-on? (child task, or a linked Implement/Build task)
        has_impl = Item.objects.filter(parent_item=d).exists()
        try:
            for l in list(ItemLink.objects.filter(source_item=d)) + list(ItemLink.objects.filter(target_item=d)):
                o = l.target_item if l.source_item_id == d.id else l.source_item
                if o and re.match(r'^\s*(implement|build)\b', (o.title or ''), re.I):
                    has_impl = True
        except Exception:
            pass
        if has_impl:
            meta['impl_spawned'] = 'preexisting'
            d.metadata = meta
            d.save(update_fields=['metadata'])
            continue
        impl_title = ('Implement: ' + re.sub(r'^\s*(HLD|LLD)\s*[:\-]\s*', '', title, flags=re.I))[:200]
        try:
            # PLAT-236: auto-spawned impl tasks are NEW work — WIP-cap enforced.
            # All engineers at cap → created group-only (capacity queue).
            uid, email = WorkflowService._resolve_team_assignee(
                'engineering', exclude_email=None, enforce_cap=True,
                project_key=WorkflowService._item_project_key(d), item=d,
            )
        except Exception:
            uid, email = None, None
        try:
            key = d.project.get_next_item_key() if d.project else None
            ni = Item.objects.create(
                project=d.project, item_key=key, title=impl_title, mode='task',
                status='open', workflow_name='autonomous-dev', assignment_group='engineering',
                created_by_email='claude@shizuha.com', created_by_id=0,
                description=(f'Auto-spawned implementation task for approved design **{d.item_key}** ({title}). '
                            f'Build it per the HLD (see {d.item_key}); break into sub-tasks as needed. '
                            f'This handoff is automatic — approved designs no longer stall before engineering.'),
                metadata={'spawned_from': d.item_key},
            )
            if uid or email:
                ni.assign({'id': uid, 'email': email}, save=True)
            try:
                ItemLink.objects.create(source_item=d, target_item=ni, link_type='blocks')
            except Exception:
                pass
            meta['impl_spawned'] = ni.item_key
            d.metadata = meta
            d.save(update_fields=['metadata'])
            Comment.objects.create(item=d, author_id=0, author_email='claude@shizuha.com',
                content=f'Auto design→impl handoff: spawned **{ni.item_key}** ("{impl_title}") for the engineering pod ({email or "engineering"}). Approved designs no longer stall without implementation.')
            spawned += 1
        except Exception as e:
            logger.warning(f"sweep_design_handoff: failed to spawn impl for {d.item_key}: {e}")
    if spawned:
        logger.info(f"sweep_design_handoff: spawned {spawned} implementation tasks")
    return {'spawned': spawned}


@shared_task(bind=True)
def sweep_replenish_ready(self, ready_target: int | None = None):
    """
    Replenishment / Definition-of-Ready gate (PLS-168) — the upstream complement to
    sweep_rebalance_pods. Industry model: Kanban *replenishment cadence* + the
    orchestrator-worker *epic decomposition* pattern. The rebalance sweep PULLS ready
    work evenly across members; this sweep FEEDS the ready queue so a pull-based team
    can't sit idle while epics pile in its backlog (exactly the Architecture symptom:
    0 ready tasks, 50+ undecomposed epics, agents starving).

    Per team, ONLY when an available under-cap member exists (reusing the rebalancer's
    _resolve_team_assignee cap/availability gate — so we never push past WIP limits or
    onto offline/stopped agents) AND the team's READY queue (workable, non-epic
    open/todo tasks) is below REPLENISH_READY_TARGET: take the team's highest-priority
    UNGROOMED epics (umbrella items with no non-terminal child yet) and spawn ONE
    'Groom & decompose' task each — assigned to the least-loaded available member, so
    successive spawns fan out across the team. An architect then breaks the epic into
    sprint-sized ready child stories (each routed to its owning team) that the rebalancer
    distributes. Output is BOUNDED to the ready deficit, so it can never flood a team —
    the queue is fed to match capacity (the whole point of a pull system), then the next
    cycle tops it up as work completes.

    Idempotent: an epic already in decomposition (any non-terminal child, incl. a prior
    grooming task) or flagged metadata.groom_spawned is skipped. Also LIFTS epics off the
    coordinator/CoS identity (_COS_EMAIL) onto the owning team — the coordinator is a
    governance role, not a worker, so it must not anchor a team's epics (operator
    2026-06-29). Safe to run repeatedly and as the one-time unstick.
    """
    import os
    from django.db import transaction
    from django.db.models import Q
    from .models import Item, Comment, ItemLink, TeamRoster
    from .services.workflow_service import WorkflowService

    target = int(ready_target) if ready_target is not None else int(os.environ.get('REPLENISH_READY_TARGET', '3'))
    max_per_team = int(os.environ.get('REPLENISH_MAX_PER_TEAM', '5'))
    COORD = (getattr(WorkflowService, '_COS_EMAIL', 'shizuha@shizuha.com') or '').lower()
    excluded = set(WorkflowService._assignment_excluded_statuses())
    # Umbrella predicate catches item_type=epic + [EPIC]/[STANDING]/... bracket titles;
    # ALSO catch the bare "EPIC:"/"Epic " colon/space titles the bracket form misses
    # (our roadmap epics are titled "EPIC: ..." with no brackets).
    epic_q = WorkflowService._umbrella_item_q() | Q(title__istartswith='epic')
    READY = ['open', 'todo']
    PRIORITY_RANK = {'urgent': 0, 'high': 1, 'medium': 2, 'normal': 2, 'low': 3}

    def _is_decomposed(target):
        """True when the epic already has real decomposition. 2026-07-02: a
        link to another EPIC is provenance, not decomposition — the PLAT-197
        seeding epic linked itself to the roadmap epics it created, leaving 15
        never-groomed epics (BKS/FIN/INV/MAIL/SRCH/CON…) invisible to
        replenishment. Only parent_item children (excl. our own grooming task)
        or a link to a NON-epic work item count. Shared by the advisory
        pre-check AND the PLAT-1055 locked re-check so they can't disagree."""
        if Item.objects.filter(parent_item=target).exclude(
                title__startswith='Groom & decompose:').exists():
            return True
        linked_ids = set()
        for src, tgt in ItemLink.objects.filter(
            Q(source_item=target) | Q(target_item=target)
        ).values_list('source_item_id', 'target_item_id'):
            linked_ids.add(src)
            linked_ids.add(tgt)
        linked_ids.discard(target.pk)
        return bool(linked_ids) and Item.objects.filter(
            pk__in=linked_ids).exclude(epic_q).exists()

    spawned = 0
    reowned = 0
    details = {}

    teams = list(TeamRoster.objects.filter(is_active=True).values_list('team_name', flat=True))
    for team in teams:
        team_q = Q(assignment_group__iexact=team)
        ready_qs = Item.objects.filter(team_q, resolution__isnull=True, status__in=READY).exclude(epic_q)
        if ready_qs.count() >= target:
            continue
        epics = list(Item.objects.filter(epic_q, team_q, resolution__isnull=True).exclude(status__in=excluded))
        if not epics:
            continue
        epics.sort(key=lambda e: (PRIORITY_RANK.get((e.priority or 'normal'), 2), e.created_at))
        epic_iter = iter(epics)
        made = 0
        while made < max_per_team:
            if ready_qs.count() >= target:
                break
            # next UNGROOMED epic: one with NO existing decomposition by ANY linkage.
            # An epic counts as already decomposed (skip it) if it has child stories
            # via parent_item FK OR an ItemLink in EITHER direction — our roadmap
            # epics carry their slices as ItemLinked tasks, NOT parent_item children,
            # so a parent_item-only check spuriously groomed ~50 already-sliced
            # architecture epics (agents correctly closed them as no-ops = churn).
            # Count children of any status: an all-done decomposition is groomed too
            # (it needs closing, not grooming). Exclude our own grooming task from the
            # parent_item signal so a freshly-spawned groom child doesn't self-mask.
            epic = None
            for cand in epic_iter:
                if _is_decomposed(cand) or (cand.metadata or {}).get('groom_spawned'):
                    continue
                epic = cand
                break
            if epic is None:
                break
            # Resolve only after selecting the epic so duplicate team names use
            # that epic's project organization rather than the flattened global
            # roster. (None,None) means this org/team has no headroom.
            try:
                uid, email = WorkflowService._resolve_team_assignee(
                    team, enforce_cap=True,
                    project_key=WorkflowService._item_project_key(epic), item=epic,
                )
            except Exception:
                uid, email = None, None
            if not (uid or email):
                # Another same-named team in a different org may still be
                # routable; consume this epic for the current sweep and try the
                # next candidate instead of treating the name as one global pod.
                continue
            gtitle = ('Groom & decompose: ' + (epic.title or epic.item_key or 'epic'))[:200]
            try:
                with transaction.atomic():
                    # PLAT-1055: the pre-loop idempotency check is only advisory.
                    # Lock and re-read the epic before creating the groom task so two
                    # concurrent sweep workers cannot both pass check-then-create on
                    # the same stale metadata/child snapshot.
                    locked_epic = Item.objects.select_for_update().get(pk=epic.pk)
                    locked_meta = dict(locked_epic.metadata or {})
                    existing_groom = Item.objects.filter(
                        parent_item=locked_epic,
                        title__startswith='Groom & decompose:',
                        resolution__isnull=True,
                    ).exclude(status__in=excluded).order_by('created_at', 'id').first()
                    decomposed_now = _is_decomposed(locked_epic)
                    if decomposed_now or locked_meta.get('groom_spawned'):
                        continue
                    if existing_groom is not None:
                        # Self-heal old rows created before the metadata write (or by
                        # the racing worker) so future sweeps skip by both guards.
                        locked_meta['groom_spawned'] = existing_groom.item_key or str(existing_groom.id)
                        locked_epic.metadata = locked_meta
                        locked_epic.save(update_fields=['metadata'])
                        continue

                    # lift epic ownership off the coordinator/CoS identity onto the team
                    if (locked_epic.assignee_email or '').lower() == COORD:
                        try:
                            locked_epic.assign({'id': uid, 'email': email}, save=True)
                            reowned += 1
                            Comment.objects.create(
                                item=locked_epic, author_id=0, author_email='system@pulse',
                                content=(f'Epic ownership moved off the coordinator/CoS identity onto **{email or team}** — '
                                         f'architects own and decompose their team\'s epics; the coordinator is a governance '
                                         f'role, not a worker (PLS-168 replenishment).'))
                        except Exception:
                            pass

                    key = locked_epic.project.get_next_item_key() if locked_epic.project else None
                    gt = Item.objects.create(
                        project=locked_epic.project, item_key=key, title=gtitle, mode='task',
                        status='open', workflow_name='autonomous-dev', assignment_group=team,
                        parent_item=locked_epic, priority=(locked_epic.priority or 'high'),
                        created_by_email='claude@shizuha.com', created_by_id=0,
                        description=(
                            f'**Replenishment / Definition-of-Ready** (auto). The **{team}** ready queue is below '
                            f'target ({target}) while epic **{locked_epic.item_key or locked_epic.id}** ("{locked_epic.title}") sits '
                            f'undecomposed. Break this epic into sprint-sized, **ready** child stories — each with '
                            f'clear acceptance criteria and the correct `assignment_group` for its owning team — so the '
                            f'pull-based rebalancer can distribute them. **Decompose, do not implement here.** This task '
                            f'is auto-generated to feed the pull system so epics never starve the team again (PLS-168).'
                        ),
                        metadata={'replenish_groom_for': (locked_epic.item_key or str(locked_epic.id))},
                    )
                    if uid or email:
                        gt.assign({'id': uid, 'email': email}, save=True)
                    locked_meta['groom_spawned'] = gt.item_key or str(gt.id)
                    locked_epic.metadata = locked_meta
                    locked_epic.save(update_fields=['metadata'])
                    Comment.objects.create(
                        item=locked_epic, author_id=0, author_email='system@pulse',
                        content=(f'Replenishment: spawned grooming task **{gt.item_key or gt.id}** → {email or team} to '
                                 f'decompose this epic into ready child stories (PLS-168).'))
                spawned += 1
                made += 1
            except Exception as e:
                logger.warning(f"sweep_replenish_ready: failed to spawn groom task for {epic.item_key or epic.id}: {e}")
                break
        if made:
            details[team] = {'groom_spawned': made}
    if spawned or reowned:
        logger.info(f"sweep_replenish_ready: spawned {spawned} grooming task(s), re-owned {reowned} epic(s) "
                    f"off the coordinator across {len(details)} team(s)")
    return {'groom_spawned': spawned, 'epics_reowned': reowned, 'teams': details}


def _org_health_throughput_activity(activities):
    """Exclude configured exact human/system actors without publishing an owner."""
    import json
    configured = os.environ.get('PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS', '')
    try:
        excluded = json.loads(configured) if configured else []
        if (not isinstance(excluded, list) or any(not isinstance(email, str)
                or '@' not in email or any(character.isspace() for character in email)
                for email in excluded)):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('Invalid PULSE_THROUGHPUT_EXCLUDED_ACTOR_EMAILS configuration') from None
    excluded = {email.lower() for email in excluded}
    return [activity for activity in activities if activity.user_email
            and '@' in activity.user_email and 'pulse' not in activity.user_email
            and activity.user_email.lower() not in excluded]


def _org_health_throughput_window_minutes(now) -> int:
    """Return the no-activity window for throughput-flatline detection.

    Overnight IST is a legitimate lower-traffic period for the agent fleet, so a
    20m no-activity window is too sensitive there (PLS-33). Defaults are kept in
    env vars so ops can tune without a code deploy.
    """
    import os
    from datetime import timedelta

    normal = int(os.environ.get('PULSE_THROUGHPUT_WINDOW_MIN', '20'))
    overnight = int(os.environ.get('PULSE_THROUGHPUT_OVERNIGHT_WINDOW_MIN', '60'))
    start_h = int(os.environ.get('PULSE_THROUGHPUT_OVERNIGHT_START_IST_HOUR', '22'))
    end_h = int(os.environ.get('PULSE_THROUGHPUT_OVERNIGHT_END_IST_HOUR', '9'))
    ist_hour = (now + timedelta(hours=5, minutes=30)).hour
    if start_h <= end_h:
        in_overnight = start_h <= ist_hour < end_h
    else:
        in_overnight = ist_hour >= start_h or ist_hour < end_h
    return overnight if in_overnight else normal

def _org_health_fresh_agent_sessions(max_age_seconds: int) -> dict:
    """Read agent-health exporter session freshness for flatline suppression.

    Returns {fresh_count, fresh_agents}. Fails closed (0 fresh) when the exporter
    is unreachable or missing freshness metrics, so real flatlines still alert.
    """
    import os
    import re
    from tasks.net_utils import safe_urlopen  # PLAT-1574: scheme-guarded urlopen

    url = os.environ.get('PULSE_HEALTH_EXPORTER_URL',
                         'http://host.docker.internal:9888/metrics').strip()
    if not url or url.lower() in {'disabled', 'off', 'none'}:
        return {'fresh_count': 0, 'fresh_agents': []}
    try:
        with safe_urlopen(url, timeout=5) as resp:
            text = resp.read().decode('utf-8', 'replace')
    except Exception as e:
        logger.warning(f"sweep_org_health: freshness scrape failed ({url}): {e}")
        return {'fresh_count': 0, 'fresh_agents': []}

    # Metrics are per (agent, backend). Aggregate by that exact tuple first so
    # an up-but-stale backend cannot combine with a down-but-recent backend and
    # falsely mark the agent fresh. A missing enabled/wedge/provider metric keeps
    # the same forward-compatible behavior as sweep_agent_availability: it does
    # not by itself make an otherwise live backend unavailable.
    label_rx = re.compile(r'(\w+)="([^"]*)"')

    def _labels(blob: str) -> dict:
        return {k: v for k, v in label_rx.findall(blob)}

    sample_rx = re.compile(
        r'^shizuha_agent_(process_up|last_activity_seconds|enabled|wedge_errors|provider_healthy)'
        r'\{([^}]*)\}\s+([0-9.]+)',
        re.M,
    )
    by_backend = {}
    for metric, label_blob, raw in sample_rx.findall(text):
        labels = _labels(label_blob)
        agent = (labels.get('agent') or '').strip().lower()
        if not agent:
            continue
        backend = (labels.get('backend') or '_default').strip().lower() or '_default'
        rec = by_backend.setdefault((agent, backend), {
            'process_up': False,
            'enabled': True,
            'last': None,
            'wedge_errors': None,
            'provider_healthy': None,
        })
        val = float(raw)
        if metric == 'process_up':
            rec['process_up'] = rec['process_up'] or val >= 1.0
        elif metric == 'enabled':
            rec['enabled'] = rec['enabled'] and val >= 1.0
        elif metric == 'last_activity_seconds':
            rec['last'] = val if rec['last'] is None else min(rec['last'], val)
        elif metric == 'wedge_errors':
            rec['wedge_errors'] = val if rec['wedge_errors'] is None else max(rec['wedge_errors'], val)
        elif metric == 'provider_healthy':
            rec['provider_healthy'] = val if rec['provider_healthy'] is None else max(rec['provider_healthy'], val)

    wedge_threshold = int(os.environ.get('PULSE_WEDGE_THRESHOLD', '10'))
    fresh_agents = set()
    for (agent, _backend), rec in by_backend.items():
        wedged = rec['wedge_errors'] is not None and rec['wedge_errors'] >= wedge_threshold
        provider_down = rec['provider_healthy'] is not None and rec['provider_healthy'] < 1.0
        if (
            rec['process_up']
            and rec['enabled']
            and not wedged
            and not provider_down
            and rec['last'] is not None
            and rec['last'] <= max_age_seconds
        ):
            fresh_agents.add(f"{agent}@shizuha.com")

    fresh = sorted(fresh_agents)
    return {'fresh_count': len(fresh), 'fresh_agents': fresh}


def _org_health_exporter_fresh() -> bool:
    """PLAT-795: is the agent-health exporter signal trustworthy THIS cycle?

    sweep_org_health derives its whole-team-outage / capacity findings from
    _is_agent_available_for_assignment, which reads the Redis availability map
    published by sweep_agent_availability from this exporter. A single empty or
    malformed scrape makes every roster member read "down", so the outage loop
    fires one false task per team. sweep_rebalance_pods already refuses to act on
    such a signal (PLAT-319/348); mirror that gate here so the DETECTOR does too.

    Returns True only when the exporter is reachable AND its body carries a
    process_up sample. Fails CLOSED (False) on any unreachable / 5xx / empty /
    malformed response, so availability-derived findings are SKIPPED (never
    fired) on a bad signal. When scraping is explicitly disabled, returns True:
    availability then comes from static env policy, not a flaky live scrape, so
    the gate is a no-op. NOTE: this catches an absent/empty signal; a scrape that
    returns explicit (but wrong) process_up=0 for live agents is caught instead
    by the global-sanity + debounce guards in sweep_org_health."""
    import os
    from tasks.net_utils import safe_urlopen  # PLAT-1575: scheme-guarded urlopen
    url = os.environ.get('PULSE_HEALTH_EXPORTER_URL', '').strip()
    if not url or url.lower() in {'disabled', 'off', 'none'}:
        return True  # static env policy — no live signal to distrust
    try:
        with safe_urlopen(url, timeout=5) as resp:
            if resp.status >= 500:
                logger.warning(
                    f"sweep_org_health: exporter returned {resp.status} — skipping "
                    "availability-derived findings this cycle")
                return False
            body = resp.read().decode('utf-8', 'replace')
    except Exception as e:
        logger.warning(
            f"sweep_org_health: exporter unreachable ({e!r}) — skipping "
            "availability-derived findings this cycle")
        return False
    if 'shizuha_agent_process_up' not in body:
        logger.warning(
            "sweep_org_health: exporter 200 but no process_up sample — skipping "
            "availability-derived findings this cycle")
        return False
    return True


def _org_health_outage_streak(sig: str, threshold: int) -> int:
    """PLAT-795 debounce: count CONSECUTIVE detector cycles a signal stays active.

    Increments and returns the streak for `sig` (TTL-bounded so a long-ago blip
    doesn't accumulate). The caller files the finding only when the returned
    streak >= threshold, so a single transient bad scrape (one cycle of explicit
    process_up=0 for live agents — the 2026-06-24 19:45 pattern) cannot fire a
    task. Fails OPEN: on any Redis error returns `threshold` so a REAL outage is
    never silently suppressed by an infra hiccup."""
    import os
    try:
        from .services.workflow_service import WorkflowService
        rc = WorkflowService._redis_client()
        key = f"pulse:org_health_streak:{sig}"
        n = int(rc.incr(key))
        rc.expire(key, int(os.environ.get('PULSE_ORG_HEALTH_STREAK_TTL', '3600')))
        return n
    except Exception:
        return threshold  # fail-open: never suppress a real outage on Redis error


def _org_health_clear_streak(sig: str) -> None:
    """Reset a debounce streak once its condition clears, so the next occurrence
    starts fresh at 1. Best-effort; a TTL backstops any missed clear."""
    try:
        from .services.workflow_service import WorkflowService
        WorkflowService._redis_client().delete(f"pulse:org_health_streak:{sig}")
    except Exception:
        pass


def _org_health_absent_streak(sig: str, threshold: int) -> int:
    """PLS-854 clear-side debounce: count CONSECUTIVE detector cycles a fired
    outage signal is ABSENT.

    The fire path requires the outage conjunction to be present for 2+
    consecutive cycles before filing (PLAT-795); the clear side now requires the
    SAME number of consecutive absent cycles before a carrier terminalizes,
    instead of terminating on a single clean read. Fails SAFE (returns 0) on a
    Redis error so an infra hiccup can never fast-forward a clear — the opposite
    of the fire-side helper, which fails OPEN.
    """
    import os
    try:
        from .services.workflow_service import WorkflowService
        rc = WorkflowService._redis_client()
        key = f"pulse:org_health_absent_streak:{sig}"
        n = int(rc.incr(key))
        rc.expire(key, int(os.environ.get('PULSE_ORG_HEALTH_STREAK_TTL', '3600')))
        return n
    except Exception:
        return 0


def _org_health_reset_absent_streak(sig: str) -> None:
    """PLS-854: reset the clear-side absent counter while the signal is present
    again (a detected cycle must not count toward a clear)."""
    try:
        from .services.workflow_service import WorkflowService
        WorkflowService._redis_client().delete(f"pulse:org_health_absent_streak:{sig}")
    except Exception:
        pass


def _org_health_outage_clear_evidence(W, sig, now):
    """PLS-854: re-read the current lane of a firing signal at clear time and
    produce the clear comment evidence: which conjunction leg went false, the
    roster string, and the queued-item count.

    Returns a short markdown sentence, or None when the lane cannot be re-read
    (the caller falls back to the generic 'condition no longer detected').
    """
    try:
        from .services.routing_authority import routing_candidate_snapshot_for_item
        if sig.startswith('zero-member-outage:'):
            key = sig[len('zero-member-outage:'):]
        elif sig.startswith('zero-member-team:'):
            key = sig[len('zero-member-team:'):]
        else:
            return None
        parts = key.split(':', 1)
        org_id = int(parts[0]) if parts and parts[0].isdigit() else 1
        team = parts[1] if len(parts) > 1 and parts[1] else key
        if not team:
            return None
        queued = _org_health_routable_queue(list(
            W._queued_tasks_for_team(team, organization_id=org_id)
        ))
        members = []
        reasons = {}
        if queued:
            sample = queued[0]
            lane = routing_candidate_snapshot_for_item(team, sample)
            members = [
                str(r.get('email') or '').strip().lower()
                for r in lane.get('candidates') or []
                if isinstance(r, dict) and str(r.get('email') or '').strip()
            ]
            reasons = _org_health_candidate_availability(W, lane, sample)
        live = sum(
            1 for r in reasons.values()
            if 'seat_not_live' not in (r or [])
        )
        roster = ', '.join(
            f'{email}={("|".join(r) if r else "available")}'
            for email, r in sorted(reasons.items())
        ) or '(none)'
        if members:
            liveness = f'{live}/{len(members)} member(s) readable-available now'
            if live > 0 and not queued:
                legs = 'both legs cleared (availability leg AND queue leg)'
            elif live > 0:
                legs = f'availability leg cleared; queue leg still {len(queued)} task(s) queued'
            elif not queued:
                legs = f'queue leg cleared; availability leg still {len(members) - live}/{len(members)} down'
            else:
                legs = 'neither leg cleared (unexpected clear while condition still detected)'
        else:
            liveness = 'no members readable'
            legs = ('queue leg cleared' if not queued
                    else f'queue leg still {len(queued)} task(s) queued')
        return (
            f"signal absent for 2+ consecutive detector cycles — {legs}; "
            f"{liveness}; queued={len(queued)} at clear time; "
            f"roster=[{roster}] (PLS-854)"
        )
    except Exception:
        return None


@shared_task(bind=True, max_retries=1)
def sweep_blocked_retriage(self):
    """Autonomous blocked-task invariant — the system self-heals hoarded blocks.

    RULE (operator directive 2026-06-23, refined 2026-06-30): a `blocked` task is only
    legitimately blocked when it is blocked BY an active linked task (another
    non-terminal item) — which Pulse auto-resumes when that blocker closes — OR it is
    parked on the Admin Ops / Human Ops escalation queue awaiting triage of a real
    human-only decision. ANY other `blocked` task (an agent sitting on it with no linked
    blocker) was never legitimately blocked: it is hoarded/mis-parked and invisible to
    whoever could act. The system returns it to its pre-block actionable state + owner so
    it re-enters a READY queue, where the heartbeat + queue-hygiene skill force the owner
    to work it, route it (Raise to Admin Ops → admin-ops / Escalate to DevOps / reassign),
    or resolve it. No agent can hoard blocked work; no coordinator hand-routes. Generic
    mechanism — the routing policy stays in the workflow transitions the owner then fires.

    DRY-RUN by default (logs what it WOULD do). Set PULSE_BLOCKED_RETRIAGE_APPLY=true to
    act. Reuses the existing pre-block resume machinery (WorkflowService).
    """
    import os
    try:
        from .models import Item, Comment, ItemLink
        from .services.workflow_service import WorkflowService as W
    except Exception as e:
        logger.warning(f"sweep_blocked_retriage: import failed: {e}")
        return {'error': 'import'}
    apply = os.environ.get('PULSE_BLOCKED_RETRIAGE_APPLY', 'false').lower() == 'true'
    op_email = (getattr(W, '_OPERATOR_EMAIL', '') or '').strip().lower()
    TERM = {'done', 'cancelled', 'deferred', 'closed', 'resolved', 'merged',
            'rejected', 'completed', 'wont_fix', 'duplicate', 'expired'}
    legit = 0
    retriaged = []
    def _real_blocker(it, term, depth=0, seen=None):
        """True iff `it` has a blocker that is non-terminal AND not part of a block
        CYCLE back to `it` (a mutual/transitive deadlock A->B->...->A is NOT a real
        blocker — it can never resolve, so the task is hoarded, not legitimately
        blocked). A normal chain A->B->C(actionable) IS legit. Bounded DFS."""
        if seen is None:
            seen = set()
        if it.id in seen or depth > 12:
            return False  # cycle (or runaway) — this path is a deadlock, not a real blocker
        seen = seen | {it.id}
        # collect this item's blockers (both link directions)
        blockers = []
        for L in ItemLink.objects.filter(source_item=it, link_type__in=['is_blocked_by', 'blocked_by']):
            if L.target_item:
                blockers.append(L.target_item)
        for L in ItemLink.objects.filter(target_item=it, link_type='blocks'):
            if L.source_item:
                blockers.append(L.source_item)
        for b in blockers:
            st = (b.status or '').lower()
            if st in term:
                continue  # terminal blocker (resolved) — doesn't legitimise the block
            if st != 'blocked':
                # A non-terminal, non-blocked blocker (todo/in_progress/in_review/
                # backlog/scheduled/...) is a real linked dependency that will reach a
                # terminal state and auto-resume this task — legitimately blocked.
                return True
            # PLS-148 / 2026-06-30 refinement: a blocked blocker on the human
            # escalation queue is legitimate. It has no further linked blockers (it IS
            # the terminal ask), so recursion would falsely return False and
            # auto-unblock the upstream task.
            b_group = (b.assignment_group or '').strip().lower().replace('_', '-')
            if (op_email and (b.assignee_email or '').strip().lower() == op_email) or b_group in {'admin-ops', 'human-ops'}:
                return True
            # blocker is itself blocked: only real if ITS chain reaches a non-cyclic
            # actionable blocker (and doesn't cycle back to anything we've seen).
            if _real_blocker(b, term, depth + 1, seen):
                return True
            # PLS-149: A blocker in `blocked` status is itself non-terminal.
            # No further chain ≠ resolved; don't auto-unblock the upstream task.
            return True
        return False

    try:
        for it in Item.objects.filter(mode='task', status='blocked'):
            # (a) legitimately blocked: an ACTIVE, non-cyclic linked blocker chain.
            #     A mutual deadlock (A<->B) or a chain that cycles back is NOT legit —
            #     it never auto-resolves, so retriage it (breaking the cycle).
            has_blocker = _real_blocker(it, TERM)
            # (b) legitimately parked on an escalation queue for human/Admin Ops triage.
            group = (it.assignment_group or '').strip().lower().replace('_', '-')
            on_escalation_queue = (
                (op_email and (it.assignee_email or '').strip().lower() == op_email)
                or group in {'admin-ops', 'human-ops'}
            )
            if has_blocker or on_escalation_queue:
                legit += 1
                continue
            # No active linked blocker, not on the operator → hoarded. Return to ready.
            pre = W._pre_block_state_from_activity(it)
            resume_status = W._choose_resume_status(it, pre) or 'todo'
            if apply:
                try:
                    uid, email = W._resolve_unblock_assignee(it, pre)
                except Exception:
                    uid, email = (None, None)
                try:
                    it._activity_actor = (0, 'system@pulse')
                except Exception:
                    pass
                it.status = resume_status
                if uid or email:
                    it.assign({'id': uid, 'email': email}, save=False)
                it.save()
                try:
                    Comment.objects.create(
                        item=it, author_id=0, author_email='system@pulse',
                        content=(f'[autonomous re-triage] Auto-unblocked → {resume_status}: a `blocked` '
                                 f'task must be blocked BY an active linked task (none here) or be on an '
                                 f'Admin Ops / Human Ops escalation queue. It was neither — not legitimately blocked. Returned '
                                 f'to the ready queue: work it, route it (Raise to Admin Ops → admin-ops / '
                                 f'Escalate to DevOps / reassign to the owning team), or resolve it. It '
                                 f'cannot sit blocked without a linked blocker.'))
                except Exception:
                    pass
            retriaged.append(it.item_key)
    except Exception as e:
        logger.warning(f"sweep_blocked_retriage failed: {e}")
    logger.warning(f"sweep_blocked_retriage: legit_blocked={legit} "
                   f"{'APPLIED' if apply else 'DRYRUN'} retriaged({len(retriaged)})={retriaged}")
    return {'legit_blocked': legit, 'retriaged': retriaged, 'applied': apply}


def _stale_sweep_counts_payload(result=None, *, state='success', error='', backlog_non_empty=False):
    result = result or {}
    payload = {
        'state': state,
        'applied': bool(result.get('applied', False)),
        'marked': len(result.get('marked') or []),
        'closed': len(result.get('closed') or []),
        'flagged': len(result.get('flagged') or []),
        'cleared': len(result.get('cleared') or []),
        'exempted': int(result.get('exempted') or 0),
        'cap_hit': bool(result.get('cap_hit', False)),
        'item_errors': int(result.get('item_errors') or 0),
        'backlog_non_empty': bool(backlog_non_empty or result.get('backlog_non_empty', False)),
        'candidate_count': int(result.get('candidate_count') or 0),
        'error': str(error or result.get('error') or '')[:500],
        'at': timezone.now().isoformat(),
        'invariant': (
            'only non-exempt idle task items are marked/closed; exemptions always hold; '
            'stale closes per run never exceed PULSE_STALE_SWEEP_MAX_CLOSES'
        ),
    }
    payload['processed'] = (
        payload['marked'] + payload['closed'] + payload['flagged'] +
        payload['cleared'] + payload['exempted'] + payload['item_errors']
    )
    return payload


def _record_stale_sweep_health(result=None, *, state='success', error='', backlog_non_empty=False):
    """PLS-262/PLAT-1254: durable liveness + metric evidence for stale sweep."""
    payload = _stale_sweep_counts_payload(
        result, state=state, error=error, backlog_non_empty=backlog_non_empty,
    )
    try:
        from django.core.cache import cache
        from . import liveness
        payload['last_run_ts'] = liveness.record_heartbeat(liveness.STALE_SWEEP)
        cache.set('pulse:stale_sweep:last', payload, timeout=None)
        cache.set(f'pulse:stale_sweep:last_{state}', payload, timeout=None)
    except Exception:
        logger.exception('stale-sweep liveness cache write failed')
    logger.warning(
        'metric:stale_sweep_health state=%s applied=%s marked=%d closed=%d flagged=%d '
        'cleared=%d exempted=%d cap_hit=%s item_errors=%d processed=%d backlog_non_empty=%s candidate_count=%d error=%s',
        payload['state'], payload['applied'], payload['marked'], payload['closed'],
        payload['flagged'], payload['cleared'], payload['exempted'], payload['cap_hit'],
        payload['item_errors'], payload['processed'], payload['backlog_non_empty'],
        payload['candidate_count'], payload['error'],
    )
    return payload


def _notify_stale_sweep_owner(payload, *, title='Stale-sweep anomaly/failure'):
    """Fail-loud owner DM for stale sweep anomalies.

    Best-effort only: notification failure must not break the sweep, but the
    liveness/cache/log artifacts above remain for the meta-probe.
    """
    try:
        import requests as req
        from tasks.services.connect_origin import system_message_headers
        from tasks.services.notifications import UserNotificationService
        from tasks.services.workflow_service import WorkflowService
        from tasks.user_service import user_service

        lead_email = (
            WorkflowService._resolve_team_lead_email('product')
            or WorkflowService._resolve_team_lead_email('engineering')
            or 'sora@shizuha.com'
        )
        info = user_service.get_user_by_email(lead_email)
        user_id = info.get('id') if isinstance(info, dict) else None
        if not user_id:
            return False

        content = (
            '🔴 ANDON [PLS-262 stale-sweep health]\n'
            f"- state: `{payload.get('state')}`\n"
            f"- processed: `{payload.get('processed')}` "
            f"(marked={payload.get('marked')}, closed={payload.get('closed')}, "
            f"flagged={payload.get('flagged')}, cleared={payload.get('cleared')}, "
            f"exempted={payload.get('exempted')}, item_errors={payload.get('item_errors')})\n"
            f"- cap_hit: `{payload.get('cap_hit')}`; "
            f"backlog_non_empty: `{payload.get('backlog_non_empty')}`; "
            f"candidate_count: `{payload.get('candidate_count')}`\n"
            f"- error: `{payload.get('error') or ''}`\n"
            f"- invariant: {payload.get('invariant')}\n"
            '- liveness: `stale_sweep_last_run_ts` on `/statusz` + '
            '`pulse:stale_sweep:last_*` cache markers\n'
            '- need: inspect `sweep_stale_tasks` before enabling '
            '`PULSE_STALE_SWEEP_APPLY=true`.'
        )

        # Direct Connect system DM is the fail-loud path required by PLAT-1254.
        connect_base_url = getattr(
            settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api'
        ).rstrip('/')
        body = {
            'recipient_user_id': int(user_id),
            'content': content,
            'sender_name': 'Pulse Mechanism Health',
            'sender_service': 'shizuha-pulse',
        }
        resp = req.post(
            f'{connect_base_url}/internal/system-message/',
            json=body,
            headers=system_message_headers(body),
            timeout=5,
        )

        # Also persist/send through the existing alert notification channels.
        UserNotificationService().notify_user(
            int(user_id), title, content, event_type='alert', severity='error'
        )
        return 200 <= getattr(resp, 'status_code', 0) < 300
    except Exception:
        logger.exception('stale-sweep owner notification failed')
        return False


@shared_task(bind=True, max_retries=1)
def sweep_stale_tasks(self):
    """PLS-256 Backlog Custodian — the stale-bot sweep (daily beat).

    Non-terminal task items with NO human activity in STALE_DAYS get labeled
    `stale` + a comment naming the rule and a grace window. An item still `stale`
    and still idle past STALE_GRACE_DAYS is closed as stale. Fully reversible:
    any real (non-system) comment/transition since the mark clears `stale` and
    keeps the item open; a closed-as-stale item reopens on any new activity/
    transition per the workflow.

    NEVER marks/closes (exempt): labels containing pinned/security/recurring/
    deferred/epic, item_type=epic, is_recurring, any item with an ACTIVE
    is_blocked_by link, and high-value work (priority high/urgent OR story_points
    >= cap) — those are flagged to the custodian queue for a human, never
    auto-closed.

    Safety: DRY-RUN by default (logs what it WOULD do) unless
    PULSE_STALE_SWEEP_APPLY=true; a kill-switch PULSE_STALE_SWEEP_ENABLED; a hard
    per-run close cap PULSE_STALE_SWEEP_MAX_CLOSES so a config slip can never
    mass-close the backlog. Emits marked/closed/flagged/cleared/exempted counts.
    """
    import os
    from datetime import timedelta
    from django.utils.dateparse import parse_datetime
    try:
        from .models import Item, Comment, ItemActivity, ItemLink
    except Exception as e:
        logger.warning(f"sweep_stale_tasks: import failed: {e}")
        result = {'error': 'import', 'item_errors': 0, 'backlog_non_empty': False}
        payload = _record_stale_sweep_health(result, state='failure', error=f'import: {e}')
        _notify_stale_sweep_owner(payload, title='Stale-sweep failed before import')
        return result

    if os.environ.get('PULSE_STALE_SWEEP_ENABLED', 'true').lower() != 'true':
        logger.info("sweep_stale_tasks: disabled via PULSE_STALE_SWEEP_ENABLED")
        return {'skipped': 'kill-switch'}

    apply = os.environ.get('PULSE_STALE_SWEEP_APPLY', 'false').lower() == 'true'
    stale_days = int(os.environ.get('PULSE_STALE_DAYS', str(getattr(settings, 'PULSE_STALE_DAYS', 21))))
    grace_days = int(os.environ.get('PULSE_STALE_GRACE_DAYS', str(getattr(settings, 'PULSE_STALE_GRACE_DAYS', 7))))
    max_closes = int(os.environ.get('PULSE_STALE_SWEEP_MAX_CLOSES', str(getattr(settings, 'PULSE_STALE_SWEEP_MAX_CLOSES', 25))))
    sp_cap = int(os.environ.get('PULSE_STALE_STORY_POINT_CAP', str(getattr(settings, 'PULSE_STALE_STORY_POINT_CAP', 5))))
    if stale_days < 1 or grace_days < 1:
        logger.warning("sweep_stale_tasks: invalid stale/grace days, skipping")
        result = {'error': 'invalid config', 'item_errors': 0, 'backlog_non_empty': False}
        payload = _record_stale_sweep_health(result, state='failure', error='invalid stale/grace days')
        _notify_stale_sweep_owner(payload, title='Stale-sweep invalid config')
        return result

    SYSTEM_EMAIL = 'system@pulse'
    EXEMPT_LABEL_TOKENS = ('pinned', 'security', 'recurring', 'deferred', 'epic')
    HIGH_PRIORITY = {'high', 'urgent'}
    now = timezone.now()
    stale_cutoff = now - timedelta(days=stale_days)

    marked, closed, flagged, cleared = [], [], [], []
    exempted = 0
    item_errors = 0

    def _last_human_activity(item):
        """Latest non-system activity timestamp (the sweep's own system comments
        are excluded, so marking an item does NOT reset its staleness clock)."""
        ts = (ItemActivity.objects
              .filter(item=item)
              .exclude(user_email__iexact=SYSTEM_EMAIL)
              .order_by('-created_at')
              .values_list('created_at', flat=True)
              .first())
        return ts or item.created_at

    def _normalize_labels(raw, item_key='?'):
        """Canonicalize legacy label shapes to dict.

        Known shapes (returned as dict):
        - ``None`` / ``{}`` → ``{}``
        - ``{"key": "val"}`` → as-is
        - ``["a", "b"]`` (legacy list-of-strings) → ``{"a": "true", "b": "true"}``
        - ``"a"`` (legacy bare string) → ``{"a": "true"}``

        Unsupported shapes return ``(None, True)`` — the caller must treat the
        item as exempt and count an anomaly (fail-closed, never silent empty).

        Mixed list entries (e.g. ``["frontend", {"bad": true}]``) also return
        ``(None, True)`` — partial success would silently drop data and hide
        the anomaly from counters.
        """
        if raw is None or raw == {}:
            return {}, False
        if isinstance(raw, dict):
            return raw, False
        if isinstance(raw, list):
            out = {}
            for entry in raw:
                if isinstance(entry, str) and entry.strip():
                    out[entry.strip()] = 'true'
                else:
                    # Mixed/unsupported entry — fail closed entirely.
                    # Partial success would silently drop data and hide the
                    # anomaly from counters.
                    logger.warning(
                        f"sweep_stale_tasks: item {item_key} has mixed/unsupported list entry "
                        f"(type={type(entry).__name__}, repr={entry!r:.120}); treating as exempt")
                    return None, True
            return out, False
        if isinstance(raw, str) and raw.strip():
            return {raw.strip(): 'true'}, False
        # Unsupported shape — fail closed.
        logger.warning(
            f"sweep_stale_tasks: item {item_key} has unsupported labels shape "
            f"(type={type(raw).__name__}, repr={raw!r:.120}); treating as exempt")
        return None, True

    def _is_exempt(item, labels):
        if item.is_recurring:
            return True
        it_type = (item.item_type or '').lower()
        if it_type == 'epic' or it_type.startswith('epic.'):
            return True
        if labels is None:
            return True  # fail-closed: unsupported shape → exempt
        for k, v in labels.items():
            token = f"{k} {v}".lower()
            if any(tok in token for tok in EXEMPT_LABEL_TOKENS):
                return True
        # An ACTIVE (non-terminal) blocker link is a legitimate reason to be idle.
        for L in ItemLink.objects.filter(source_item=item, link_type__in=['is_blocked_by', 'blocked_by']):
            if L.target_item and (L.target_item.status or '').lower() not in _TERMINAL_ENUM_SLUGS:
                return True
        for L in ItemLink.objects.filter(target_item=item, link_type='blocks'):
            if L.source_item and (L.source_item.status or '').lower() not in _TERMINAL_ENUM_SLUGS:
                return True
        return False

    def _human_in_loop(item):
        """High-value work is never auto-closed — flag it for a human instead."""
        if (item.priority or '').lower() in HIGH_PRIORITY:
            return True
        if item.story_points is not None and item.story_points >= sp_cap:
            return True
        return False

    def _mark_stale(item, labels):
        item._activity_actor = (0, SYSTEM_EMAIL)
        item.labels = {**(labels or {}), 'stale': 'true'}
        item.metadata = {**(item.metadata or {}), 'stale_marked_at': now.isoformat()}
        item.save(update_fields=['labels', 'metadata', 'updated_at'])
        Comment.objects.create(
            item=item, author_id=0, author_email=SYSTEM_EMAIL,
            content=(f'[backlog-custodian] Marked `stale`: no activity in {stale_days} days. '
                     f'This is reversible — any comment or transition clears `stale` and keeps '
                     f'this open. If it stays idle, it will be closed as stale after a '
                     f'{grace_days}-day grace window.'))

    def _clear_stale(item, labels):
        item._activity_actor = (0, SYSTEM_EMAIL)
        item.labels = {k: v for k, v in (labels or {}).items() if k != 'stale' and k != 'needs-custodian-review'}
        meta = {k: v for k, v in (item.metadata or {}).items() if k != 'stale_marked_at'}
        item.metadata = meta
        item.save(update_fields=['labels', 'metadata', 'updated_at'])

    def _close_stale(item):
        item._activity_actor = (0, SYSTEM_EMAIL)
        item.metadata = {**(item.metadata or {}), 'stale_closed_from_status': item.status}
        item.status = 'closed'
        item.save(update_fields=['status', 'metadata', 'updated_at'])
        Comment.objects.create(
            item=item, author_id=0, author_email=SYSTEM_EMAIL,
            content=(f'[backlog-custodian] Closed as stale: no activity for {stale_days}+{grace_days} '
                     f'days, including through the grace window after the stale warning. Reversible — '
                     f'reopen with a comment or transition if this is still relevant.'))

    def _flag_custodian(item, labels):
        item._activity_actor = (0, SYSTEM_EMAIL)
        item.labels = {**(labels or {}), 'stale': 'true', 'needs-custodian-review': 'true'}
        existing_mark = (item.metadata or {}).get('stale_marked_at')
        item.metadata = {**(item.metadata or {}), 'stale_marked_at': existing_mark or now.isoformat()}
        item.save(update_fields=['labels', 'metadata', 'updated_at'])
        Comment.objects.create(
            item=item, author_id=0, author_email=SYSTEM_EMAIL,
            content=('[backlog-custodian] Idle beyond the stale threshold but HIGH-VALUE '
                     '(priority high/urgent or story points at/above the cap) — NOT auto-closing. '
                     'Flagged for human custodian review.'))

    cap_hit = False
    candidates = Item.objects.filter(mode='task').exclude(status__in=_TERMINAL_ENUM_SLUGS)
    candidate_count = candidates.count()
    backlog_non_empty = candidate_count > 0
    for item in candidates.iterator():
        try:
            labels, anomaly = _normalize_labels(item.labels, item.item_key)
            if labels is None:
                item_errors += 1
                continue
            is_stale_labeled = 'stale' in labels
            last_act = _last_human_activity(item)

            if _is_exempt(item, labels):
                exempted += 1
                if is_stale_labeled:  # an exempt item should never carry the mark
                    if apply:
                        _clear_stale(item, labels)
                    cleared.append(item.item_key)
                continue

            if is_stale_labeled:
                marked_at_raw = (item.metadata or {}).get('stale_marked_at')
                marked_at = parse_datetime(marked_at_raw) if marked_at_raw else None
                # Reversibility: real activity after the mark clears `stale`.
                if marked_at is None or (last_act and last_act > marked_at):
                    if apply:
                        _clear_stale(item, labels)
                    cleared.append(item.item_key)
                    continue
                # Still idle — close after grace (unless high-value → flag).
                if (now - marked_at) >= timedelta(days=grace_days):
                    if _human_in_loop(item):
                        if apply:
                            _flag_custodian(item, labels)
                        flagged.append(item.item_key)
                        continue
                    if len(closed) >= max_closes:
                        cap_hit = True
                        break
                    if apply:
                        _close_stale(item)
                    closed.append(item.item_key)
                continue

            # Not yet marked — mark if idle beyond the stale threshold.
            if last_act <= stale_cutoff:
                if _human_in_loop(item):
                    if apply:
                        _flag_custodian(item, labels)
                    flagged.append(item.item_key)
                    continue
                if apply:
                    _mark_stale(item, labels)
                marked.append(item.item_key)
        except Exception as e:  # never let one bad item abort the whole sweep
            item_errors += 1
            logger.warning(f"sweep_stale_tasks: item {getattr(item, 'item_key', '?')} failed: {e}")

    if cap_hit:
        logger.warning(f"sweep_stale_tasks: hit per-run close cap ({max_closes}); "
                       f"remaining stale-closes deferred to the next run (anti-runaway).")
    result = {
        'applied': apply, 'marked': marked, 'closed': closed, 'flagged': flagged,
        'cleared': cleared, 'exempted': exempted, 'cap_hit': cap_hit,
        'item_errors': item_errors, 'backlog_non_empty': backlog_non_empty,
        'candidate_count': candidate_count,
        'stale_days': stale_days, 'grace_days': grace_days,
    }
    processed = len(marked) + len(closed) + len(flagged) + len(cleared) + exempted + item_errors
    anomaly_reasons = []
    if cap_hit:
        anomaly_reasons.append(f'close cap hit ({max_closes})')
    if item_errors:
        anomaly_reasons.append(f'item_errors={item_errors}')
    zero_processed_min = int(os.environ.get('PULSE_STALE_SWEEP_ZERO_PROCESSED_MIN_BACKLOG', str(getattr(settings, 'PULSE_STALE_SWEEP_ZERO_PROCESSED_MIN_BACKLOG', 100))))
    if backlog_non_empty and processed == 0 and candidate_count >= zero_processed_min:
        anomaly_reasons.append(f'zero processed while candidate_count={candidate_count} >= {zero_processed_min}')
    state = 'anomaly' if anomaly_reasons else 'success'
    payload = _record_stale_sweep_health(result, state=state, error='; '.join(anomaly_reasons))
    if anomaly_reasons:
        _notify_stale_sweep_owner(payload)
    logger.warning(
        f"sweep_stale_tasks: {'APPLIED' if apply else 'DRYRUN'} "
        f"marked={len(marked)} closed={len(closed)} flagged={len(flagged)} "
        f"cleared={len(cleared)} exempted={exempted} item_errors={item_errors} "
        f"backlog_non_empty={backlog_non_empty} candidate_count={candidate_count} cap_hit={cap_hit}")
    return result


def _org_health_active_blocked_item_ids():
    """Item IDs whose backlog-pressure signal is suppressed by a live blocker."""
    from .models import ItemLink
    return set(
        ItemLink.objects
        .filter(link_type='blocks', source_item__resolution__isnull=True)
        .exclude(source_item__status__in=_TERMINAL_ENUM_SLUGS)
        .values_list('target_item_id', flat=True)
    ) | set(
        ItemLink.objects
        .filter(link_type__in=['is_blocked_by', 'blocked_by'], target_item__resolution__isnull=True)
        .exclude(target_item__status__in=_TERMINAL_ENUM_SLUGS)
        .values_list('source_item_id', flat=True)
    )


def _org_health_backlog_item_ready_for_pressure(item, now, active_blocked_ids=None):
    """Pure readiness predicate mirrored by the backlog-pressure queryset."""
    if getattr(item, 'mode', None) != 'task' or getattr(item, 'status', None) != 'backlog':
        return False
    if getattr(item, 'resolution', None) is not None or getattr(item, 'is_archived', False):
        return False
    due_date = getattr(item, 'due_date', None)
    if due_date is not None and due_date > now:
        return False
    if getattr(item, 'id', None) in (active_blocked_ids or set()):
        return False
    return True


def _org_health_backlog_pressure_queryset(now):
    """Backlog rows that are actually ready enough to count as PM pressure.

    Excludes future-due recurring/scheduled rows and work with active linked
    blockers so the signal matches Pulse's actionable/readiness semantics.
    """
    from django.db.models import Q
    from .models import Item
    from .services.workflow_service import WorkflowService as W
    return (
        Item.objects
        .filter(mode='task', status='backlog', resolution__isnull=True, is_archived=False)
        .filter(Q(due_date__isnull=True) | Q(due_date__lte=now))
        .exclude(id__in=_org_health_active_blocked_item_ids())
        .exclude(W._umbrella_item_q())
    )


def _org_health_capacity_starve_grace_hours():
    """Minimum queued-item age before capacity-starve may file.

    PLAT-1246: sweep_org_health can run before the 10-minute rebalance
    sweep has had a chance to claim/reroute a newly-created task.  Queue
    starvation is an org-health signal, not a first-minute routing event; gate
    it behind at least one full rebalance cycle (default floor: 30 minutes) so
    fresh tasks do not page Program Management on every CI/deploy burst.
    """
    import os

    explicit = os.environ.get('PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES')
    if explicit is not None:
        try:
            return max(0.0, float(explicit)) / 60.0
        except (TypeError, ValueError):
            logger.warning(
                "Invalid PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES=%r; falling back to default",
                explicit,
            )

    try:
        rebalance_min = max(
            0.0,
            float(os.environ.get('PULSE_REBALANCE_SWEEP_INTERVAL_MINUTES', '10')),
        )
    except (TypeError, ValueError):
        rebalance_min = 10.0
    try:
        floor_min = max(
            0.0,
            float(os.environ.get('PULSE_QUEUE_STARVATION_MIN_AGE_FLOOR_MINUTES', '30')),
        )
    except (TypeError, ValueError):
        floor_min = 30.0
    return max(rebalance_min, floor_min) / 60.0


# PLS-261: statuses that mean "a role/team has already PICKED UP the item and is
# working it in place" — not "awaiting routing/pickup". These items are commonly
# assignee-NULL (they are dispositioned via role-gated workflow transitions, e.g.
# a security-finding routed to the security team, not via capacity-pull
# individual assignment), so `_queued_tasks_for_team` (assignee_id IS NULL) still
# returns them. Counting them as "unrouted capacity queue" produced ≥5 false
# `capacity-starve` fires on assigned-and-being-worked security findings
# (PLAT-2913/2901/2733/1314/1266). Only genuinely awaiting-routing items
# (needs_triage/open/todo) count toward starvation / whole-team-outage.
_ORG_HEALTH_ROLE_OWNED_ACTIVE_STATUSES = frozenset({'found', 'accepted', 'in_progress'})
# SRCH-133 (PLAT-8802 phantom probe): items in `resolved_elsewhere` are NOT
# actionable demand. The slug is deliberately pinned NON-terminal
# (Pulse `BLOCK_RELEASING_NONTERMINAL_SLUGS`, PLS-719/PLS-1261) so the
# blocker-release machinery can discover it — but no seat can work it: the
# resolver's own active-queue ready lanes exclude it (a `resolved_elsewhere`
# item never satisfies `pulse_get_my_tasks` ready lanes). Counting it in the
# sweep's routable queue made the stalest such item a PHANTOM demand leg and a
# permanent availability probe — PLAT-8802 kept satisfying the `[Team Outage]`
# conjunction 8 days after its blocker resolved elsewhere (cumulative incident
# age 1574h across PLAT-9764→9765→9733→9772). PLS-1382 later extended the same
# exclusion into `_assignment_excluded_statuses` (capacity-pull / WIP load /
# critical-override). This sweep-local filter remains as a belt-and-suspenders
# defense for org-health; blocker-release refuse-leg guards stay untouched.
_ORG_HEALTH_UNACTIONABLE_QUEUE_STATUSES = frozenset({'resolved_elsewhere'})
_ORG_HEALTH_PROTECTED_QUEUE_STATUSES = frozenset({
    # Active linked blockers already carry an owner and auto-release contract.
    'blocked',
    # These stage queues have their own event-driven owner/escalation path
    # (notably PLS-461 review-seat starvation) and must not page the origin pod
    # a second time as generic capacity starvation.
    'in_review', 'in_qa', 'awaiting_merge', 'awaiting_deploy',
    'awaiting_verification', 'verification', 'applied',
})


def _org_health_merge_protected(it) -> bool:
    """True if the item is deliberately held at the merge separation-of-duties
    gate (`metadata['merge_sod_hold']`). PLS-460 / aoi 2026-07-13: such an item is
    PROTECTED/held (parked because author==merger), not capacity-starved, so it
    must never surface as a starvation finding."""
    meta = getattr(it, 'metadata', None)
    return bool(isinstance(meta, dict) and meta.get('merge_sod_hold'))


def _org_health_routable_queue(queued):
    """Filter a team's capacity queue down to items genuinely AWAITING routing —
    drop items already picked up / being worked in place (PLS-261), protected
    stage/linked-blocker holds, future-due generations, and items deliberately
    held at the merge SoD gate (PLS-460 — protected, not starved). Keeps the
    starvation and whole-team-outage signals honest without touching the shared
    `_queued_tasks_for_team` (also used by capacity-pull / rebalancer)."""
    now = timezone.now()
    return [
        it for it in queued
        if (getattr(it, 'status', '') or '').lower() not in _ORG_HEALTH_ROLE_OWNED_ACTIVE_STATUSES
        and (getattr(it, 'status', '') or '').lower() not in _ORG_HEALTH_UNACTIONABLE_QUEUE_STATUSES
        and (getattr(it, 'status', '') or '').lower() not in _ORG_HEALTH_PROTECTED_QUEUE_STATUSES
        and not (
            getattr(it, 'due_date', None) is not None
            and getattr(it, 'due_date') > now
        )
        and not _org_health_merge_protected(it)
    ]


def _org_health_exact_routing_lanes(W):
    """Return queued exact-org lanes with the resolver's candidate snapshot.

    Team names are not globally unique.  Discover lanes from durable queued
    items, scope the queue join by ``project.org_id``, and ask the same
    item-aware source helper used by the resolver for membership/version data.
    """
    from .models import Item
    from .services.routing_authority import routing_candidate_snapshot_for_item
    from .services.org_health_snapshot_probe import (
        record_snapshot_failure, record_snapshot_success,
    )

    excluded = set(W._assignment_excluded_statuses())
    raw_lanes = (
        Item.objects.filter(
            mode='task', assignee_id__isnull=True, resolution__isnull=True,
            is_archived=False,
        )
        .filter(Q(assignee_email__isnull=True) | Q(assignee_email=''))
        .exclude(status__in=excluded)
        .exclude(assignment_group__isnull=True).exclude(assignment_group='')
        .values_list('project__org_id', 'assignment_group').distinct()
    )
    lanes = []
    probe_lanes = []
    attempted = 0
    completed = 0
    current_lane = None
    seen = set()
    try:
        for org_id, raw_team in raw_lanes:
            if org_id is None:
                continue
            team = W._canonical_team_name(raw_team)
            identity = (int(org_id), team)
            if not team or identity in seen:
                continue
            seen.add(identity)
            current_lane = {
                'organization_id': int(org_id), 'team_key': team,
                'source': 'unknown', 'version': 0,
            }
            attempted += 1
            queued = _org_health_routable_queue(list(
                W._queued_tasks_for_team(team, organization_id=org_id)
            ))
            if queued:
                sample = queued[0]
                snapshot = routing_candidate_snapshot_for_item(team, sample)
                snapshot['queued'] = queued
                snapshot['members'] = [
                    str(row.get('email') or '').strip().lower()
                    for row in snapshot.get('candidates', [])
                    if isinstance(row, dict) and str(row.get('email') or '').strip()
                ]
                current_lane.update({
                    'source': snapshot.get('source') or 'unknown',
                    'version': snapshot.get('source_version') or 'unknown',
                })
                lanes.append(snapshot)
            current_lane['success'] = True
            probe_lanes.append(current_lane)
            completed += 1
            current_lane = None
    except Exception as exc:
        probe_state = record_snapshot_failure(
            exc, attempted=attempted, completed=completed,
            failed_lane=current_lane, lanes=probe_lanes,
        )
        try:
            exc._org_health_probe_state = probe_state
        except Exception:
            pass
        raise
    lanes.sort(key=lambda row: (
        int(row.get('organization_id') or 0), row.get('team_key') or '',
    ))
    record_snapshot_success(
        attempted=attempted, completed=completed, lanes=probe_lanes,
    )
    return lanes


def _org_health_admin_team_snapshot(organization_id, organization_slug, team_name):
    """Read exact Admin membership for labeled drift evidence only.

    Failure returns an explicit unavailable diagnostic.  This result is never
    fed into candidate discovery, liveness, assignment, or any membership write.
    """
    import os
    from datetime import timedelta
    try:
        import jwt
        import requests
        from .services.workflow_service import WorkflowService as W

        now = timezone.now()
        service_token = jwt.encode({
            'user_id': 999,
            'email': 'system@shizuha.com',
            'username': 'system',
            'organization_id': int(organization_id),
            'is_staff': True,
            'is_superuser': True,
            'exp': now + timedelta(minutes=10),
            'iat': now,
            'token_type': 'access',
            'jti': f'org-health-team-drift-{organization_id}-{int(now.timestamp())}',
        }, configured_service_signing_key(), algorithm='HS256')
        rows = W._fetch_admin_team_pages(
            requests,
            os.environ.get(
                'SHIZUHA_ADMIN_API_URL', 'http://shizuha-admin:8003/api',
            ).rstrip('/'),
            organization_slug,
            {'Authorization': f'Bearer {service_token}', 'Accept': 'application/json'},
        )
        key = W._canonical_team_name(team_name)
        row = next((raw for raw in rows if isinstance(raw, dict) and
                    W._canonical_team_name(
                        raw.get('slug') or raw.get('name') or raw.get('team_name')
                    ) == key), None)
        if row is None:
            return {'status': 'team_missing', 'team_id': None, 'members': []}
        team_id = row.get('id') or row.get('team_id')
        if not team_id:
            return {
                'status': 'partial', 'reason': 'team_id_missing',
                'team_id': None, 'member_count': row.get('member_count'),
                'members': [], 'observed_members': [],
            }

        # The team-list serializer intentionally exposes only lead_email and
        # member_count.  Identities live on the org-scoped members endpoint as
        # employee_email; never infer a membership set from the list row.
        from urllib.parse import quote
        members_url = (
            f"{os.environ.get('SHIZUHA_ADMIN_API_URL', 'http://shizuha-admin:8003/api').rstrip('/')}"
            f'/organizations/{quote(str(organization_slug), safe="")}'
            f'/teams/{team_id}/members/'
        )
        response = requests.get(
            members_url,
            headers={'Authorization': f'Bearer {service_token}', 'Accept': 'application/json'},
            timeout=5,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f'Admin team members failed with HTTP {response.status_code}'
            )
        data = response.json()
        if (not isinstance(data, dict)
                or not isinstance(data.get('members'), list)
                or isinstance(data.get('count'), bool)):
            raise ValueError('Admin team members response has an invalid shape')
        member_rows = data['members']

        members = sorted({
            str(member.get('employee_email') or '').strip().lower()
            for member in member_rows if isinstance(member, dict)
            and str(member.get('employee_email') or '').strip()
        })
        expected_count = row.get('member_count')
        endpoint_count = data.get('count')
        try:
            endpoint_count_matches = int(endpoint_count) == len(members)
        except (TypeError, ValueError):
            endpoint_count_matches = False
        try:
            list_count_matches = int(expected_count) == len(members)
        except (TypeError, ValueError):
            list_count_matches = False
        if not endpoint_count_matches or not list_count_matches:
            return {
                'status': 'partial', 'reason': 'member_count_mismatch',
                'team_id': team_id, 'member_count': expected_count,
                'endpoint_member_count': endpoint_count,
                'observed_member_count': len(members),
                'members': [], 'observed_members': members,
            }
        return {
            'status': 'ok',
            'team_id': team_id,
            'member_count': expected_count,
            'endpoint_member_count': endpoint_count,
            'observed_member_count': len(members),
            'members': members,
        }
    except Exception as exc:
        logger.warning(
            'sweep_org_health: Admin drift read failed for %s::%s: %s',
            organization_slug, team_name, exc,
        )
        return {'status': 'unavailable', 'team_id': None, 'members': []}


def _org_health_admin_routing_drift(admin_snapshot, routing_members):
    """Return drift only for a complete Admin membership snapshot."""
    if admin_snapshot.get('status') != 'ok':
        return None
    admin_members = set(admin_snapshot.get('members') or [])
    routing_members = set(routing_members or [])
    return {
        'admin_only': sorted(admin_members - routing_members),
        'routing_only': sorted(routing_members - admin_members),
    }


def _org_health_candidate_availability(W, lane, item):
    """Return resolver-parity availability reason classes for one item lane.

    Candidate discovery is only the first half of routability.  Apply the same
    seat/capability/liveness/status checks as the resolver and, for an armed
    binding, the same independent receive-item entitlement/freshness decision.
    Capacity is intentionally excluded: a full but otherwise healthy team is a
    queue-pressure condition, not a whole-team outage.
    """
    reasons_by_email = {}
    binding_source = lane.get('source') == 'routing_binding'
    for candidate in lane.get('candidates') or []:
        if not isinstance(candidate, dict):
            continue
        email = str(candidate.get('email') or '').strip().lower()
        if not email:
            continue
        user_id = candidate.get('user_id') or candidate.get('subject_id')
        try:
            verdict = W._seat_routability_verdict(
                item,
                lane.get('team_key'),
                email,
                intent='new_assignment',
                enforce_cap=False,
                user_id=user_id,
            )
            reasons = list(verdict.reasons or ())
            if binding_source:
                from .services.routing_authority import subject_authorized_for_item
                decision = subject_authorized_for_item(
                    user_id,
                    item,
                    team_name=lane.get('team_key'),
                ) if user_id else None
                if not decision or not decision.allowed:
                    reasons.append(
                        decision.reason if decision is not None
                        else 'identity_unresolved'
                    )
            reasons_by_email[email] = list(dict.fromkeys(reasons)) or ['available']
        except Exception:
            logger.warning(
                'sweep_org_health: candidate verdict failed for %s on %s::%s',
                email,
                lane.get('organization_id'),
                lane.get('team_key'),
                exc_info=True,
            )
            reasons_by_email[email] = ['availability_unknown']
    return reasons_by_email


def _org_health_recent_active_team_activity(members, now):
    """Return recent production activity for explicitly-active roster members.

    PLAT-3981: whole-team-outage is an availability-derived *dead team*
    detector. During runtime pod recreation / exporter churn, every member can
    briefly read unavailable even while the team is actively producing Pulse
    comments/transitions. Before filing an urgent zero-member outage, cross-check
    durable AgentAvailability(active=True) plus recent Pulse activity; a team
    that is both operator-marked active and producing work is not offline.
    """
    import os
    from datetime import timedelta

    try:
        from .models import AgentAvailability, Comment, ItemActivity
        from .services.workflow_service import WorkflowService as W
    except Exception:
        return None

    try:
        minutes = int(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_ACTIVITY_MINUTES', '30'))
        min_events = int(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_ACTIVITY_MIN_EVENTS', '1'))
    except Exception:
        minutes, min_events = 30, 1
    if minutes <= 0 or min_events <= 0:
        return None

    aliases_by_member = {}
    all_aliases = set()
    for member in members or []:
        aliases = {a.strip().lower() for a in W._agent_email_aliases(member) if a}
        if aliases:
            aliases_by_member[(member or '').strip().lower()] = aliases
            all_aliases.update(aliases)
    if not all_aliases:
        return None

    try:
        active_rows = set(
            AgentAvailability.objects
            .filter(active=True, email__in=all_aliases)
            .values_list('email', flat=True)
        )
    except Exception:
        return None
    active_row_aliases = {str(e or '').strip().lower() for e in active_rows if e}
    if not active_row_aliases:
        return None

    active_members = []
    activity_aliases = set()
    for member, aliases in aliases_by_member.items():
        if aliases & active_row_aliases:
            active_members.append(member)
            # Count activity under either canonical email or @agents alias for the
            # active identity; Pulse comments/activities are not guaranteed to use
            # the same alias the AgentAvailability row uses.
            activity_aliases.update(aliases)
    active_members = sorted(active_members)
    if not active_members or not activity_aliases:
        return None

    since = now - timedelta(minutes=minutes)
    try:
        comment_count = Comment.objects.filter(
            author_email__in=activity_aliases, created_at__gte=since,
        ).count()
        activity_count = ItemActivity.objects.filter(
            user_email__in=activity_aliases, created_at__gte=since,
        ).count()
    except Exception:
        return None

    total = int(comment_count or 0) + int(activity_count or 0)
    if total < min_events:
        return None
    return {
        'events': total,
        'comments': int(comment_count or 0),
        'activities': int(activity_count or 0),
        'window_minutes': minutes,
        'active_members': active_members,
    }


def _org_health_member_at_capacity(email, uid, *, team=None, organization_id=None):
    """PLS-547 / PLS-667: evaluate headroom with the router's capacity contract.

    Org health must use the same email-primary active/in-flight counts and
    two-tier capacity predicate as assignment/rebalance.  An assignee-id-only
    count misses legacy/email-owned work and can false-page available headroom
    while the router correctly refuses the pull.

    PLS-667: when the lane has a wip_enforced roster, also honor ready/active
    slot caps (the same dimensions reclaim + capacity-pull use).  Members who
    are full on assigned-ready must not appear as "available under-cap" just
    because active/in-flight still has headroom — that false desync pages
    capacity-starve while the router correctly holds the queue.
    """
    from types import SimpleNamespace

    from .services.workflow_service import WorkflowService as W

    normalized_email = (email or '').strip().lower()
    active = W._agent_active_load(int(uid), normalized_email)
    inflight = W._agent_workable_load(int(uid), normalized_email)
    at_cap = W._at_capacity(active, inflight)
    ready_load = None
    ready_cap = None
    roster_active_load = None
    roster_active_cap = None

    team_name = W._canonical_team_name(team) if team else ''
    if team_name and organization_id is not None:
        probe = SimpleNamespace(
            pk=None,
            mode='task',
            status='open',
            assignment_group=team_name,
            assignee_id=int(uid),
            project=SimpleNamespace(org_id=int(organization_id)),
        )
        config = W._wip_roster_config(team_name, probe)
        if config is not None:
            ready_cap = int(config.ready_wip_cap or 0)
            roster_active_cap = int(config.active_wip_cap or 0)
            if ready_cap > 0:
                ready_load = W._agent_slot_load(
                    probe, normalized_email, 'ready', user_id=int(uid),
                )
                if ready_load >= ready_cap:
                    at_cap = True
            if roster_active_cap > 0:
                roster_active_load = W._agent_slot_load(
                    probe, normalized_email, 'active', user_id=int(uid),
                )
                if roster_active_load >= roster_active_cap:
                    at_cap = True

    return (
        at_cap,
        active,
        inflight,
        {
            'ready_load': ready_load,
            'ready_cap': ready_cap,
            'roster_active_load': roster_active_load,
            'roster_active_cap': roster_active_cap,
        },
    )


def _signal_org(signal_key: str):
    """Extract the subject organization id from an org-health signal key.

    Signals are keyed ``<class>:<org>:<subject>`` (e.g. ``capacity-starve:1:
    engineering``). Returns an int org id when present, else None. A missing or
    non-numeric org component means there is no scoped subject org to enforce.
    """
    parts = str(signal_key or '').split(':', 2)
    if len(parts) >= 2 and str(parts[1] or '').strip().isdigit():
        return int(parts[1])
    return None


def _resolve_in_org_owner(admin_service, org_id: int):
    """Return ``(user_id, email)`` of an owner/admin inside the subject org.

    PLS-849 fallback: when the least-loaded PM lead lacks read scope for the
    subject org, prefer an in-org owner/admin who can verify and remediate the
    signal. Returns ``(None, None)`` when the org has no resolvable owner/admin
    — the caller then fails loud rather than leaking the packet to a global
    free seat.
    """
    try:
        members = admin_service.get_org_members(int(org_id)) or []
        for member in members:
            if str(member.get('role') or '').strip().lower() not in {'owner', 'admin'}:
                continue
            uid = member.get('user_id')
            if uid is None:
                continue
            return int(uid), str(member.get('user_email') or '').strip()
    except Exception:
        logger.debug("sweep_org_health: in-org owner resolution failed", exc_info=True)
    return None, None


def _dm_program_management_lead(
    workflow_service,
    text: str,
    *,
    organization_id=None,
    subject_key: str = '',
) -> bool:
    """Deliver one org-health Connect DM without creating a Pulse carrier.

    PLS-849: the recipient must hold canonical read scope for the subject
    organization. A scope-blind recipient cannot verify or remediate the signal
    and the same finding re-pings indefinitely. The resolved PM lead is
    intersected with the subject org's membership at delivery time; if the
    least-loaded lead lacks scope, fall back to an in-org owner/admin. With no
    authorized recipient the packet is NOT leaked to a global free seat — the
    caller fails loud (returns False) so the dedupe claim is released for a
    bounded retry. ``organization_id`` is always carried on the packet for
    provenance/audit.
    """
    try:
        uid, email = workflow_service._resolve_team_assignee(
            'program-management', exclude_email=None, enforce_cap=False,
        )
        if not uid:
            return False
        from .services.admin_service import admin_service
        org_id = None
        if organization_id is not None:
            try:
                org_id = int(organization_id)
            except (TypeError, ValueError):
                org_id = None
        if org_id and org_id > 0:
            if not admin_service.is_org_member(uid, org_id, email):
                uid, email = _resolve_in_org_owner(admin_service, org_id)
                if not uid:
                    logger.warning(
                        "sweep_org_health: no authorized recipient for org %s "
                        "(subject %s); fail-loud, no cross-org DM",
                        org_id, subject_key or '?',
                    )
                    return False
        from django.conf import settings as _s
        from .services.connect_origin import system_message_headers
        base = getattr(
            _s, 'SHIZUHA_CONNECT_API_URL',
            'http://shizuha-connect:8019/api',
        ).rstrip('/')
        body = {
            'recipient_user_id': uid,
            'content': text,
            'sender_name': 'Pulse org-health',
            'sender_service': 'shizuha-pulse',
        }
        if org_id and org_id > 0:
            body['organization_id'] = org_id
        if subject_key:
            body['subject_key'] = str(subject_key)
        import requests as _rq
        response = _rq.post(
            f'{base}/internal/system-message/', json=body,
            headers=system_message_headers(body), timeout=5,
        )
        return 200 <= response.status_code < 300
    except Exception:
        logger.debug("sweep_org_health: DM delivery failed", exc_info=True)
        return False


def emit_org_health_signal(
    signal_key,
    payload,
    *,
    workflow_service,
    now,
    redis_client,
    reping_seconds,
    organization_id=None,
    subject_key='',
):
    """Emit a PM-side org-health signal through the single DM-only sink.

    ``payload`` preserves the detector branch's title/body/priority contract.
    This boundary intentionally has no Item/Comment dependency: it cannot
    create, update, reopen, or assign a Pulse task. Redis provides onset
    dedupe and the re-ping cadence. A failed Connect delivery releases only
    this attempt's claim so the next sweep can retry rather than waiting for
    the full re-ping window (PLS-460).

    PLS-849: ``organization_id`` (and ``subject_key``) are threaded through so
    the DM sink can enforce recipient read scope for the exact subject org and
    carry org provenance on the packet. When omitted, they are inferred from
    the signal key (``<class>:<org>:<subject>``).
    """
    if organization_id is None:
        organization_id = _signal_org(signal_key)
    if not subject_key:
        parts = str(signal_key or '').split(':', 2)
        if len(parts) >= 3:
            subject_key = parts[2]
    reping_seconds = max(1, int(reping_seconds))
    token = now.isoformat()
    redis_key = f"pulse:org_health_dm:{signal_key}"
    claimed = True
    if redis_client is not None:
        try:
            claimed = bool(redis_client.set(
                redis_key, token, nx=True, ex=reping_seconds,
            ))
        except Exception:
            # Redis loss must not turn into silent signal loss. Delivery is
            # still attempted; at worst this produces a rare duplicate.
            logger.warning(
                "sweep_org_health: dedupe unavailable for %s; delivering anyway",
                signal_key, exc_info=True,
            )
            claimed = True
        if not claimed:
            return False

    title = str(payload.get('title') or '')
    body = str(payload.get('body') or '')
    delivered = _dm_program_management_lead(
        workflow_service,
        f"🩺 [org-health] {title}\n\n{body}\n\n(signal: {signal_key}; "
        f"DM-only per operator 2026-07-12 — no task filed; re-pings "
        f"every {reping_seconds // 3600}h while the condition persists)",
        organization_id=organization_id,
        subject_key=subject_key,
    )
    if delivered:
        return True

    logger.warning(
        "sweep_org_health: Connect delivery failed for %s; releasing dedupe claim",
        signal_key,
    )
    if redis_client is not None and claimed:
        try:
            current = redis_client.get(redis_key)
            if isinstance(current, bytes):
                current = current.decode('utf-8', errors='replace')
            if current == token:
                redis_client.delete(redis_key)
        except Exception:
            logger.warning(
                "sweep_org_health: failed to release dedupe claim for %s",
                signal_key, exc_info=True,
            )
    return False


@shared_task(bind=True, max_retries=0)
def evaluate_pretriage_age_deadline(self, item_id):
    """PLS-621: delayed pre-triage age deadline (event-driven, not a full scan)."""
    try:
        from .services.routing_source_health import evaluate_pretriage_deadline_for_item
        return evaluate_pretriage_deadline_for_item(str(item_id))
    except Exception:
        logger.exception('PLS-621: evaluate_pretriage_age_deadline failed for %s', item_id)
        return False


@shared_task(bind=True, max_retries=1, queue='freshness')
def sweep_org_health(self):
    """Program-Management watchdog (operator directive 2026-06-13): the always-on
    OUTER control loop. Detects org-level stalls/bottlenecks that no single agent
    can see (each agent only sees its own queue) and turns them into a TASK in the
    `program-management` team's queue — so the Chief of Staff fixes them on her
    heartbeat, with no operator reminder. Self-resolves a filed task when its signal
    clears. Idempotent (dedup via metadata.org_health_signal); never raises into beat.

    Signals:
      - capacity-starve:<pod> — a team has queued work AND either it's past the queue
        SLA or an available under-cap member exists who SHOULD be getting it but isn't
        (the routing/availability-desync failure mode of 2026-06-13).
      - throughput-flatline — zero agent task-activity in the window while plenty of
        actionable work exists (fleet-stall fingerprint).
      - zero-member-outage:<pod> — ALL members of a team are offline/unavailable while
        ≥1 task is queued; the rebalance sweep cannot reroute. Routes to admin-ops
        (operator) rather than PM because recovery requires agent re-enable/restart
        (PLAT-532, PLAT-367 prior whole-team-deadlock incident).
    """
    import os
    from datetime import timedelta
    try:
        from .models import Item, Comment, Project, ItemActivity
        from .services.workflow_service import WorkflowService as W
        from .user_service import user_service
    except Exception as e:
        logger.warning(f"sweep_org_health: import failed: {e}")
        return {'error': 'import'}

    now = timezone.now()
    PM = 'program-management'
    sla_h = float(os.environ.get('PULSE_QUEUE_AGE_SLA_HOURS', '96'))
    excluded = set(W._assignment_excluded_statuses())
    active_cap = W._wip_cap()
    inflight_cap = W._inflight_cap()
    findings = []  # (signal_key, priority, title, body)

    # PLAT-795: availability-derived findings (capacity-starve avail-desync,
    # whole-team outage) are only trustworthy when the agent-health exporter
    # signal is fresh. A stale/empty scrape makes every member read "down" and
    # spawned one false task per team (the 2026-06-24 19:45 mass false-fire).
    avail_signal_fresh = _org_health_exporter_fresh()
    # Operator-DISABLED agents (intentional budget-cap kill-switch) are distinct
    # from CRASHED/at-cap ones: a team whose ONLY seat is operator-disabled is in
    # an accepted state, not an outage to escalate (PLAT-795 / budget cap).
    # PLS-732: use the INTENT-ONLY set here — the full _disabled_agent_emails
    # union also contains PLS-253 service-denied seats (a fault, not an intent),
    # and treating a lockout as "intended budget-cap" would silently hide a real
    # single-seat outage.
    try:
        disabled_emails = W._operator_disabled_agent_emails()
    except Exception:
        disabled_emails = set()

    routing_snapshot = {
        'status': 'ok',
        'lane_count': 0,
        'failure_class': None,
    }
    exact_snapshot_complete = True
    try:
        exact_lanes = _org_health_exact_routing_lanes(W)
    except Exception as exc:
        from .services.org_health_snapshot_probe import record_snapshot_failure
        probe_state = getattr(exc, '_org_health_probe_state', None)
        if not isinstance(probe_state, dict):
            probe_state = record_snapshot_failure(exc)
        exact_snapshot_complete = False
        failure_class = type(exc).__name__
        routing_snapshot = {
            'status': 'failed',
            'lane_count': None,
            'failure_class': failure_class,
            'attempted_lane_count': probe_state['attempted'],
            'completed_lane_count': probe_state['completed'],
        }
        logger.error('sweep_org_health: exact routing-lane snapshot failed: %s', exc)
        exact_lanes = []
    else:
        routing_snapshot['lane_count'] = len(exact_lanes)
        from .services.org_health_snapshot_probe import snapshot_probe_state
        probe_state = snapshot_probe_state()
        routing_snapshot.update({
            'attempted_lane_count': probe_state['attempted'],
            'completed_lane_count': probe_state['completed'],
        })
    # Signal 3b performs its own exact-item reads after the primary lane
    # snapshot.  Its completeness is independent: a successful primary scan
    # must not authorize zero-member-team reconciliation when this second
    # family only produced a partial view.
    zero_member_snapshot_complete = exact_snapshot_complete

    def _all_operator_disabled(members):
        return bool(members) and all(
            (m or '').strip().lower() in disabled_emails for m in members)

    def _is_exact_routing_signal(signal):
        """Identify signals whose create/clear authority is the exact snapshot."""
        parts = str(signal or '').split(':', 2)
        return (
            len(parts) == 3
            and parts[0] in {
                'capacity-starve', 'zero-member-outage', 'zero-member-team',
            }
            and parts[1].isdigit()
            and bool(parts[2])
        )

    def _exact_signal_snapshot_complete(signal):
        """Return whether this signal's complete detector family was observed."""
        parts = str(signal or '').split(':', 2)
        if len(parts) == 3 and parts[0] == 'zero-member-team':
            return exact_snapshot_complete and zero_member_snapshot_complete
        return exact_snapshot_complete

    # ── qa.cadence containment: standing QA verification belongs to QA, never the CoS ──
    # A QA agent that hit a live failure but couldn't file a bug used to fire "Raise to
    # Admin Ops", dumping the whole cadence task on shizuha where the blocked-retriage
    # sweep auto-unblocked it into a stuck false in_progress (the "14 doing, doesn't know
    # about them" false-drain, 2026-06-23). Enforce the invariant: any non-terminal
    # qa.cadence item assigned outside QA is routed back to `qa` so the QA agent owns it
    # (closes a passed run / re-runs) and the CoS queue isn't polluted. A real failure
    # must become a SEPARATE bug to the owning team — never the cadence task itself.
    try:
        strays = (Item.objects.filter(mode='task', item_type='qa.cadence')
                  .exclude(status__in=excluded).exclude(assignment_group='qa'))
        contained = 0
        for it in strays:
            old_grp = it.assignment_group or '(none)'
            uid, email = W._resolve_team_assignee(
                'qa', project_key=W._item_project_key(it), item=it,
            )
            it.assignment_group = 'qa'
            if email:
                fields = ['assignment_group']
                W._apply_resolved_assignee(it, uid, email, fields)
                it.save(update_fields=list(dict.fromkeys(fields)))
            else:
                from .services.routing_authority import (
                    reroute_assignment_to_group_queue,
                )
                reroute_assignment_to_group_queue(
                    it, 'qa', reason='no_candidate', save=False,
                )
                it.save(update_fields=['assignment_group', 'assignee_id', 'assignee_email'])
            Comment.objects.create(
                item=it, author_id=0, author_email='system@pulse',
                content=(f"[qa-cadence containment] Standing QA verification belongs to the "
                         f"QA team — routed back to **qa** (was on {old_grp}). A live failure "
                         f"must be filed as a SEPARATE bug to the owning team, not escalated "
                         f"as this cadence task."))
            contained += 1
        if contained:
            logger.info(f"sweep_org_health: qa-cadence containment routed {contained} stray(s) back to qa")
    except Exception as e:
        logger.warning(f"sweep_org_health: qa-cadence containment failed: {e}")

    # ── signal 1: per-team capacity-queue starvation ──
    for lane in exact_lanes:
        team = lane['team_key']
        org_label = lane.get('organization_slug') or f"org-{lane['organization_id']}"
        pod = f'{org_label}::{team}'
        lane_key = f"{lane['organization_id']}:{team}"
        if team in (PM, 'governance'):
            continue
        # PLAT-9125: human-only lanes (zero agent members by design) hold
        # genuinely-human work — the backlog is awaits-human, not starving.
        # capacity-starve re-pings must not fire for them.
        if team in getattr(W, '_HUMAN_ONLY_TEAMS', frozenset()):
            continue
        queued = lane['queued']
        if not queued:
            continue
        members = lane['members']
        # PLAT-795: a pod whose every roster member is OPERATOR-DISABLED is an
        # intended budget-cap state (1 seat/team, deliberately off), not a routing
        # fault — don't nag the CoS about it. A CRASHED / at-cap member (down but
        # NOT in the operator-disabled set) still alerts via the paths below.
        if _all_operator_disabled(members):
            continue
        avail_under = []
        # Only compute the "available-but-not-getting-work" desync from a FRESH
        # signal; on a stale scrape avail_under would be bogus (PLAT-795). The
        # availability-independent age>SLA path below still fires.
        for e in (members if avail_signal_fresh else []):
            try:
                if not W._is_agent_available_for_assignment(e):
                    continue
                info = user_service.get_user_by_email(e)
                uid = info.get('id') if info else None
                if not (uid and str(uid).isdigit()):
                    continue
                # PLS-547 / PLS-667: same capacity contract as assignment/rebalance
                # — email-primary two-tier load PLUS roster ready/active slots.
                at_cap, active, inflight, slot_detail = _org_health_member_at_capacity(
                    e, uid,
                    team=team,
                    organization_id=lane.get('organization_id'),
                )
                if not at_cap:
                    active_bound = active_cap if active_cap > 0 else "disabled"
                    inflight_bound = inflight_cap if inflight_cap > 0 else "disabled"
                    parts = [
                        f"active {active}/{active_bound}",
                        f"in-flight {inflight}/{inflight_bound}",
                    ]
                    ready_cap_v = slot_detail.get('ready_cap')
                    ready_load_v = slot_detail.get('ready_load')
                    if ready_cap_v and ready_cap_v > 0 and ready_load_v is not None:
                        parts.append(f"ready {ready_load_v}/{ready_cap_v}")
                    roster_active_cap_v = slot_detail.get('roster_active_cap')
                    roster_active_load_v = slot_detail.get('roster_active_load')
                    if (
                        roster_active_cap_v and roster_active_cap_v > 0
                        and roster_active_load_v is not None
                    ):
                        parts.append(
                            f"roster-active {roster_active_load_v}/{roster_active_cap_v}"
                        )
                    avail_under.append(f"{e}({', '.join(parts)})")
            except Exception:
                continue
        oldest = min(queued, key=lambda i: i.created_at)
        age_h = (now - oldest.created_at).total_seconds() / 3600.0
        min_age_h = _org_health_capacity_starve_grace_hours()
        if age_h < min_age_h:
            logger.info(
                "sweep_org_health: suppressing capacity-starve:%s for fresh queue item %s "
                "(age %.2fh < grace %.2fh)",
                pod, oldest.item_key, age_h, min_age_h,
            )
            continue
        if age_h > sla_h or avail_under:
            if avail_under:
                why = (f"Available under-cap members who SHOULD be getting this work but are NOT: "
                       f"{', '.join(avail_under)}. This is a routing/availability desync — check the "
                       f"enable-state chain (daemon enabled-agents.json ↔ agents.json status ↔ "
                       f"PULSE_UNAVAILABLE_AGENT_EMAILS ↔ agent-health exporter ↔ Redis availability map), "
                       f"reconcile it, then run sweep_rebalance_pods. Routing source: "
                       f"{lane['source']} @ {lane['source_version']}.")
            else:
                why = ("No available under-cap member — the pod is at WIP cap or its (one) member is "
                       "down/disabled. If the member merely CRASHED, recover it (bridge/provider/restart). "
                       "Otherwise rebalance to a peer pod, or have the coordinator pick the work up directly. "
                       "Budget cap (1 agent/team, operator directive 2026-06-22): do NOT add agents, raise the "
                       "WIP cap for headcount, or re-enable an agent the operator explicitly disabled — those "
                       f"stops are intentional. Routing source: {lane['source']} @ "
                       f"{lane['source_version']}.")
            findings.append((
                f"capacity-starve:{lane_key}", 'high',
                f"[Program Mgmt] {pod} queue starving — {len(queued)} unrouted, oldest {oldest.item_key} {age_h:.0f}h",
                (f"The **{pod}** capacity queue holds {len(queued)} unrouted task(s); oldest {oldest.item_key} "
                 f"aged {age_h:.0f}h (SLA {sla_h:.0f}h). {why}"),
            ))

    # ── signal 1b: backlog pressure / aging ──
    # Backlog is not immediate WIP, but it is real assigned load. Treat a large or
    # aging backlog like ServiceNow-style unified backlog pressure: visible,
    # triaged, and owned by PM/CoS, while agents still pull only one top item at a
    # time through pulse_get_my_tasks. This avoids both extremes: invisible
    # parking lots and mass churn.
    try:
        from django.db.models import Count, Min

        backlog_count_threshold = int(os.environ.get('PULSE_BACKLOG_PRESSURE_COUNT', '25'))
        backlog_bottleneck_threshold = int(os.environ.get('PULSE_BACKLOG_BOTTLENECK_COUNT', '75'))
        backlog_age_sla_h = float(os.environ.get('PULSE_BACKLOG_AGE_SLA_HOURS', str(sla_h)))
        backlog_qs = _org_health_backlog_pressure_queryset(now)
        groups = (
            backlog_qs
            .values('assignment_group')
            .annotate(c=Count('id'), oldest=Min('created_at'))
            .order_by('-c')
        )
        for row in groups:
            group = W._canonical_team_name(row.get('assignment_group') or 'unassigned')
            if not group or group in {PM, 'governance'}:
                continue
            count = int(row.get('c') or 0)
            oldest_at = row.get('oldest') or now
            age_h = (now - oldest_at).total_seconds() / 3600.0
            if count < backlog_count_threshold and age_h < backlog_age_sla_h:
                continue

            sample_candidates = list(
                backlog_qs
                .filter(assignment_group=row.get('assignment_group'))
                .order_by('created_at')
                .values('item_key', 'title', 'priority', 'assignee_email', 'created_at')[:100]
            )
            sample = sorted(
                sample_candidates,
                key=lambda it: (-W._QUEUE_PRIORITY_WEIGHT.get((it.get('priority') or '').lower(), 2), it['created_at'])
            )[:8]
            assignees = list(
                backlog_qs
                .filter(assignment_group=row.get('assignment_group'))
                .values('assignee_email')
                .annotate(c=Count('id'))
                .order_by('-c')[:6]
            )
            samples_md = '\n'.join(
                f"- {it['item_key']} [{it.get('priority') or 'normal'}] "
                f"{(it.get('title') or '')[:140]} — {it.get('assignee_email') or 'unassigned'}"
                for it in sample
            ) or "- (no sample available)"
            assignees_md = ', '.join(
                f"{(a.get('assignee_email') or 'unassigned')}={a.get('c')}"
                for a in assignees
            ) or 'none'
            prio = 'urgent' if count >= backlog_bottleneck_threshold else 'high'
            findings.append((
                f"backlog-pressure:{group}", prio,
                f"[Program Mgmt] {group} backlog pressure — {count} parked task(s), oldest {age_h:.0f}h",
                (f"The **{group}** backlog has {count} parked task(s); oldest age is "
                 f"{age_h:.0f}h (count threshold {backlog_count_threshold}, age SLA "
                 f"{backlog_age_sla_h:.0f}h). This is assigned load, not a harmless "
                 f"parking lot.\n\n"
                 f"Top assignee concentrations: {assignees_md}.\n\n"
                 f"Highest queue samples:\n{samples_md}\n\n"
                 f"Expected handling: preserve WIP limits; agents should pull only the "
                 f"highest-priority movable backlog item when their active queue has "
                 f"capacity. PM/CoS should revalidate stale backlog, close duplicates, "
                 f"split umbrella work, and route blocked/decision-only items to an "
                 f"explicit blocker owner. This signal self-resolves when backlog "
                 f"pressure drops below threshold."),
            ))
    except Exception as e:
        logger.warning(f"sweep_org_health: backlog pressure check failed: {e}")

    # ── signal 2: org throughput flatline ──
    win = _org_health_throughput_window_minutes(now)
    try:
        acts = ItemActivity.objects.filter(created_at__gte=now - timedelta(minutes=win))
        agent_acts = _org_health_throughput_activity(acts)
        actionable = Item.objects.filter(mode='task', status__in=['open', 'todo', 'in_progress', 'in_review'],
                                         resolution__isnull=True).count()
        in_progress = Item.objects.filter(mode='task', status__in=W._WORKING_STATUSES, resolution__isnull=True).count()
        min_actionable = int(os.environ.get('PULSE_THROUGHPUT_MIN_ACTIONABLE', '10'))
        min_fresh_agents = int(os.environ.get('PULSE_THROUGHPUT_FRESH_AGENTS_MIN', '3'))
        min_in_progress = int(os.environ.get('PULSE_THROUGHPUT_IN_PROGRESS_MIN', '10'))
        fresh_minutes = int(os.environ.get('PULSE_THROUGHPUT_SESSION_FRESH_MIN', '30'))
        if len(agent_acts) == 0 and actionable >= min_actionable:
            freshness = _org_health_fresh_agent_sessions(fresh_minutes * 60)
            if freshness['fresh_count'] >= min_fresh_agents and in_progress >= min_in_progress:
                logger.info(
                    "sweep_org_health: suppressing throughput-flatline; "
                    f"0 ItemActivity in {win}m but {freshness['fresh_count']} fresh agent sessions "
                    f"and {in_progress} working-status tasks indicate heads-down work"
                )
            else:
                findings.append((
                    'throughput-flatline', 'urgent',
                    f"[Program Mgmt] Org throughput flatlined — 0 agent actions in {win}m, {actionable} actionable tasks",
                    (f"No agent task-activity in the last {win} minutes while {actionable} actionable tasks exist — "
                     f"fleet-stall fingerprint. Fresh-session cross-check saw "
                     f"{freshness['fresh_count']} agent(s) active within {fresh_minutes}m and "
                     f"{in_progress} working-status task(s) (suppression thresholds: "
                     f"{min_fresh_agents} fresh agents + {min_in_progress} in_progress). Check token "
                     f"cooldowns (~/.shizuha/credentials.json), agent heartbeats/wedge, and whether queues "
                     f"are being fed (assignment/availability chain). Agents alive but idle ⇒ the feeder "
                     f"loop (routing) failed, not the agents."),
                ))
    except Exception as e:
        logger.warning(f"sweep_org_health: throughput check failed: {e}")

    # ── signal 2b: recurring stale-cycle report (PLS-214) ──
    try:
        from .services.recurring import parse_interval_seconds
        stale_recurring = []
        recurring_qs = (Item.objects.filter(mode='task', is_recurring=True)
                        .exclude(status__in=excluded))
        for it in recurring_qs:
            cycle = ((it.metadata or {}).get('recurring_cycle') or {})
            last_done = cycle.get('last_terminal_completion_at')
            if not last_done:
                continue
            try:
                from datetime import datetime as _datetime
                last_done_dt = _datetime.fromisoformat(str(last_done).replace('Z', '+00:00'))
                if timezone.is_naive(last_done_dt):
                    last_done_dt = timezone.make_aware(last_done_dt, timezone.get_current_timezone())
            except Exception:
                continue
            interval = parse_interval_seconds(it.frequency, it.recurrence_pattern)
            if not interval:
                # monthly/yearly/custom-unparseable: approximate from due_date if present, else skip report-only signal.
                continue
            age_s = (now - last_done_dt).total_seconds()
            if age_s > (2 * interval):
                stale_recurring.append((it, int(age_s // 3600), int((age_s // interval))))
        if stale_recurring:
            stale_recurring.sort(key=lambda row: row[1], reverse=True)
            preview = '; '.join(
                f"{it.item_key or '#'+str(it.id)} status={it.status} last_done={hours}h ({periods} periods)"
                for it, hours, periods in stale_recurring[:10]
            )
            findings.append((
                'recurring-stale-cycles', 'high',
                f"[Program Mgmt] {len(stale_recurring)} recurring item(s) stale >2 periods since terminal completion",
                (f"{len(stale_recurring)} recurring item(s) have `metadata.recurring_cycle.last_terminal_completion_at` "
                 f"older than 2 configured periods. These are recurring cadences that are not closing their cycles. "
                 f"Oldest/sample: {preview}. Check each item's stale-cycle comments/escalations and either complete, "
                 f"unblock, reroute, or intentionally Defer it. [PLS-214]"),
            ))
    except Exception as e:
        logger.warning(f"sweep_org_health: recurring stale-cycle report failed: {e}")

    # ── signal 2b: capability-class single-seat lanes (PLAT-8980, ADR-0086) ──
    # Alert-NOT-page: a critical lane scoring exactly 1 live-capable seat is a
    # resilience degradation (topology fact, demand-independent), not an outage.
    # count == 0 with queued demand belongs to the zero-live family below —
    # deliberately not duplicated here. Routing target: CoS (program-management)
    # per the aoi review note, with the provisioning ask bundled (PLAT-8263
    # one-window pointer — never a fragmented ask).
    try:
        from .services.capability_lane_health import (
            critical_capability_lanes,
            evaluate_single_seat_episode,
            live_capable_seat_count,
        )
        for cap_lane in critical_capability_lanes():
            try:
                cap_count, cap_detail = live_capable_seat_count(
                    W, cap_lane, organization_id=1, probe_item=None,
                )
            except Exception as exc:
                logger.warning(
                    'sweep_org_health: capability lane %s count failed: %r',
                    cap_lane, exc)
                continue
            episode = evaluate_single_seat_episode(cap_lane, cap_count)
            if episode == 'fire':
                rows = '; '.join(
                    f'{email}={("|".join(reasons) if reasons else "live-capable")}'
                    for email, reasons in sorted(cap_detail.items())
                ) or '(no roster)'
                findings.append((
                    f'single-capability-seat:1:{cap_lane}', 'high',
                    f'[Resilience] capability lane "{cap_lane}" is down to ONE live-capable seat',
                    (f'Capability-class lane "{cap_lane}" (ADR-0086: every critical lane '
                     f'maintains >=2 capable live seats) currently scores exactly '
                     f'{cap_count} live-capable seat(s). Roster evidence: [{rows}]. '
                     f'A single dark window on that seat strands the lane\'s carriers '
                     f'(PLAT-8960 class). Provisioning ask (bundled, one window): '
                     f'qualify and warm at least one additional live seat for this '
                     f'lane at the next operator window [PLAT-8263]. This alert is '
                     f'demand-independent — it fires regardless of queued work. '
                     f'Rule review condition: lane at >=2 capable live seats for 14 '
                     f'consecutive days -> review on the ADR-0086 thread.'),
                ))
    except Exception as e:
        logger.warning(f"sweep_org_health: capability-lane detector failed: {e}")

    # ── signal 3: whole-team outage (PLAT-532) ──
    # Distinct from capacity-starve: fires when ALL team members are unavailable
    # (down/disabled/wedged) while tasks queue. The rebalance sweep cannot reroute
    # because no live peer exists in the team. Routes to admin-ops (operator), not PM,
    # because recovery requires re-enabling or restarting agents — not CoS nudging.
    admin_findings = []  # (signal_key, priority, title, body)
    # PLS-854: signals detected THIS cycle (even below the fire debounce) reset
    # the clear-side absent counter, so a present-but-debouncing signal can never
    # count toward a clear.
    present_admin_sigs = set()
    # PLAT-795: collect genuine outage candidates first, then disposition them via
    # the global-sanity + debounce guards below — instead of filing one task per
    # team inline (which mass-false-fired ~13 tasks on a single bad scrape at 19:45).
    # (stable org/team key, display label, members, queue, oldest, age, lane, reasons)
    outage_candidates = []
    outage_pods_evaluated = 0
    if not avail_signal_fresh:
        logger.warning(
            "sweep_org_health: exporter signal stale/empty — SKIPPING whole-team-outage "
            "detection this cycle (PLAT-795 anti-false-fire gate)")
    else:
        for lane in exact_lanes:
            team = lane['team_key']
            org_label = lane.get('organization_slug') or f"org-{lane['organization_id']}"
            pod = f'{org_label}::{team}'
            lane_key = f"{lane['organization_id']}:{team}"
            if team in (PM, 'governance'):
                continue
            # ADR-0058 (PLS-990): operator-capable lanes (human-ops,
            # ceo-decisions) are owned by a human operator — zero live agent
            # members is the CORRECT state, not an outage. Never file a
            # [Team Outage] carrier for them; clear any stale streak so an
            # existing false signal auto-resolves.
            if W._is_operator_capable_team(team):
                _org_health_clear_streak(f"zero-member-outage:{lane_key}")
                logger.info(
                    f"sweep_org_health: {pod} is an operator-capable lane "
                    f"(ADR-0058); zero agent members is by design — suppressing "
                    "zero-member-outage")
                continue
            queued = lane['queued']
            if not queued:
                continue
            members = lane['members']
            if not members:
                continue
            outage_pods_evaluated += 1
            oldest = min(queued, key=lambda i: i.created_at)
            availability_reasons = _org_health_candidate_availability(
                W, lane, oldest,
            )
            all_down = all(
                reasons != ['available']
                for reasons in availability_reasons.values()
            )
            if not all_down:
                _org_health_clear_streak(f"zero-member-outage:{lane_key}")
                continue
            # PLAT-795: ALL members operator-disabled = intended budget-cap offline,
            # NOT an outage to escalate. A CRASH (down but NOT in the disabled set)
            # still fires below. This is the residual sole-seat-disabled churn case.
            if _all_operator_disabled(members):
                _org_health_clear_streak(f"zero-member-outage:{lane_key}")
                logger.info(
                    f"sweep_org_health: {pod} — all member(s) operator-disabled "
                    f"(intended budget-cap state); suppressing zero-member-outage")
                continue
            recent_activity = _org_health_recent_active_team_activity(members, now)
            if recent_activity:
                _org_health_clear_streak(f"zero-member-outage:{lane_key}")
                logger.info(
                    "sweep_org_health: suppressing zero-member-outage:%s — "
                    "AgentAvailability(active=True) members produced %s Pulse event(s) "
                    "(%s comments, %s activities) in the last %sm: %s",
                    pod, recent_activity['events'], recent_activity['comments'],
                    recent_activity['activities'], recent_activity['window_minutes'],
                    ', '.join(recent_activity['active_members']),
                )
                continue
            age_h = (now - oldest.created_at).total_seconds() / 3600.0
            outage_candidates.append((
                lane_key, pod, members, queued, oldest, age_h, lane,
                availability_reasons,
            ))
            present_admin_sigs.add(f"zero-member-outage:{lane_key}")

    # PLAT-795 global-sanity guard: if a large fraction of evaluated pods ALL read
    # zero-available in ONE cycle, that is far more likely an exporter / availability
    # data glitch than that many simultaneous whole-team outages — emit ONE
    # consolidated signal instead of one false task per team (the 19:45 pattern,
    # where the exporter served process_up=0 for live agents).
    _sanity_min = int(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_SANITY_MIN', '4'))
    _sanity_frac = float(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_SANITY_FRAC', '0.5'))
    if (len(outage_candidates) >= _sanity_min and outage_pods_evaluated
            and len(outage_candidates) >= _sanity_frac * outage_pods_evaluated):
        _pods = ', '.join(display for _key, display, *_ in outage_candidates)
        for _key, *_rest in outage_candidates:
            _org_health_clear_streak(f"zero-member-outage:{_key}")
            present_admin_sigs.add(f"zero-member-outage:{_key}")
        admin_findings.append((
            'availability-data-outage', 'urgent',
            (f"[Availability Data] {len(outage_candidates)} teams read zero-available at "
             f"once — likely exporter glitch, not {len(outage_candidates)} real outages"),
            (f"{len(outage_candidates)} of {outage_pods_evaluated} evaluated teams ({_pods}) "
             f"ALL read zero available members in a single detector cycle. That pattern is "
             f"far more likely an agent-health exporter / `pulse:agent_availability` data "
             f"outage than {len(outage_candidates)} simultaneous whole-team outages, so this "
             f"is ONE consolidated signal instead of one false task per team (PLAT-795; the "
             f"2026-06-24 19:45 mass false-fire). **Check:** the exporter "
             f"(`PULSE_HEALTH_EXPORTER_URL`) and the `pulse:agent_availability` Redis key. If "
             f"the teams are actually staffed this self-resolves on the next clean cycle. If a "
             f"genuinely broad outage IS confirmed, address it per affected team."),
        ))
    else:
        # PLAT-795 debounce: require the outage to persist DEBOUNCE consecutive
        # cycles before filing, so a single transient bad scrape can't fire a task.
        _debounce = int(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_DEBOUNCE', '2'))
        for (lane_key, pod, members, queued, oldest, age_h, lane,
             availability_reasons) in outage_candidates:
            sig = f"zero-member-outage:{lane_key}"
            streak = _org_health_outage_streak(sig, _debounce)
            if streak < _debounce:
                logger.info(
                    f"sweep_org_health: {sig} detected (streak {streak}/{_debounce}) — "
                    f"debouncing, not filing yet")
                continue
            admin_snapshot = _org_health_admin_team_snapshot(
                lane['organization_id'], lane.get('organization_slug') or '',
                lane['team_key'],
            )
            admin_members = set(admin_snapshot.get('members') or [])
            observed_admin_members = set(
                admin_snapshot.get('observed_members') or admin_members
            )
            drift = _org_health_admin_routing_drift(admin_snapshot, members)
            drift_text = (
                f"admin_only={', '.join(drift['admin_only']) or '(none)'}, "
                f"routing_only={', '.join(drift['routing_only']) or '(none)'}"
                if drift is not None else
                f"drift=unavailable ({admin_snapshot.get('reason') or admin_snapshot.get('status')})"
            )
            reason_text = ', '.join(
                f'{email}={"|".join(reasons)}'
                for email, reasons in sorted(availability_reasons.items())
            )
            # PLS-854 defect 3/4: headline computed from the roster it prints.
            # "0 live members" only when EVERY member carries seat_not_live;
            # otherwise name how many are live-but-capability-filtered.
            offline_count = sum(
                1 for _r in availability_reasons.values()
                if 'seat_not_live' in (_r or [])
            )
            live_but_filtered = len(members) - offline_count
            if live_but_filtered == 0:
                live_clause = '0 live members'
            else:
                live_clause = (
                    f'{offline_count}/{len(members)} offline, '
                    f'{live_but_filtered} live but capability-filtered'
                )
            admin_findings.append((
                sig, 'urgent',
                f"[Team Outage] {pod} — {live_clause}, {len(queued)} task(s) queued, oldest {age_h:.0f}h",
                (f"**WHOLE-TEAM OUTAGE: {pod}** — all {len(members)} exact-source member(s) are "
                 f"offline/unavailable for {streak}+ consecutive detector cycles while "
                 f"{len(queued)} task(s) wait in queue (oldest: {oldest.item_key}, {age_h:.0f}h). "
                 f"Routing authority: organization_id={lane['organization_id']}, "
                 f"team={lane['team_key']}, source={lane['source']}, "
                 f"version={lane['source_version']}, members={', '.join(members)}, "
                 f"availability=[{reason_text}]. "
                 f"Liveness detail (PLS-854): {live_but_filtered}/{len(members)} member(s) are LIVE "
                 f"but scored unavailable by a capability predicate ({offline_count} offline): a "
                 f"live-but-capability-filtered seat is NOT a crash. "
                 f"Admin directory (diagnostic only): "
                 f"status={admin_snapshot.get('status')}, team_id={admin_snapshot.get('team_id')}, "
                 f"members={', '.join(sorted(observed_admin_members)) or '(none)'}, "
                 f"{drift_text}. "
                 f"Admin membership does not authorize routing. "
                 f"The rebalance sweep cannot reroute these tasks — there is no available peer in "
                 f"this team to receive them. "
                 f"**Action required — pick by liveness:** "
                 f"(1) if the {offline_count} offline member(s) merely CRASHED, recover them "
                 f"(bridge process, provider health, restart) — that is fixing an issue. "
                 f"(2) if {live_but_filtered} member(s) are live but capability-filtered "
                 f"(seat_incapable and the like), this is a capability-routing condition, NOT a "
                 f"crash: check the seat/availability verdict and routing intent (PLS-721, PLAT-5032) "
                 f"and fix what the scorer rejects — leave the live seats enabled; do NOT restart "
                 f"them. "
                 f"(3) if the operator explicitly disabled/stopped a member, that stop is "
                 f"INTENTIONAL — do NOT re-enable it and do NOT add a second agent against an "
                 f"explicit current stop. (The prior 'budget cap: 1 agent/team' directive of "
                 f"2026-06-22 was LIFTED on 2026-06-26 — teams may run multiple agents, so a "
                 f"legitimately stopped sole seat is a provisioning choice, not a hard cap.) "
                 f"Where no recovery applies, reassign queued tasks to an available peer pod or "
                 f"have the coordinator pick the work up directly. "
                 f"Refs: PLAT-532, PLAT-367 (prior whole-team deadlock incident), PLS-854."),
            ))

    # ── signal 3b: ZERO-MEMBER team with active work (PLS-197, PLAT-532 gap) ──
    # Consume the same exact-org, versioned candidate source as the resolver.
    # Directory teams remain diagnostic-only and never confer routing authority.
    zero_member_findings = []
    zero_member_streak_ops = []
    try:
        from .services.routing_authority import routing_candidate_snapshot_for_item
        # Keep this family immediate by default: zero-member-team drift is a
        # routing/invariants failure that should alert on first detected cycle.
        _zm_debounce = int(os.environ.get('PULSE_ORG_HEALTH_ZERO_MEMBER_DEBOUNCE', '1'))
        _raw_lanes = (
            list(
                Item.objects.filter(mode='task', is_archived=False, resolution__isnull=True)
                .exclude(status__in=excluded)
                .exclude(assignment_group__isnull=True).exclude(assignment_group='')
                .values_list('project__org_id', 'assignment_group').distinct()
            )
            if exact_snapshot_complete else []
        )
        _lanes_by_identity = {}
        for _org_id, _raw in _raw_lanes:
            _norm = W._canonical_team_name(_raw)
            if _org_id is not None and _norm:
                _lanes_by_identity.setdefault((int(_org_id), _norm), set()).add(_raw)
        for (_org_id, _norm), _raws in sorted(_lanes_by_identity.items()):
            if _norm in (PM, 'governance'):
                continue
            # ADR-0058 (PLS-990): operator-capable lanes (human-ops,
            # ceo-decisions) are owned by a human operator — zero live agent
            # members is the CORRECT state, not a routing failure. Never file a
            # [Zero-Member Team] carrier for them; clear any stale streak so an
            # existing false signal auto-resolves.
            if W._is_operator_capable_team(_norm):
                zero_member_streak_ops.append(('clear', f"zero-member-team:{_org_id}:{_norm}", None))
                logger.info(
                    f"sweep_org_health: {_norm} is an operator-capable lane "
                    f"(ADR-0058); zero agent members is by design — suppressing "
                    "zero-member-team")
                continue
            _active_qs = (
                Item.objects.filter(mode='task', project__org_id=_org_id,
                                    assignment_group__in=sorted(_raws),
                                    is_archived=False, resolution__isnull=True)
                .exclude(status__in=excluded)
            )
            _count = _active_qs.count()
            if not _count:
                continue
            _oldest = _active_qs.select_related('project').order_by('created_at').first()
            _snapshot = routing_candidate_snapshot_for_item(_norm, _oldest)
            _members = [
                str(row.get('email') or '').strip().lower()
                for row in _snapshot.get('candidates', [])
                if isinstance(row, dict) and str(row.get('email') or '').strip()
            ]
            _org_label = _snapshot.get('organization_slug') or f'org-{_org_id}'
            _lane_label = f'{_org_label}::{_norm}'
            _sig = f"zero-member-team:{_org_id}:{_norm}"
            if _members:
                zero_member_streak_ops.append(('clear', _sig, None))
                continue
            # PLS-743: personal workspaces are auto-provisioned single-human orgs
            # with no teams by construction — a missing TeamRoster row there is
            # the permanent correct answer, not a repairable zero-member-team
            # defect. Skip the carrier; the reconciliation below auto-clears any
            # existing one (existing_admin_sigs - active_admin).
            if W.org_is_personal_workspace(_org_id):
                zero_member_streak_ops.append(('clear', _sig, None))
                continue
            _age_h = (now - _oldest.created_at).total_seconds() / 3600.0 if _oldest else 0.0
            _admin = _org_health_admin_team_snapshot(_org_id, _org_label, _norm)
            _legacy_diagnostic = (
                'NOT registered in Pulse TeamRoster'
                if _snapshot.get('reason') == 'team_roster_missing'
                else 'empty TeamRoster entry'
                if _snapshot.get('reason') == 'team_roster_empty'
                else 'versioned routing authority has no active candidate'
            )
            zero_member_streak_ops.append(('outage', _sig, {
                'lane_label': _lane_label,
                'count': _count,
                'age_h': _age_h,
                'oldest_key': _oldest.item_key if _oldest else 'n/a',
                'organization_id': _org_id,
                'snapshot': _snapshot,
                'admin': _admin,
                'legacy_diagnostic': _legacy_diagnostic,
            }))
    except Exception as e:
        zero_member_snapshot_complete = False
        logger.warning(f"sweep_org_health: zero-member-team detector failed: {e}")
        _report_zero_member_team_detector_failure(
            signal='zero-member-team:*', trigger='sweep:snapshot', error=e,
        )
    else:
        # Apply debounce state only after every exact-item lane and diagnostic
        # snapshot succeeds. A later lane failure must not clear or advance an
        # earlier lane's durable streak evidence.
        for _op, _sig, _context in zero_member_streak_ops:
            if _op == 'clear':
                _org_health_clear_streak(_sig)
                continue
            _streak = _org_health_outage_streak(_sig, _zm_debounce)
            present_admin_sigs.add(_sig)  # PLS-854: detected this cycle (even below debounce)
            if _streak < _zm_debounce:
                logger.info(
                    f"sweep_org_health: {_sig} detected (streak {_streak}/{_zm_debounce}) — "
                    f"debouncing, not filing yet")
                continue
            _snapshot = _context['snapshot']
            _admin = _context['admin']
            zero_member_findings.append((
                _sig, 'urgent',
                f"[Zero-Member Team] {_context['lane_label']} — no members at all, "
                f"{_context['count']} active task(s), oldest {_context['age_h']:.0f}h",
                (f"**ZERO-MEMBER TEAM: {_context['lane_label']}** — "
                 f"{_context['legacy_diagnostic']}; exact routing authority has no candidates "
                 f"(organization_id={_context['organization_id']}, source={_snapshot['source']}, "
                 f"version={_snapshot['source_version']}, reason={_snapshot['reason']}). "
                 f"Admin directory is diagnostic-only: status={_admin.get('status')}, "
                 f"team_id={_admin.get('team_id')}, members="
                 f"{', '.join(_admin.get('members') or []) or '(none)'}. "
                 f"{_context['count']} active task(s) are routed to it via assignment_group "
                 f"(oldest: {_context['oldest_key']}, {_context['age_h']:.0f}h). "
                 f"Nothing can ever pull or rebalance this work — it is silently swallowed. "
                 f"**Action required:** repair the reviewed routing authority source or "
                 f"reroute its tasks; never copy Admin membership into routing. "
                 f"Refs: PLS-197 (R-05 game-day fail), PLAT-532 (owning invariant), PLAT-540."),
            ))
        # Publish this family's findings only after the complete scan and its
        # staged streak operations have succeeded.
        admin_findings.extend(zero_member_findings)

    # ── reconcile findings → DM the PM lead; tasks ONLY for legacy cleanup ──
    # HIVE-694 (operator 2026-07-12, "are we seriously creating tasks for all
    # this? aren't alert DMs enough?"): PM-team org-health findings are
    # TRANSIENT, self-clearing health conditions (capacity-starve,
    # backlog-pressure, throughput-flatline, recurring-stale-cycles) — the
    # alert class our standing rule already routes as DMs, never Pulse tasks.
    # Filing tasks for them bloated the board and the Home live feed. Now:
    # DM the PM lead ONCE on signal onset (redis-deduped; re-ping only if the
    # condition persists past PULSE_ORG_HEALTH_DM_REPING_SECONDS) and once on
    # clear. Existing signal tasks are still auto-resolved below (legacy
    # cleanup) with a workflow-VALID terminal status — the old literal
    # 'resolved' was coerced by _coerce_invalid_status back to the workflow's
    # INITIAL status (autonomous-dev has no 'resolved'), so the task never
    # left the reconcile set and the sweep re-commented "signal cleared —
    # auto-resolving" every 10 minutes, forever (PLAT-4188: 100+ identical
    # comments over a day).
    # Adopt pre-exact-org carriers before building the DM/task suppression maps.
    # This is required for legacy Deferred sentinels such as
    # ``capacity-starve:review``; otherwise the first scoped detector cycle
    # would emit again under ``capacity-starve:1:review``.
    for _sig, *_ in findings:
        _adopt_legacy_org_health_signal_carriers(_sig)

    existing = {}
    deferred_sigs = set()  # PLAT-756: operator-deferred signals suppress re-fires
    for it in Item.objects.filter(
        mode='task', metadata__org_health_signal__isnull=False,
    ):
        sig = (it.metadata or {}).get('org_health_signal')
        if not sig:
            continue
        if not (
            sig.startswith('capacity-starve:')
            or sig.startswith('backlog-pressure:')
            or sig in {'throughput-flatline', 'recurring-stale-cycles'}
        ):
            continue
        if (it.status or '').lower() == 'deferred':
            deferred_sigs.add(sig)
        elif _is_terminal_status(it.status):
            continue
        else:
            existing[sig] = it
    active = set()
    dmed = resolved = 0
    reping = int(os.environ.get('PULSE_ORG_HEALTH_DM_REPING_SECONDS', '21600'))
    try:
        from .services.workflow_service import WorkflowService
        _rc = WorkflowService._redis_client()
    except Exception:
        _rc = None
    for sig, prio, title, body in findings:
        active.add(sig)
        if sig in deferred_sigs:
            continue  # operator-parked
        if emit_org_health_signal(
            sig,
            {'priority': prio, 'title': title, 'body': body},
            workflow_service=W,
            now=now,
            redis_client=_rc,
            reping_seconds=reping,
        ):
            dmed += 1
    # auto-resolve cleared signals (legacy signal tasks + clear-notify)
    for sig, it in existing.items():
        if sig in active:
            continue
        if _is_exact_routing_signal(sig) and not _exact_signal_snapshot_complete(sig):
            continue
        try:
            last = Comment.objects.filter(item=it).order_by('-created_at').first()
            if not (last and '[org-health] signal cleared' in (last.content or '')):
                Comment.objects.create(item=it, author_id=0, author_email='system@pulse',
                    content='[org-health] signal cleared — condition no longer detected; auto-resolving.')
            # NB: Item.resolution is a ForeignKey to Resolution — the old
            # `it.resolution = 'fixed'` raised on ASSIGNMENT, the bare
            # `except: pass` swallowed it, so the comment landed but the
            # status change NEVER SAVED. That (with the 'resolved' status
            # coercion) is why signal tasks re-commented every 10 minutes
            # forever. Status alone is sufficient; don't touch resolution.
            it.status = _terminal_status_for_workflow(it)
            try:
                it._activity_actor = (0, 'system@pulse')
            except Exception:
                pass
            it.save(update_fields=['status', 'updated_at'])
            resolved += 1
        except Exception:
            logger.warning("sweep_org_health: auto-resolve failed for %s",
                           it.item_key or it.pk, exc_info=True)
    # clear-side DM + dedupe-key release for signals that stopped firing
    if _rc is not None:
        try:
            for key in _rc.scan_iter('pulse:org_health_dm:*', count=200):
                sig = key.split('pulse:org_health_dm:', 1)[-1]
                if _is_exact_routing_signal(sig) and not _exact_signal_snapshot_complete(sig):
                    continue
                if sig not in active:
                    _rc.delete(key)
                    _dm_program_management_lead(
                        W,
                        f"✅ [org-health] cleared: {sig} — condition no longer detected.",
                    )
        except Exception:
            pass
    if findings or dmed or resolved:
        logger.warning(f"sweep_org_health: findings={len(findings)} dmed={dmed} resolved={resolved}")

    # Admin findings remain durable tasks; HIVE-694 removed the PM-task path,
    # but this branch still needs the project used to allocate their item keys.
    proj = Project.objects.filter(key='PLAT').first() or Project.objects.order_by('id').first()

    # ── reconcile admin-findings → admin-ops tasks (PLAT-532) ──
    ADMIN = 'admin-ops'
    existing_admin_sigs = set()
    for it in Item.objects.filter(
        mode='task',
        metadata__org_health_signal__isnull=False,
    ):
        sig = (it.metadata or {}).get('org_health_signal')
        if not sig or not (sig.startswith('zero-member-outage:') or sig.startswith('zero-member-team:') or sig == 'availability-data-outage'):
            continue
        status = (it.status or '').lower()
        if status == 'deferred' or not _is_terminal_status(status):
            existing_admin_sigs.add(sig)
    admin_filed = admin_resolved = 0
    filed_this_sweep_admin = set()  # PLAT-756: suppress duplicate emissions within one sweep
    for sig, prio, title, body in admin_findings:
        try:
            reconciliation = _reconcile_org_health_signal_carriers(
                sig,
                signal_active=True,
            )
        except Exception as exc:
            if sig.startswith('zero-member-team:'):
                _report_zero_member_team_detector_failure(
                    signal=sig, trigger='sweep:onset-reconcile',
                    project_id=getattr(proj, 'id', None), error=exc,
                )
            raise
        admin_resolved += len(reconciliation['resolved'])
        if reconciliation['carrier'] or sig in filed_this_sweep_admin:
            continue
        try:
            uid, email = W._resolve_team_assignee(ADMIN, exclude_email=None, enforce_cap=False)
        except Exception:
            uid, email = None, None
        # PLS-854 defect 2: a re-fire of the same (org, team) signal must be linked
        # to its predecessor and name it + the cumulative incident age, so triage
        # never restarts from zero on a recurrence.
        previous_carriers = list(
            Item.objects.filter(mode='task', metadata__org_health_signal=sig)
            .order_by('created_at', 'id')
        )
        predecessor = previous_carriers[-1] if previous_carriers else None
        incident_first_seen = (
            previous_carriers[0].created_at if previous_carriers else now
        )
        if predecessor is not None and predecessor.pk is not None:
            cumulative_h = (now - incident_first_seen).total_seconds() / 3600.0
            body = (f"{body}\n\n**Recurrence (PLS-854):** this is a re-fire of the same "
                    f"(organization, team) org-health signal. Predecessor carrier: "
                    f"**{getattr(predecessor, 'item_key', None) or predecessor.pk}** "
                    f"(created {incident_first_seen:%Y-%m-%d %H:%M}%z). Incident first seen "
                    f"{incident_first_seen:%Y-%m-%d %H:%M}%z — cumulative age "
                    f"**{cumulative_h:.0f}h** (linked `relates_to`).")
        try:
            key = proj.get_next_item_key() if proj else None
            it = Item.objects.create(
                project=proj, item_key=key, title=title[:200], mode='task', status='open',
                workflow_name='autonomous-dev', assignment_group=ADMIN, priority=prio,
                created_by_email='system@pulse', created_by_id=0,
                description=body, metadata={'org_health_signal': sig, 'auto_filed': now.isoformat()},
            )
            if predecessor is not None and predecessor.pk is not None:
                try:
                    from .models import ItemLink
                    ItemLink.objects.create(
                        source_item=predecessor, target_item=it,
                        link_type='relates_to',
                        created_by_id=0, created_by_email='system@pulse',
                    )
                    logger.info(
                        f"sweep_org_health: linked recurrence {it.item_key} relates_to "
                        f"{getattr(predecessor, 'item_key', None) or predecessor.pk} (PLS-854)")
                except Exception as link_exc:
                    logger.warning(
                        f"sweep_org_health: failed to link recurrence for {sig}: {link_exc}")
            if uid or email:
                it.assign({'id': uid, 'email': email}, save=True)
            filed_this_sweep_admin.add(sig)
            admin_filed += 1
            logger.warning(f"sweep_org_health: FILED {it.item_key} → {ADMIN} ({email or 'unrouted'}) signal='{sig}'")
        except Exception as e:
            logger.warning(f"sweep_org_health: failed to file '{sig}': {e}")
            if sig.startswith('zero-member-team:'):
                # The complete-scan read succeeded, but the detector did not
                # finish its write-side contract.  Do not publish a success
                # heartbeat (or clear the bounded failure alert) later in this
                # same sweep.
                zero_member_snapshot_complete = False
                _report_zero_member_team_detector_failure(
                    signal=sig, trigger='sweep:file-carrier',
                    project_id=getattr(proj, 'id', None), error=e,
                )
    # PLS-854 defect 1: the clear path must require the fire condition to be
    # ABSENT for the same 2+ consecutive detector cycles the fire path requires
    # it to be present. A single clean read must not terminalize an urgent
    # carrier (PLAT-5723 cleared in exactly one 10-minute cycle with 422 queued).
    _clear_debounce = int(os.environ.get('PULSE_ORG_HEALTH_OUTAGE_CLEAR_DEBOUNCE', '2'))
    for sig in sorted(existing_admin_sigs):
        if sig in present_admin_sigs:
            _org_health_reset_absent_streak(sig)
            continue
        if _is_exact_routing_signal(sig) and not _exact_signal_snapshot_complete(sig):
            continue
        absent = _org_health_absent_streak(sig, _clear_debounce)
        if absent < _clear_debounce:
            logger.info(
                f"sweep_org_health: {sig} absent (streak {absent}/{_clear_debounce}) — "
                f"carrier kept, not cleared yet (PLS-854)")
            continue
        cleared_detail = (
            _org_health_outage_clear_evidence(W, sig, now)
            or 'condition no longer detected'
        )
        try:
            reconciliation = _reconcile_org_health_signal_carriers(
                sig,
                signal_active=False,
                cleared_detail=cleared_detail,
                preserve_routed_carrier_on_clear=sig.startswith('zero-member-team:'),
            )
        except Exception as exc:
            if sig.startswith('zero-member-team:'):
                _report_zero_member_team_detector_failure(
                    signal=sig, trigger='sweep:clear-reconcile',
                    project_id=getattr(proj, 'id', None), error=exc,
                )
            raise
        _org_health_reset_absent_streak(sig)
        admin_resolved += len(reconciliation['resolved'])
    if admin_findings or admin_filed or admin_resolved:
        logger.warning(f"sweep_org_health: admin findings={len(admin_findings)} filed={admin_filed} resolved={admin_resolved}")

    # ── workflow-configured stale timers ─────────────────────────────────────
    # The engine supplies only the generic "item stale in configured status/group
    # for N hours → execute this workflow transition's post-functions" mechanism.
    # Organisation-specific escalation policy (groups, markers, comments, routing)
    # lives in WorkflowTransition JSON.
    stale_timer_marked = _run_workflow_stale_timers(now)

    if zero_member_snapshot_complete:
        _record_zero_member_team_detector_success(
            signal='zero-member-team:*', trigger='sweep:complete',
            project_id=getattr(proj, 'id', None), complete_scope=True,
        )

    return {
        # PM-side findings are DM-only (HIVE-694); preserve the legacy `filed`
        # counter contract as zero and expose successful deliveries separately.
        'findings': len(findings), 'filed': 0, 'dmed': dmed, 'resolved': resolved,
        'admin_findings': len(admin_findings), 'admin_filed': admin_filed, 'admin_resolved': admin_resolved,
        'routing_snapshot': routing_snapshot,
        'stale_timer_marked': stale_timer_marked,
    }


# PLAT-708: actor stamped on AgentAvailability rows the health sweep owns. Lets us
# auto-reactivate ONLY rows we deactivated and NEVER touch an operator force-OFF.
# PLS-742: single source of truth lives on the model (AgentAvailability.
# ACTOR_HEALTH_SWEEP); this alias keeps every writer/reader on that one constant.
from .models import AgentAvailability as _AgentAvailabilityModel
HEALTH_SWEEP_ACTOR = _AgentAvailabilityModel.ACTOR_HEALTH_SWEEP


def _apply_provider_down_cooldown(avail, provider_down, previous_until, now_ts, cooldown_seconds):
    """Keep provider-down assignment availability sticky for a short cooldown.

    Provider/token-pool telemetry can flap between scrapes while a provider is
    exhausted. Without stickiness, a rebalance run that lands on one "healthy"
    scrape treats the agent as eligible and leaves work pinned there. The
    cooldown is assignment-only: it writes the availability map as down, but it
    does not stop or disable the runtime.

    Prefer ``_apply_health_signal_hysteresis`` for entry+exit duration gates; this
    cooldown remains as an optional extra recovery floor for provider_down.
    """
    normalized_down = {
        (email or '').strip().lower()
        for email in (provider_down or [])
        if (email or '').strip()
    }
    if cooldown_seconds <= 0:
        return [], {}

    until = {}
    for email, raw_until in (previous_until or {}).items():
        em = (email or '').strip().lower()
        if not em:
            continue
        try:
            expiry = float(raw_until)
        except (TypeError, ValueError):
            continue
        if expiry > now_ts:
            until[em] = expiry

    new_expiry = now_ts + cooldown_seconds
    for email in normalized_down:
        until[email] = max(until.get(email, 0), new_expiry)

    sticky = []
    for email, expiry in sorted(until.items()):
        if expiry <= now_ts:
            continue
        if avail.get(email) is not False:
            avail[email] = False
            if email not in normalized_down:
                provider_down.append(email)
                sticky.append(email)

    return sticky, until


# Redis state for sustained health demotion (entry + exit hysteresis).
_HEALTH_HYSTERESIS_KEY = 'pulse:agent_health_hysteresis:v1'
# Instantaneous Connect/Hive-style bad signals that are NOT soft-protected once
# sustained: drain owned work immediately (same class as wedged).
_HARD_HEALTH_UNAVAILABILITY_REASONS = frozenset({'runtime_unhealthy'})


def _health_issue_types(health):
    """Normalize issue-type tokens from Connect/Hive health payloads."""
    if not isinstance(health, dict):
        return set()
    tokens = set()
    for key in ('issue_types', 'issues'):
        raw = health.get(key)
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if isinstance(item, dict):
                    t = item.get('type') or item.get('issue_type') or item.get('label') or ''
                    if t:
                        tokens.add(str(t).strip().lower())
                elif item:
                    tokens.add(str(item).strip().lower())
        elif isinstance(raw, str) and raw.strip():
            tokens.add(raw.strip().lower())
    summary = health.get('summary') or health.get('health_summary') or {}
    if isinstance(summary, dict):
        for item in summary.get('issues') or []:
            if isinstance(item, dict):
                t = item.get('type') or item.get('issue_type') or item.get('label') or ''
                if t:
                    tokens.add(str(t).strip().lower())
        for t in summary.get('issue_types') or []:
            if t:
                tokens.add(str(t).strip().lower())
    return tokens


def _health_consecutive_errors(health):
    if not isinstance(health, dict):
        return 0
    for key in (
        'consecutive_errors',
        'health_consecutive_errors',
        'error_turns_total',
        'health_error_turns_total',
    ):
        raw = health.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _classify_connect_health_signal(health, runtime=None):
    """Classify an instantaneous Connect/Hive health snapshot.

    Returns a reason string or None if the sample looks fine. Instantaneous only
    — callers must apply ``_apply_health_signal_hysteresis`` before demoting so
    multi-second blips do not thrash routing.
    """
    health = health if isinstance(health, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}

    if health.get('token_pool_unavailable') is True:
        return 'provider_down'
    if health.get('provider_unavailable') is True:
        return 'provider_down'

    gateway_error_text = ' '.join(
        str(error).lower()
        for error in (health.get('recent_errors') or [])
    )
    gateway_provider_error = (
        runtime.get('harness') == 'shizuha-gateway'
        and bool(gateway_error_text)
        and (
            'cannot connect to vllm' in gateway_error_text
            or 'cannot connect to cortex' in gateway_error_text
            or (
                any(token in gateway_error_text for token in (
                    'econnrefused', 'enetunreach', 'ehostunreach', 'timed out', 'timeout',
                ))
                and any(provider in gateway_error_text for provider in (
                    'vllm', 'cortex', 'model provider', 'ai service',
                ))
            )
            or (
                'provider' in gateway_error_text
                and any(state in gateway_error_text for state in (
                    'unavailable', 'unreachable', 'offline', 'down',
                ))
            )
        )
    )
    if gateway_provider_error:
        return 'provider_down'

    issues = _health_issue_types(health)
    if 'runtime_unhealthy' in issues or 'provider_unavailable' in issues:
        if 'provider_unavailable' in issues and 'runtime_unhealthy' not in issues:
            return 'provider_down'
        return 'runtime_unhealthy'

    # Explicit needs_help / health.ok=false with a sustained error ring is the
    # Agy-class failure: Gemini (and similar) tool/LLM hard-fails while process
    # and provider flags still claim "up". Treat as runtime_unhealthy.
    consecutive = _health_consecutive_errors(health)
    import os
    try:
        err_threshold = int(os.environ.get('PULSE_RUNTIME_UNHEALTHY_ERROR_THRESHOLD', '3'))
    except (TypeError, ValueError):
        err_threshold = 3
    if err_threshold < 1:
        err_threshold = 1
    needs_help = health.get('needs_help') is True
    ok_false = health.get('ok') is False
    recent_n = len(health.get('recent_errors') or [])
    # health.ok=false is itself a runtime-unhealthy sample (duration-gated later).
    # Require at least one corroborating signal (error ring / needs_help /
    # consecutive counter) so a transient ok-flip with an empty ring is ignored.
    if ok_false and (recent_n > 0 or needs_help or consecutive >= err_threshold):
        return 'runtime_unhealthy'
    if needs_help and consecutive >= err_threshold:
        return 'runtime_unhealthy'
    if consecutive >= max(err_threshold * 2, 6) and recent_n > 0:
        # High consecutive error counter with a live ring, even if ok is sticky-true
        # on older bridges.
        return 'runtime_unhealthy'
    return None


def _apply_health_signal_hysteresis(
    raw_bad,
    previous_state,
    now_ts,
    demote_after_sec,
    recover_after_sec,
):
    """Gate health demotion/recovery on sustained signal duration.

    Instantaneous bad samples (provider_unavailable, runtime_unhealthy, …) are
    ignored until they persist for ``demote_after_sec``. Once demoted, a short
    healthy flap does not restore eligibility until ``recover_after_sec`` of
    continuous good samples. Returns ``(sustained_down: {email: reason}, next_state)``.
    """
    try:
        demote_after_sec = int(demote_after_sec)
    except (TypeError, ValueError):
        demote_after_sec = 180
    try:
        recover_after_sec = int(recover_after_sec)
    except (TypeError, ValueError):
        recover_after_sec = 180
    if demote_after_sec < 0:
        demote_after_sec = 0
    if recover_after_sec < 0:
        recover_after_sec = 0

    raw = {}
    for email, reason in (raw_bad or {}).items():
        em = (email or '').strip().lower()
        if not em:
            continue
        why = (reason or 'provider_down').strip().lower() or 'provider_down'
        raw[em] = why

    prev = previous_state if isinstance(previous_state, dict) else {}
    emails = set(raw) | {(e or '').strip().lower() for e in prev if e}
    next_state = {}
    sustained = {}

    for em in sorted(emails):
        old = prev.get(em) if isinstance(prev.get(em), dict) else {}
        was_down = bool(old.get('down'))
        reason = raw.get(em) or (old.get('reason') or 'provider_down')
        entry = {
            'down': was_down,
            'bad_since': None,
            'good_since': None,
            'reason': reason,
        }
        if em in raw:
            entry['reason'] = raw[em]
            entry['good_since'] = None
            try:
                bad_since = (
                    float(old.get('bad_since'))
                    if old.get('bad_since') is not None
                    else now_ts
                )
            except (TypeError, ValueError):
                bad_since = now_ts
            entry['bad_since'] = bad_since
            if was_down or (now_ts - float(entry['bad_since'])) >= demote_after_sec:
                entry['down'] = True
        else:
            entry['bad_since'] = None
            if was_down:
                try:
                    good_since = float(old.get('good_since')) if old.get('good_since') is not None else now_ts
                except (TypeError, ValueError):
                    good_since = now_ts
                if old.get('good_since') is not None:
                    entry['good_since'] = good_since
                else:
                    entry['good_since'] = now_ts
                if (now_ts - float(entry['good_since'])) >= recover_after_sec:
                    entry['down'] = False
                    entry['good_since'] = None
                    entry['reason'] = None
                else:
                    entry['down'] = True
                    entry['reason'] = (old.get('reason') or reason)
            else:
                # Fully up and healthy — drop from state to keep the map bounded.
                continue

        if entry['down']:
            sustained[em] = entry.get('reason') or 'provider_down'
            next_state[em] = entry
        elif entry.get('bad_since') is not None:
            # Candidate demotion in progress — keep observing.
            next_state[em] = entry

    return sustained, next_state


def _reconcile_health_managed_availability(empty_streaks, threshold, now_iso,
                                           stopped=None, enabled_up=None,
                                           provider_down=None, wakeable=None,
                                           measured_streak_emails=None,
                                           health=None):
    """Propagate a SUSTAINED provider-exhaustion signal into the durable
    ``AgentAvailability`` table so routing AND the UI reflect reality — not just the
    ephemeral Redis map.

    PLAT-708 (operator-flagged 2026-06-22): when codex hit the weekly gpt-5.5 usage
    limit the health sweep correctly marked them unavailable in Redis, but routing
    consults the ``AgentAvailability`` DB row FIRST (``_configured_agent_availability``
    short-circuits on it), and a stale operator ``active=True`` shadowed the
    health-down — so Pulse kept assigning work to dead codex instead of failing over
    to claude. This writes the durable row when exhaustion is sustained, and restores
    it on recovery.

    Signal = ``shizuha_agent_empty_turn_streak`` (consecutive empty turns from the
    codex-bridge /health endpoint, PLAT-394). Deliberately NOT keyed on
    ``provider_healthy``/``pulse_wedge``: those also fire for a BUSY claude agent on a
    long task (its heartbeat is skipped) and would falsely deactivate a productively
    working agent. ``empty_turn_streak`` is codex-rpc-specific (claude bridges never
    emit it) and is itself the sustained-ness, so it cleanly targets real provider
    exhaustion without that false-positive.

    ``empty_streaks`` maps email -> consecutive-empty-turn count, ONLY for agents the
    exporter sampled this scrape. ``provider_down`` is the final, cooldown-stabilized
    per-agent provider/token-pool signal assembled by the sweep. Returns
    ``(deactivated, reactivated)``.

    Rules:
    - ``streak >= threshold`` -> deactivate. Overrides a stale operator ``active=True``
      (a force-ON cannot make a usage-limited provider serve) and takes ownership
      (``updated_by='health-sweep'``).
    - Any inactive row that is NOT a genuine operator toggle (``updated_by`` !=
      ``'operator'``), whose agent is sampled healthy again (``streak == 0``) ->
      reactivate. This covers BOTH our own ``health-sweep`` rows (quota reset) AND
      stale incident deactivations another AGENT/automation stamped during a past
      outage (e.g. ``updated_by='ichi'`` "codex agent DOWN 2026-06-15" or
      ``'shizuha-steward'`` "weekly usage-limit 2026-06-22"). Before this, those
      non-health-sweep rows were treated as sacred and pinned the agent unavailable
      FOREVER even after it recovered (ni/nori stuck 5-12 days, healthy + heartbeating
      but Hive-Unavailable; operator-flagged 2026-06-27). The ``streak == 0`` gate is
      what makes this safe: a genuinely retired/ghost agent has no running container,
      emits no streak sample, and is therefore never reactivated; an exhausted agent
      (streak >= threshold, e.g. san) is likewise not reactivated.
    - A genuine operator force-OFF (``updated_by == 'operator'``) is NEVER
      auto-reactivated — operator intent is sacred.

    PLS-208 extends the same durable propagation to STOPPED/DISABLED agents:
    ``stopped`` = emails the exporter sampled with ``shizuha_agent_enabled=0``
    this scrape (operator toggle off / daemon enabled=false). Their
    unavailability previously lived ONLY in the ephemeral Redis keys
    (``pulse:agent_availability`` / ``pulse:agent_disabled``, TTL ~600s), so a
    dead sweep / expired key / Redis restart made routing fail OPEN and resolve
    NEW work to an agent that has been stopped for days (the sara repro on
    PLS-208). Persisting the toggle into ``AgentAvailability`` makes the durable
    DB row — which ``_configured_agent_availability`` consults FIRST — carry the
    stopped state across every ephemeral-signal outage. ``enabled_up`` = emails
    sampled ``shizuha_agent_enabled=1`` AND available in the final map this
    scrape. It may reactivate only a row this sweep previously created for
    ``shizuha_agent_enabled=0``. Process-up/enabled is not provider recovery:
    self-reported quota/provider deactivations remain inactive until the bridge
    observes a productive turn and restores availability.

    PLS-741 / PLS-869 (JOIN_PAD contract, wiki `1c95857d`): ``empty_turn_streak``
    is NOT measured on ``:9888`` today — the producer emits it as self-identifying
    JOIN_PAD (``placeholder="1"`` label) for the Pulse scrape path. The sweep is
    the only boundary with wire access to that provenance label: it filters JOIN_PAD
    series out and passes ONLY the measured sample set here via
    ``measured_streak_emails``. A bare ``0`` in ``empty_streaks`` is
    indistinguishable from padding, so recovery is granted ONLY for emails the
    caller explicitly certifies as measured. Until the producer wires real values,
    ``measured_streak_emails`` is empty -> both the streak deactivate gate AND the
    streak reactivate gate no-op (fail-closed). Recovery for self-reported provider
    exhaustion flows through ``provider_down`` / ``_provider_recovered`` (real
    MEASUREMENT), not the pad streak.
    """
    from .models import AgentAvailability
    deactivated, reactivated = [], []
    stopped = {(e or '').strip().lower() for e in (stopped or set()) if e}
    enabled_up = {(e or '').strip().lower() for e in (enabled_up or set()) if e}
    provider_down = {
        (e or '').strip().lower() for e in (provider_down or set()) if e
    }
    wakeable = {(e or '').strip().lower() for e in (wakeable or set()) if e}

    # PLS-208: durably deactivate operator-stopped/disabled agents. Mirrors the
    # streak deactivation below, including overriding a stale active=True row —
    # the daemon toggle IS the operator's current intent, so a leftover
    # force-active row must not shadow it.
    for em in sorted(stopped):
        row = AgentAvailability.objects.filter(email__iexact=em).first()
        reason = f'auto: agent stopped/disabled — shizuha_agent_enabled=0 @ {now_iso}'
        if row is None:
            AgentAvailability.objects.create(
                email=em, active=False, status='inactive',
                reason=reason, updated_by=HEALTH_SWEEP_ACTOR)
            deactivated.append(em)
        elif row.active:
            was_by = (row.updated_by or '').strip()
            row.active = False
            row.status = 'inactive'
            row.reason = reason + (f' (was active by {was_by})' if was_by else '')
            row.updated_by = HEALTH_SWEEP_ACTOR
            row.save()
            deactivated.append(em)

    for email, streak in empty_streaks.items():
        em = (email or '').strip().lower()
        if not em or streak < threshold:
            continue
        row = AgentAvailability.objects.filter(email__iexact=em).first()
        reason = (f'auto: provider exhausted — codex empty-turn streak {streak} '
                  f'(>= {threshold}) @ {now_iso}')
        if row is None:
            AgentAvailability.objects.create(
                email=em, active=False, status='inactive',
                reason=reason, updated_by=HEALTH_SWEEP_ACTOR)
            deactivated.append(em)
        elif row.active:
            was_by = (row.updated_by or '').strip()
            row.active = False
            row.status = 'inactive'
            row.reason = reason + (f' (was active by {was_by})' if was_by else '')
            row.updated_by = HEALTH_SWEEP_ACTOR
            row.save()
            deactivated.append(em)

    # Provider health is not Codex-specific. Grok/Claude bridges report explicit
    # provider/token-pool unavailability without incrementing the Codex empty-turn
    # metric. Persist that final debounced/cooldown-stabilized signal too, otherwise
    # an old AgentAvailability(active=True) row wins over the Redis map and routing
    # continues assigning work to a quota-exhausted seat.
    for em in sorted(provider_down):
        row = AgentAvailability.objects.filter(email__iexact=em).first()
        reason = f'auto: provider unavailable — provider-health signal @ {now_iso}'
        if row is None:
            AgentAvailability.objects.create(
                email=em, active=False, status='inactive',
                reason=reason, updated_by=HEALTH_SWEEP_ACTOR)
            deactivated.append(em)
        elif row.active:
            was_by = (row.updated_by or '').strip()
            row.active = False
            row.status = 'inactive'
            row.reason = reason + (f' (was active by {was_by})' if was_by else '')
            row.updated_by = HEALTH_SWEEP_ACTOR
            row.save()
            deactivated.append(em)

    # Auto-reactivate any non-operator deactivation once the agent is sampled healthy
    # (streak == 0 = codex producing output again). Excludes genuine operator toggles
    # (updated_by='operator') which stay sacred; the streak==0 gate keeps ghosts/
    # retired/exhausted agents off (they emit no streak-0 sample). Takes health-sweep
    # ownership so the row is auto-managed from here on.
    # PLS-742: ONE normalised operator actor comparison (strip+lower, same as the
    # Python norm_actor rule) so `Operator`/`OPERATOR ` rows stay sacred here,
    # exactly like the canonical `operator` stamp.
    for row in (AgentAvailability.objects
                .filter(active=False)
                .exclude(AgentAvailability.actor_q(AgentAvailability.ACTOR_OPERATOR))):
        em = (row.email or '').strip().lower()
        # PLS-208: enabled=1 may recover only a row this sweep created from a
        # prior enabled=0 sample. A self-reported provider outage must not be
        # cleared merely because its pod/process remains enabled and running.
        _stopped_row = (
            AgentAvailability.norm_actor(row.updated_by) == HEALTH_SWEEP_ACTOR
            and 'shizuha_agent_enabled=0' in (row.reason or '')
        )
        _provider_row = (
            AgentAvailability.norm_actor(row.updated_by) == HEALTH_SWEEP_ACTOR
            and 'auto: provider unavailable — provider-health signal' in (row.reason or '')
        )
        # PLS-748 (2026-08-18): recovery must observe CAPABILITY, not just
        # reachability. `enabled_up` (shizuha_agent_enabled=1 + process-up) and
        # "not in provider_down" measure that the pod/process is up and the
        # toggle is on — they do NOT measure whether the seat can actually mint
        # a model token and serve a turn (live 2026-07-27: 4 merge seats flipped
        # active=True on 'provider-health signal available' /
        # 'shizuha_agent_enabled=1' while their broker sidecar refused model
        # tokens with coordinator HTTP 404; ~120 items routed into the dead
        # lane). The only capability signal this sweep can observe is a
        # productive turn: `shizuha_agent_empty_turn_streak == 0` (codex
        # producing output again). Gate every recovery path on it; where no such
        # signal is available the seat must NOT silently re-enter the routable
        # set (it stays unavailable / unverified).
        _capable = empty_streaks.get(em, None) == 0
        _reenabled = (
            _stopped_row
            and em in enabled_up
            and em not in stopped
            and _capable
        )
        _provider_recovered = (
            _provider_row
            and em in enabled_up
            and em not in provider_down
            and em not in stopped
            and _capable
        )
        _wakeable_recovered = (
            _stopped_row
            and em in wakeable
            and em not in stopped
            and em not in provider_down
        )
        # A generic streak=0 sample is not recovery evidence for a non-Codex
        # provider-health row (Grok emits no meaningful Codex streak). Such rows
        # recover only from a final available provider sample above.
        #
        # PLS-741 / PLS-869 (JOIN_PAD contract): a bare 0 in `empty_streaks` is
        # indistinguishable from producer padding, so `_streak_recovered` is
        # granted ONLY for emails the caller explicitly certifies as measured via
        # `measured_streak_emails` (the sweep passes the placeholder-filtered
        # set). Missing/pad series are NEVER treated as a measured zero for a
        # recovery grant; with the field still JOIN_PAD on :9888 no email is
        # certified -> no streak recovery (fail-closed).
        _streak_recovered = (
            measured_streak_emails is not None
            and em in measured_streak_emails
            and empty_streaks.get(em, None) == 0
            and not _provider_row
            and em not in provider_down
            and em not in stopped
        )
        # PLAT-6372: a self-deactivated row (updated_by='self:<email>') whose
        # agent is live — enabled=1, up=1/process_up=1, fresh heartbeat (i.e. in
        # enabled_up), not provider-down, and not currently emitting empty turns
        # — is recovered even when the bridge emits NO empty_turn_streak sample
        # (that metric is codex-rpc-specific; claude bridges never emit it). The
        # streak==0 gate alone leaves such a row inactive forever (jun stuck 6d).
        # Operator toggles stay sacred; retired/ghost agents (no container, no
        # sample, not in enabled_up) are never reactivated.
        _self_live_recovered = (
            (row.updated_by or '').strip().lower().startswith('self:')
            and em in enabled_up
            and em not in provider_down
            and (empty_streaks.get(em, None) or 0) == 0
        )
        # PLS-734: a FOREIGN deactivation (updated_by is neither 'operator' nor
        # 'health-sweep' — e.g. 'ichi' / 'shizuha-steward' stamped during a past
        # outage) can never satisfy the capability-gated predicates above on a
        # claude-bridge seat, because claude bridges never emit
        # shizuha_agent_empty_turn_streak (the docstring's own caveat). Such a
        # row therefore pins the seat unavailable FOREVER even after it recovers
        # (the exact ni/nori failure mode the 2026-06-27 fix was written to end,
        # still live for every claude-bridge seat whose row has no self-writer).
        # Mirror what _db_agent_availability already does for health-sweep rows:
        # a CURRENT live UP health sample (health.get(em) is True) is recovery
        # evidence for a foreign row. Guards preserved: operator force-OFF stays
        # sacred (excluded by the queryset), and a ghost/retired agent with no
        # health sample at all (health.get(em) is None, not True) is NOT
        # reactivated. Health-sweep rows are deliberately NOT covered here —
        # they stay capability-gated via _reenabled/_provider_recovered/
        # _streak_recovered above.
        _foreign_row = (
            (row.updated_by or '').strip().lower() != HEALTH_SWEEP_ACTOR
            and (row.updated_by or '').strip().lower() != 'operator'
        )
        _health_recovered = (
            _foreign_row
            and health is not None
            and health.get(em) is True
            and em not in provider_down
            and em not in stopped
        )
        if (
            _streak_recovered or _reenabled or _provider_recovered
            or _wakeable_recovered or _self_live_recovered or _health_recovered
        ):
            prev_by = (row.updated_by or '').strip()
            row.active = True
            row.status = 'active'
            if _wakeable_recovered:
                _why = 'agent hibernated — wakeable for new assignment'
            elif _provider_recovered:
                _why = 'provider recovered — provider-health signal available'
            elif _self_live_recovered:
                _why = 'agent live — enabled=1, up, fresh heartbeat (self-deactivation recovered)'
            elif _reenabled:
                _why = 'agent re-enabled — shizuha_agent_enabled=1 and producing output'
            elif _health_recovered:
                _why = 'agent healthy — live UP sample clears stale foreign deactivation'
            else:
                _why = 'provider recovered — codex producing output again'
            row.reason = (f'auto: {_why} @ {now_iso}'
                          + (f' (cleared stale {prev_by} deactivation)'
                             if prev_by and prev_by != HEALTH_SWEEP_ACTOR else ''))
            row.updated_by = HEALTH_SWEEP_ACTOR
            row.save()
            reactivated.append(em)

    return deactivated, reactivated


def _connect_health_bad_agents():
    """Return ``{email: reason}`` for instantaneous Connect health bad samples.

    Reasons: ``provider_down`` (token/provider/gateway inference) or
    ``runtime_unhealthy`` (health.ok false / needs_help / runtime issue types).
    Instantaneous only — the availability sweep applies duration hysteresis
    before demoting so multi-second flaps do not thrash routing.
    """
    import json
    import os

    if os.environ.get('PULSE_CONNECT_TELEMETRY_AVAILABILITY_ENABLED', '1').strip().lower() in {
        '0', 'false', 'off', 'no', '',
    }:
        return {}

    base = (
        os.environ.get('PULSE_CONNECT_API_URL')
        or getattr(settings, 'SHIZUHA_CONNECT_API_URL', '')
        or 'http://shizuha-connect:8019/api'
    ).strip().rstrip('/')
    if not base or base.lower() in {'disabled', 'off', 'none'}:
        return {}
    path = '/internal/agents/telemetry/' if base.endswith('/api') else '/api/internal/agents/telemetry/'
    try:
        from tasks.net_utils import safe_urlopen  # PLAT-1577: scheme-guarded urlopen
        with safe_urlopen(f'{base}{path}', timeout=5) as resp:
            payload = json.load(resp) or {}
    except Exception as exc:
        logger.warning(f"sweep_agent_availability: connect telemetry unavailable: {exc}")
        return {}

    bad = {}
    for key, snap in (payload.get('agents') or {}).items():
        if not isinstance(snap, dict):
            continue
        health = snap.get('health') or {}
        runtime = snap.get('runtime') or {}
        username = (snap.get('agent_username') or key or '').strip().lower()
        email = (snap.get('email') or (f'{username}@shizuha.com' if username else '')).strip().lower()
        if not email:
            continue
        # Top-level Hive-shaped fields (some telemetry paths hoist health_*).
        if not isinstance(health, dict):
            health = {}
        if health.get('ok') is None and snap.get('health_ok') is not None:
            health = dict(health)
            health['ok'] = snap.get('health_ok')
        if health.get('provider_unavailable') is None and snap.get('provider_unavailable') is not None:
            health = dict(health)
            health['provider_unavailable'] = snap.get('provider_unavailable')
        if health.get('needs_help') is None and snap.get('needs_help') is not None:
            health = dict(health)
            health['needs_help'] = snap.get('needs_help')
        if not health.get('recent_errors') and snap.get('recent_errors'):
            health = dict(health)
            health['recent_errors'] = snap.get('recent_errors')
        if health.get('consecutive_errors') is None and snap.get('health_consecutive_errors') is not None:
            health = dict(health)
            health['consecutive_errors'] = snap.get('health_consecutive_errors')
        if not health.get('health_summary') and snap.get('health_summary'):
            health = dict(health)
            health['health_summary'] = snap.get('health_summary')
        if not runtime and snap.get('running_runtime_harness'):
            runtime = {'harness': snap.get('running_runtime_harness')}
        reason = _classify_connect_health_signal(health, runtime)
        if reason:
            bad[email] = reason
    return bad


def _connect_provider_unavailable_agents():
    """Return agents whose Connect telemetry reports provider/inference unavailable.

    Backward-compatible wrapper: provider/token/gateway only. Runtime-unhealthy
    samples are returned by ``_connect_health_bad_agents`` and gated by hysteresis
    in the availability sweep.
    """
    bad = _connect_health_bad_agents()
    return sorted(em for em, reason in bad.items() if reason == 'provider_down')


def _hive_hibernated_agents():
    """Return ``(emails, ok)`` for Hive ``status=hibernated`` seats.

    Hive is lifecycle SoT and hibernates so Pulse can drain assigned work to a
    live peer. The health exporter often has no sample for a scaled-to-zero
    seat, so availability fail-opens unless this fold writes an explicit DOWN.
    ``ok=False`` means the Hive read failed; callers keep the previous set.
    """
    import json
    import os

    if os.environ.get('PULSE_HIVE_LIFECYCLE_AVAILABILITY_ENABLED', '1').strip().lower() in {
        '0', 'false', 'off', 'no', '',
    }:
        return [], True
    url = (os.environ.get('PULSE_HIVE_LIFECYCLE_URL') or '').strip()
    if not url:
        company_state = (os.environ.get('COMPANY_STATE_HIVE_URL') or '').strip()
        marker = '/api/internal/company-state/'
        if marker in company_state:
            url = (
                company_state.split(marker, 1)[0]
                + '/internal/v1/fleet/lifecycle-availability'
            )
        else:
            url = company_state
    if not url or url.lower() in {'disabled', 'off', 'none'}:
        return [], True
    token = (
        os.environ.get('PULSE_HIVE_LIFECYCLE_TOKEN')
        or os.environ.get('COMPANY_STATE_HIVE_TOKEN')
        or ''
    ).strip()
    params = None
    if 'company-state' in url:
        org = (os.environ.get('COMPANY_STATE_INFRA_ORG_SLUG') or 'shizuha').strip().lower()
        params = {'org_slug': org}
    try:
        from urllib.parse import urlencode
        from urllib.request import Request

        from tasks.net_utils import safe_urlopen

        headers = {}
        if token:
            headers['Authorization'] = (
                token if token.lower().startswith('bearer ') else f'Bearer {token}'
            )
        target = url
        if params:
            sep = '&' if '?' in target else '?'
            target = f'{target}{sep}{urlencode(params)}'
        request = Request(target, headers=headers)
        with safe_urlopen(request, timeout=8) as resp:
            payload = json.load(resp) or {}
    except Exception as exc:
        logger.warning('sweep_agent_availability: hive lifecycle scrape failed: %s', exc)
        return [], False
    emails = []
    for agent in payload.get('agents') or []:
        if not isinstance(agent, dict):
            continue
        status = str(
            agent.get('lifecycle_status') or agent.get('status') or ''
        ).strip().lower()
        if status != 'hibernated':
            continue
        username = str(
            agent.get('username') or agent.get('agent_username') or ''
        ).strip().lower().split('@')[0]
        email = str(agent.get('email') or '').strip().lower()
        if not email and username:
            email = f'{username}@shizuha.com'
        if email:
            emails.append(email)
    return sorted(set(emails)), True


def _unique_org_roster_team(email, organization_id):
    """Return the one TeamRoster slug ``email`` belongs to in ``organization_id``.

    Blank ``assignment_group`` work may drain off a down owner only when that
    owner has exactly one team in the item's org (PLAT-916: blanks are not
    every pod's work). Alias-folded so ``@agents.shizuha.io`` matches the
    primary roster spelling.
    """
    from .services.workflow_service import WorkflowService
    from .models import TeamRoster

    if not email or organization_id is None:
        return None
    aliases = WorkflowService._agent_email_aliases(email)
    if not aliases:
        return None
    matches = []
    try:
        for roster in TeamRoster.objects.filter(
            is_active=True, organization_id=organization_id,
        ):
            members = {
                str(e).strip().lower()
                for e in WorkflowService._flat_roster(roster.tiers or [])
                if e
            }
            juniors = roster.junior_emails or []
            if isinstance(juniors, str):
                juniors = [juniors]
            members.update(str(e).strip().lower() for e in juniors if e)
            member_aliases = set()
            for member in members:
                member_aliases |= WorkflowService._agent_email_aliases(member)
            if aliases & member_aliases:
                matches.append(WorkflowService._canonical_team_name(roster.team_name))
    except Exception:
        logger.warning(
            'unique-org roster team lookup failed for %s org=%s',
            email, organization_id, exc_info=True,
        )
        return None
    uniq = list(dict.fromkeys(matches))
    return uniq[0] if len(uniq) == 1 else None


@shared_task(bind=True, max_retries=1)
def sweep_agent_availability(self):
    """Publish a live per-agent availability map to Redis so team routing can
    fail a pod over from its primary (Codex lead) to a peer (Claude) the moment
    the lead's bridge goes down — no operator action, no env edit.

    Source of truth = the agent-health exporter's local `shizuha_agent_process_up`
    gauge (bridge process present in the container) OR runtime-agnostic
    `shizuha_agent_external_liveness` samples (for remote/BYO agents with recent
    platform activity but no local Docker container). Written to the Redis key
    `pulse:agent_availability` with a TTL so a dead sweep fails OPEN: the key
    expires and routing falls back to the static env policy.
    """
    import os
    import re
    import json
    from .services.workflow_service import WorkflowService

    url = os.environ.get('PULSE_HEALTH_EXPORTER_URL', '').strip()
    if not url or url.lower() in {'disabled', 'off', 'none'}:
        logger.info("sweep_agent_availability: exporter scrape disabled")
        return {'ok': True, 'disabled': True}
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        logger.warning(f"sweep_agent_availability: exporter scrape failed ({url}): {e}")
        return {'ok': False, 'error': str(e)}

    # e.g. shizuha_agent_process_up{agent="ryo",backend="codex"} 1
    rx = re.compile(r'^shizuha_agent_process_up\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    avail = {}
    process_up = {}
    unavailability_reasons = {}
    for m in rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        up = (float(val) >= 1.0)
        process_up[email] = process_up.get(email, False) or up
        # OR-aggregate across an agent's per-backend samples (PLAT-172). The
        # exporter emits one sample per (agent, backend); after a codex->claude
        # consolidation an agent can have a live backend (e.g. claude=1) AND a
        # stale dead one (codex=0). A plain assignment was LAST-WRITE-WINS, so a
        # trailing down line marked a running agent (e.g. aoi) unavailable and the
        # team resolver excluded it. An agent is up if ANY backend reports up.
        avail[email] = avail.get(email, False) or up
        if up:
            unavailability_reasons.pop(email, None)
        elif email not in avail or not avail.get(email):
            unavailability_reasons.setdefault(email, 'offline')
    # PLAT-694: local Docker is not the universe. Remote/BYO agents (MacBooks,
    # public codex runtimes, k3s-native runners, etc.) may have no
    # `shizuha-agent-*` container on s1, so process_up is absent/0 even while the
    # agent is actively using Pulse. The exporter emits a best-effort platform
    # liveness sample from recent Pulse activity; OR it into the same ephemeral
    # availability map so routing can use any alive agent regardless of runtime
    # location. Explicit disabled/provider/wedge folds below still win.
    external_rx = re.compile(r'^shizuha_agent_external_liveness\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    external_live = []
    for m in external_rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        if float(val) >= 1.0:
            avail[email] = True
            unavailability_reasons.pop(email, None)
            external_live.append(email)
    if not avail:
        logger.warning(
            "sweep_agent_availability: parsed 0 process_up/external_liveness samples; not overwriting map"
        )
        return {'ok': False, 'parsed': 0}
    if external_live:
        logger.info(
            f"sweep_agent_availability: EXTERNAL LIVENESS (runtime-agnostic) -> "
            f"available: {sorted(set(external_live))}"
        )

    def _canonical_agent_email(email):
        email = (email or '').strip().lower()
        if email.endswith('@agents.shizuha.io'):
            return email[:-len('@agents.shizuha.io')] + '@shizuha.com'
        return email

    # PLAT-694: runtime-agnostic liveness. The exporter only sees LOCAL docker
    # containers and emits process_up=0 for any roster agent it can't find a
    # container for — ALWAYS the case for remote / BYO runtimes (e.g. v4 codex
    # agents over public shizuha.com), which would otherwise be permanently
    # unavailable. Each authenticated agent request refreshes a short-TTL Redis
    # key pulse:agent_hb:<email> (tasks/authentication.py._record_agent_heartbeat);
    # a live heartbeat means the agent is up AND successfully calling Pulse, so it
    # overrides process_up here. Applied BEFORE the wedge/provider/toggle folds
    # below so those real "alive but don't route" signals still win for LOCAL
    # agents. A dead/disconnected agent stops heartbeating, the key TTL-expires,
    # and it reverts to the exporter signal on the next sweep (fail-safe).
    hb_alive = []
    hb_observed_at = {}
    try:
        _rc_hb = WorkflowService._redis_client()
        for key in _rc_hb.scan_iter('pulse:agent_hb:*', count=200):
            k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            raw_email = k.split('pulse:agent_hb:', 1)[-1].strip().lower()
            email = _canonical_agent_email(raw_email)
            if email:
                avail[email] = True
                unavailability_reasons.pop(email, None)
                hb_alive.append(email)
                # _record_agent_heartbeat stores the observation epoch as the
                # value. Keep it so cached exporter lifecycle state can be
                # ordered against this newer, direct runtime observation.
                try:
                    raw_observed_at = _rc_hb.get(key)
                    observed_at = float(raw_observed_at)
                    hb_observed_at[email] = max(
                        hb_observed_at.get(email, 0),
                        observed_at,
                    )
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"sweep_agent_availability: heartbeat merge failed: {e}")
    if hb_alive:
        logger.info(
            f"sweep_agent_availability: heartbeat-alive -> available (pre-demotion): "
            f"{sorted(hb_alive)}"
        )

    # HIVE/Pulse parity: a heartbeat only proves the agent can reach Pulse; it
    # does not prove the model provider / runtime can serve work. Claude quota
    # exhaustion and Gemini-class runtime_unhealthy (tool schema death, needs_help
    # with error ring) are reported by Connect/Hive telemetry while agents may
    # still heartbeat. Collect instantaneous bad samples here; duration hysteresis
    # below demotes only after the bad signal has been continuous for a few minutes
    # (and recovers only after a few minutes healthy) so short flaps do not thrash.
    telemetry_health_bad = {}
    try:
        telemetry_health_bad = _connect_health_bad_agents() or {}
    except Exception as e:
        logger.warning(f"sweep_agent_availability: connect health fold failed: {e}")
        telemetry_health_bad = {}
    if telemetry_health_bad:
        logger.warning(
            "sweep_agent_availability: CONNECT HEALTH BAD (raw, pre-hysteresis): %s",
            {em: telemetry_health_bad[em] for em in sorted(telemetry_health_bad)},
        )

    # PLAT-160: process-up is NOT alive. The 2026-06-08 outage had ~18 codex
    # agents with process_up=1 wedged in a pre-sampling-compaction retry loop —
    # zero work for ~5.5h while this sweep kept reporting them available and
    # routing kept assigning them work. The exporter now emits
    # shizuha_agent_wedge_errors (wedge-signature lines over a short window: a
    # live loop holds ~200-300, a recovered agent drains to 0 within ~10min).
    # Mark an actively-wedged agent UNAVAILABLE so the team resolver routes
    # around it and the wedge is visible in the sweep log. A missing metric
    # (older exporter) yields no samples => no behavior change.
    wedge_rx = re.compile(r'^shizuha_agent_wedge_errors\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    wedge_threshold = int(os.environ.get('PULSE_WEDGE_THRESHOLD', '10'))
    wedged = []
    for m in wedge_rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        if float(val) >= wedge_threshold and avail.get(email):
            avail[email] = False
            unavailability_reasons[email] = 'wedged'
            wedged.append(email)
    if wedged:
        logger.warning(
            f"sweep_agent_availability: WEDGED (process up, zero work) -> unavailable: {sorted(wedged)}"
        )

    # PLS-15: process-up + not-wedged is STILL not enough if the agent's model
    # PROVIDER is down. A GLM/vLLM endpoint outage leaves GLM agents process-up
    # but retry-looping against a dead /v1 — sweeps kept reporting them
    # available, so queues never failed over to the Sonnet backups every team
    # has (silent org-half-stall). The exporter emits
    # shizuha_agent_provider_healthy{agent=...} (1=provider reachable, 0=down,
    # debounced N-consecutive-fails exporter-side per PLS-9). Mark a
    # provider-unhealthy agent UNAVAILABLE so the resolver routes to a healthy
    # peer; recovery auto-restores via the rebalance sweep. Missing metric
    # (older exporter) => no samples => no behavior change (forward-compatible).
    prov_rx = re.compile(r'^shizuha_agent_provider_healthy\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    # Instantaneous exporter + Connect bad samples. Duration hysteresis (below)
    # decides which ones become sustained demotions for the availability map.
    raw_health_bad = dict(telemetry_health_bad or {})
    for m in prov_rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        if float(val) < 1.0:
            raw_health_bad.setdefault(email, 'provider_down')
    capacity_rx = re.compile(r'^shizuha_agent_capacity_unavailable\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    for m in capacity_rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        if float(val) >= 1.0:
            # Capacity limited stays a soft reason; still duration-gated.
            raw_health_bad[email] = 'capacity_limited'

    provider_down = []
    try:
        demote_after = int(os.environ.get('PULSE_HEALTH_DEMOTE_AFTER_SEC', '180'))
    except (TypeError, ValueError):
        demote_after = 180
    try:
        recover_after = int(os.environ.get('PULSE_HEALTH_RECOVER_AFTER_SEC', '180'))
    except (TypeError, ValueError):
        recover_after = 180
    try:
        _rc_hyst = WorkflowService._redis_client()
        _hyst_raw = _rc_hyst.get(_HEALTH_HYSTERESIS_KEY)
        _hyst_prev = json.loads(_hyst_raw) if _hyst_raw else {}
    except Exception:
        _rc_hyst = None
        _hyst_prev = {}
    if not isinstance(_hyst_prev, dict):
        _hyst_prev = {}
    sustained_health_down, _hyst_next = _apply_health_signal_hysteresis(
        raw_health_bad, _hyst_prev, time.time(), demote_after, recover_after,
    )
    try:
        if _rc_hyst is not None:
            ttl_h = int(os.environ.get('PULSE_AVAILABILITY_TTL', '600'))
            _rc_hyst.setex(
                _HEALTH_HYSTERESIS_KEY,
                max(ttl_h, demote_after, recover_after, 3600),
                json.dumps(_hyst_next),
            )
    except Exception as e:
        logger.warning(f"sweep_agent_availability: health hysteresis write failed: {e}")

    for email, reason in sustained_health_down.items():
        avail[email] = False
        unavailability_reasons[email] = reason or 'provider_down'
        provider_down.append(email)
    if raw_health_bad or sustained_health_down:
        logger.warning(
            "sweep_agent_availability: HEALTH BAD raw=%s sustained=%s "
            "(demote_after=%ss recover_after=%ss)",
            {em: raw_health_bad[em] for em in sorted(raw_health_bad)},
            {em: sustained_health_down[em] for em in sorted(sustained_health_down)},
            demote_after,
            recover_after,
        )
    if provider_down:
        logger.warning(
            f"sweep_agent_availability: SUSTAINED HEALTH DOWN -> "
            f"unavailable: {sorted(provider_down)}"
        )

    # PLAT-291: an operator toggle (status=disabled/paused via toggle_agent) is
    # NOT visible in process_up — a toggled-OFF agent whose container is still up
    # reports process_up=1, so the resolver kept it "available" and the rebalance
    # sweep assigned it work it can never pick up => assign->failover churn loop.
    # The exporter now emits shizuha_agent_enabled{agent=...} (1=active,
    # 0=disabled/paused, sourced from the live agents.json status the dashboards
    # already use). Mark a toggled-off agent UNAVAILABLE so both new-work routing
    # and the rebalance sweep route around it. No debounce: toggle state is an
    # explicit operator action, not a flaky signal. Missing metric (older
    # exporter) => no samples => no behavior change (forward-compatible, like the
    # wedge/provider folds above).
    enabled_rx = re.compile(r'^shizuha_agent_enabled\{[^}]*agent="([^"]+)"[^}]*\}\s+([0-9.]+)', re.M)
    enabled_snapshot_rx = re.compile(
        r'^shizuha_agent_enabled_snapshot_timestamp_seconds\s+([0-9.]+)',
        re.M,
    )
    enabled_snapshot_match = enabled_snapshot_rx.search(text)
    enabled_snapshot_at = (
        float(enabled_snapshot_match.group(1))
        if enabled_snapshot_match
        else None
    )
    lifecycle_rx = re.compile(
        r'^shizuha_agent_lifecycle_state\{[^}]*agent="([^"]+)"[^}]*state="([^"]+)"[^}]*\}\s+([0-9.]+)',
        re.M,
    )
    lifecycle_states = {}
    for m in lifecycle_rx.finditer(text):
        agent, state, val = m.group(1), m.group(2).strip().lower(), m.group(3)
        if float(val) >= 1.0 and state in {'enabled', 'hibernated', 'operator_stopped'}:
            lifecycle_states[f'{agent}@shizuha.com'.lower()] = state
    disabled = []
    hibernated = []
    disabled_all = []  # PLAT-350: every toggled-off agent (enabled<1) regardless of
                       # process state — the rebalance sweep fully drains these.
    enabled_ok = []    # PLS-208: agents sampled enabled=1 this scrape — used below to
                       # reactivate durable stopped rows once the agent is back on.
    hibernated_all = []
    stale_disabled_ignored = []
    for m in enabled_rx.finditer(text):
        agent, val = m.group(1), m.group(2)
        email = f"{agent}@shizuha.com".lower()
        lifecycle_state = lifecycle_states.get(email)
        # Typed lifecycle is authoritative even if the compatibility file was
        # not yet rewritten after the SQLite transaction.  A crash between the
        # two writes can otherwise expose operator_stopped with enabled=1 and
        # route work through the kill-switch.  Contradictory evidence therefore
        # fails closed; hibernated remains the only wakeable zero-resident state.
        if lifecycle_state == 'hibernated':
            hibernated_all.append(email)
            avail[email] = False
            if unavailability_reasons.get(email) in {None, 'offline', 'disabled'}:
                unavailability_reasons[email] = 'hibernated'
            continue
        if lifecycle_state == 'operator_stopped':
            disabled_all.append(email)
            if avail.get(email, True):
                disabled.append(email)
            avail[email] = False
            unavailability_reasons[email] = 'disabled'
            continue
        if float(val) >= 1.0:
            enabled_ok.append(email)
        if float(val) < 1.0:
            # The exporter caches its expensive full gather for up to two
            # minutes. Hive can start a native Deployment and its bridge can
            # heartbeat during that window while /metrics still contains the
            # pre-start enabled=0. Reject only a lifecycle sample that is
            # provably older than the direct heartbeat. A current/untimestamped
            # enabled=0 remains authoritative, preserving operator stops.
            if (
                enabled_snapshot_at is not None
                and not process_up.get(email, False)
                and hb_observed_at.get(email, 0) > enabled_snapshot_at
            ):
                stale_disabled_ignored.append(email)
                continue
            disabled_all.append(email)
            # PLAT-350 (kei/Codex P1): force a disabled agent UNAVAILABLE even with no
            # process_up sample. A stopped+disabled agent is absent from `avail`, and
            # _is_agent_available_for_assignment fails OPEN for absent emails, so the sweep
            # would classify it available and skip the full-drain path — exactly the
            # stopped/disabled in-flight work PLAT-350 must drain. Set False
            # unconditionally so it lands in `down` and gets drained.
            if avail.get(email, True):
                disabled.append(email)
            avail[email] = False
            unavailability_reasons[email] = 'disabled'
    if disabled:
        logger.warning(
            f"sweep_agent_availability: TOGGLED OFF (operator disabled/paused) -> "
            f"unavailable: {sorted(disabled)}"
        )
    wakeable = {
        email for email in hibernated_all
        if email not in set(provider_down)
        and email not in set(wedged)
        and unavailability_reasons.get(email) == 'hibernated'
    }
    if hibernated_all:
        logger.info(
            'sweep_agent_availability: HIBERNATED -> not live; wakeable=%s held=%s',
            sorted(wakeable),
            sorted(set(hibernated_all) - wakeable),
        )
    if stale_disabled_ignored:
        logger.info(
            "sweep_agent_availability: ignored lifecycle samples older than "
            f"live heartbeat: {sorted(stale_disabled_ignored)}"
        )

    ttl = int(os.environ.get('PULSE_AVAILABILITY_TTL', '600'))
    source_degraded = False
    try:
        _rc = WorkflowService._redis_client()
        # PLS-188: provider/token-pool telemetry is noisy around exhaustion. Keep
        # provider-down assignment availability sticky long enough for the
        # rebalance sweep to see it; otherwise a transient "healthy" scrape at the
        # rebalance minute leaves work pinned to a flapping agent.
        try:
            import time as _time
            _cooldown_s = int(os.environ.get('PULSE_PROVIDER_DOWN_COOLDOWN_SECONDS', '900'))
            # v2 entries are exclusively per-agent. Do not import the legacy
            # key, whose values may have been expanded from one pool failure to
            # every agent sharing the provider.
            _cooldown_key = 'pulse:agent_provider_down_until:v2'
            _prev_cooldown_raw = _rc.get(_cooldown_key)
            _prev_cooldown = json.loads(_prev_cooldown_raw) if _prev_cooldown_raw else {}
            _sticky_down, _next_cooldown = _apply_provider_down_cooldown(
                avail, provider_down, _prev_cooldown, _time.time(), _cooldown_s)
            if _next_cooldown:
                _rc.setex(_cooldown_key, max(ttl, _cooldown_s),
                          json.dumps(_next_cooldown))
            else:
                _rc.delete(_cooldown_key)
            for _email in _sticky_down:
                unavailability_reasons.setdefault(_email, 'provider_down')
            if _sticky_down:
                logger.warning(
                    "sweep_agent_availability: PROVIDER DOWN COOLDOWN -> "
                    f"unavailable: {sorted(_sticky_down)}")
        except Exception as e:
            logger.warning(f"sweep_agent_availability: provider-down cooldown failed: {e}")
        # PLAT-939: fail-STALE on a daemon-control-plane / exporter artifact. When the
        # daemon control API is down (502/refused), the agent-health exporter can't
        # confirm containers and emits process_up=0 EN MASSE for running agents,
        # collapsing the availability map into false offline failovers + mass-rerouting
        # live work. The fail-OPEN guards above don't catch this: the exporter is
        # reachable and DOES return samples, just wrong ones. Detect the signature -- a
        # large fraction of previously-available agents flipping to down PURELY on bare
        # process_up=0 with NO corroborating signal (wedge / provider-down / toggle) --
        # and PRESERVE their last-known-good availability (fail-stale) instead of failing
        # them all over. The real per-agent folds still mark their agents down normally;
        # only the uncorroborated mass-flip is suppressed. Publishes
        # pulse:availability_source_degraded so the org-health detector can route an
        # explicit signal to DevOps/PM. Flag-guarded (PULSE_AVAILABILITY_FAILSTALE_ENABLED).
        if os.environ.get('PULSE_AVAILABILITY_FAILSTALE_ENABLED', '1').strip().lower() not in {'0', 'false', 'off', 'no', ''}:
            try:
                corroborated_down = (
                    set(wedged) | set(provider_down) | set(disabled_all)
                    | set(hibernated_all)
                )
                bare_down = {e for e, up in avail.items() if not up and e not in corroborated_down}
                _prev_raw = _rc.get('pulse:agent_availability')
                _prev = json.loads(_prev_raw) if _prev_raw else {}
                prev_up = {e for e, up in _prev.items() if up}
                mass_flip = {e for e in bare_down if e in prev_up}
                flip_min = int(os.environ.get('PULSE_AVAILABILITY_MASS_FLIP_MIN', '5'))
                flip_frac = float(os.environ.get('PULSE_AVAILABILITY_MASS_FLIP_FRAC', '0.4'))
                if prev_up and len(mass_flip) >= flip_min and len(mass_flip) >= flip_frac * len(prev_up):
                    source_degraded = True
                    for e in mass_flip:
                        avail[e] = True  # fail-stale: preserve last-known-good UP
                        unavailability_reasons.pop(e, None)
                    logger.error(
                        "PLAT-939: availability source DEGRADED -- %d/%d previously-up agents "
                        "flipped to bare process_up=0 with no corroborating signal (daemon "
                        "control-plane likely down). Preserving last-known-good (fail-stale) "
                        "for: %s", len(mass_flip), len(prev_up), sorted(mass_flip))
                    _rc.setex('pulse:availability_source_degraded', ttl,
                              json.dumps({'flipped': sorted(mass_flip), 'prev_up': len(prev_up)}))
                else:
                    _rc.delete('pulse:availability_source_degraded')
            except Exception as e:
                logger.warning(f"sweep_agent_availability: PLAT-939 fail-stale guard error: {e}")
        # Hive hibernation is lifecycle SoT. Fold AFTER fail-stale so a mass
        # process_up=0 scrape cannot resurrect a deliberately parked seat.
        hibernated = []
        try:
            _prev_hib_raw = _rc.get('pulse:agent_hibernated')
            _prev_hibernated = json.loads(_prev_hib_raw) if _prev_hib_raw else []
        except Exception:
            _prev_hibernated = []
        if not isinstance(_prev_hibernated, list):
            _prev_hibernated = []
        _hive_hibernated, _hive_ok = _hive_hibernated_agents()
        if _hive_ok:
            hibernated = list(_hive_hibernated)
        else:
            hibernated = [
                str(e).strip().lower() for e in _prev_hibernated if str(e).strip()
            ]
        newly_hibernated = []
        _prev_hib_set = {
            str(e).strip().lower() for e in _prev_hibernated if str(e).strip()
        }
        _hibernated_expanded = []
        for _email in hibernated:
            aliases = WorkflowService._agent_email_aliases(_email) or {_email}
            _hibernated_expanded.extend(aliases)
            for alias in aliases:
                avail[alias] = False
                unavailability_reasons[alias] = 'hibernated'
            if _email not in _prev_hib_set:
                newly_hibernated.append(_email)
        hibernated = sorted(set(_hibernated_expanded))
        if hibernated:
            logger.warning(
                'sweep_agent_availability: HIBERNATED (Hive lifecycle) -> '
                'unavailable: %s', sorted(hibernated),
            )
        for _email, _up in avail.items():
            if _up:
                unavailability_reasons.pop(_email, None)
            else:
                unavailability_reasons.setdefault(_email, 'offline')
        # PLS-740: an env force-park (PULSE_INACTIVE/ABSENT/UNAVAILABLE_AGENT_EMAILS)
        # rejects a seat in _configured_agent_availability AFTER the health map has
        # already returned True, and previously wrote NOTHING to the reason surface —
        # so a deliberately-parked seat read exactly like a healthy routable one on
        # every instrument (the 2026-06-13 46-task silent starvation). Fold the
        # env-parked set into the reason surface with a DISTINCT 'force_parked'
        # reason so a reader can tell which source decided (env vs row vs health).
        # Legibility only — the parking verdict itself is unchanged. Placed after
        # the avail loop so an env-parked seat that is process_up=1 keeps its
        # force_parked reason instead of being popped as healthy.
        try:
            env_parked = WorkflowService._configured_email_set(
                'PULSE_INACTIVE_AGENT_EMAILS',
                'PULSE_ABSENT_AGENT_EMAILS',
                'PULSE_UNAVAILABLE_AGENT_EMAILS',
            )
            if env_parked:
                for _ep_email in list(unavailability_reasons):
                    if WorkflowService._agent_email_aliases(_ep_email) & env_parked:
                        unavailability_reasons[_ep_email] = 'force_parked'
                # Surface env-parked seats the sweep has no exporter sample for
                # (e.g. decommissioned) so they appear in the reason census too.
                for _ep in env_parked:
                    if '@' not in _ep or _ep.endswith('@'):
                        continue  # partial entry — cannot map to a concrete email
                    unavailability_reasons.setdefault(_ep, 'force_parked')
        except Exception as e:
            logger.warning(f"sweep_agent_availability: env force-park reason fold failed: {e}")
        # Operator directive 2026-09-16 (one truth, no dead path): the Hive
        # lifecycle-regex source and the _hive_hibernated_agents() fallback can
        # disagree — when the regex path finds nothing (agents.json format
        # drift), the fallback still publishes the hibernated set, but the
        # early `wakeable` comprehension ran on the empty regex-side list, so
        # the wakeable set was permanently empty and the resolver's typed
        # wakeable-seat admission (_is_agent_wakeable_for_new_assignment) was
        # dead code. Recompute wakeable from the AUTHORITATIVE hibernation set
        # (whichever source produced it) minus the same exclusions, so
        # assignment-to-hibernated actually works. Hive still gates the wake
        # itself on lease availability (lease_wake_available), so a wake into a
        # full pool is deferred, never booted-then-rehibernated.
        _wakeable_source = set(hibernated_all) or {
            str(e).strip().lower() for e in hibernated if str(e).strip()
        }
        wakeable = {
            _email for _email in _wakeable_source
            if _email not in set(provider_down)
            and _email not in set(wedged)
            and _email not in set(disabled_all)
            and unavailability_reasons.get(_email) in {None, 'hibernated', 'offline'}
        }
        _rc.setex('pulse:agent_availability', ttl, json.dumps(avail))
        _rc.setex('pulse:agent_unavailability_reason', ttl, json.dumps(unavailability_reasons))
        # Stamp when each agent FIRST went down and keep that stamp until it comes
        # back up. The heartbeat cannot answer "how long has this been down?" --
        # an agent that Hive restarts every few minutes refreshes it on every
        # start, which is how kai (provider_down, flapping ~10x/hour) held 54
        # items no drain could ever release (2026-07-27). Carry prior stamps
        # forward so a restart never resets the clock.
        try:
            _since_raw = _rc.get('pulse:agent_unavailable_since')
            _since = json.loads(_since_raw) if _since_raw else {}
        except Exception:
            _since = {}
        if not isinstance(_since, dict):
            _since = {}
        _now_ts = time.time()
        for _email, _up in avail.items():
            if _up:
                _since.pop(_email, None)
            else:
                _since.setdefault(_email, _now_ts)
        # Drop stamps for agents no longer reported at all, so the map cannot grow
        # without bound as agents are retired.
        _since = {e: t for e, t in _since.items() if e in avail}
        # TTL is deliberately generous relative to `ttl`: losing this map only
        # restores the pre-existing (unbounded-grace) behaviour, never a false drain.
        _rc.setex('pulse:agent_unavailable_since', max(ttl, 86400), json.dumps(_since))
        # PLAT-350: publish the operator-disabled set so the rebalance sweep fully
        # drains a disabled agent's in-flight work (in_progress/in_review/open-PR),
        # not just the unstarted set.
        _rc.setex('pulse:agent_disabled', ttl, json.dumps(sorted(set(disabled_all))))
        _rc.setex('pulse:agent_hibernated', ttl, json.dumps(sorted(set(hibernated))))
        _rc.setex('pulse:agent_wakeable', ttl, json.dumps(sorted(wakeable)))
        _rc.setex('pulse:agent_lifecycle_state', ttl, json.dumps(lifecycle_states))
        # Event-driven: a seat that just became hibernated should drain now,
        # not wait for the 10-minute rebalance backstop. Cap the fan-out.
        for _email in newly_hibernated[:8]:
            try:
                sweep_rebalance_pods.apply_async(
                    kwargs={'only_agent_email': _email},
                )
            except Exception:
                logger.warning(
                    'sweep_agent_availability: could not dispatch hibernate '
                    'drain for %s', _email, exc_info=True,
                )
    except Exception as e:
        logger.warning(f"sweep_agent_availability: redis write failed: {e}")
        return {'ok': False, 'error': str(e)}

    # PLAT-708: propagate SUSTAINED codex provider-exhaustion (weekly usage limit)
    # into the durable AgentAvailability table — the Redis map above is consulted
    # AFTER the DB row, so a stale operator active=True would otherwise keep routing
    # work to a usage-limited agent. Keyed on the codex-specific empty-turn streak
    # (not provider_healthy) to avoid falsely deactivating busy claude agents. Best
    # effort, fully guarded: a failure here never breaks the Redis publish above.
    auto_deactivated, auto_reactivated = [], []
    if os.environ.get('PULSE_AUTO_DEACTIVATE_ENABLED', '1').strip().lower() not in {'0', 'false', 'off', 'no', ''}:
        try:
            # PLS-741 / PLS-869 (JOIN_PAD contract): the producer emits
            # empty_turn_streak either as real MEASUREMENT (no placeholder label)
            # or as self-identifying JOIN_PAD (`placeholder="1"`). Padding must
            # NEVER feed the streak gate in either direction (deactivate OR
            # reactivate), so those series are filtered here — the only boundary
            # with wire access to the provenance label. With the field still
            # JOIN_PAD on :9888, `empty_streaks` stays empty and both directions
            # no-op (fail-closed).
            streak_rx = re.compile(
                r'^shizuha_agent_empty_turn_streak\{([^}]*)\}\s+([0-9.]+)', re.M)
            empty_streaks = {}
            for m in streak_rx.finditer(text):
                labels, raw = m.group(1), m.group(2)
                if 'placeholder="1"' in labels:
                    continue  # JOIN_PAD series — padding, not measurement.
                agent_m = re.search(r'agent="([^"]+)"', labels)
                if not agent_m:
                    continue
                em = f"{agent_m.group(1)}@shizuha.com".lower()
                empty_streaks[em] = max(empty_streaks.get(em, 0), int(float(raw)))
            threshold = int(os.environ.get('PULSE_AUTO_DEACTIVATE_STREAK', '3'))
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            # PLS-208: propagate operator-stopped/disabled agents (enabled=0) into
            # the durable AgentAvailability table so their unavailability survives
            # Redis TTL expiry / sweep death — the fail-open path is for
            # exporter-DOWN, never for a known-stopped agent. enabled=1 agents that
            # are available in the final map reactivate their non-operator rows.
            _enabled_up = {em for em in enabled_ok
                           if em not in set(disabled_all) and avail.get(em) is True}
            auto_deactivated, auto_reactivated = _reconcile_health_managed_availability(
                empty_streaks, threshold, now_iso,
                stopped=set(disabled_all), enabled_up=_enabled_up,
                provider_down=set(provider_down), wakeable=wakeable,
                # Only the provenance-filtered (non-JOIN_PAD) sample set may grant
                # streak recovery — see _reconcile_health_managed_availability.
                measured_streak_emails=set(empty_streaks),
                # PLS-734: the final live UP/DOWN map is recovery evidence for
                # FOREIGN (agent-name-stamped) deactivations on claude-bridge
                # seats, which never emit the codex empty-turn streak.
                health=avail)
            if auto_deactivated or auto_reactivated:
                logger.warning(
                    "sweep_agent_availability: AgentAvailability auto-managed — "
                    f"deactivated(provider-exhausted)={sorted(auto_deactivated)} "
                    f"reactivated(recovered)={sorted(auto_reactivated)}")
        except Exception as e:
            logger.warning(f"sweep_agent_availability: availability DB reconcile failed: {e}")

    down = sorted(e for e, up in avail.items() if not up)
    if down:
        logger.info(f"sweep_agent_availability: {len(avail)} agents; DOWN={down}")
    return {
        'ok': True, 'count': len(avail), 'down': down,
        'wedged': sorted(wedged), 'provider_down': sorted(provider_down),
        'disabled': sorted(disabled),
        'hibernated': sorted(set(hibernated) | set(hibernated_all)),
        'wakeable': sorted(wakeable),
        'external_live': sorted(set(external_live)),
        'stale_disabled_ignored': sorted(set(stale_disabled_ignored)),
        'auto_deactivated': sorted(auto_deactivated),
        'auto_reactivated': sorted(auto_reactivated),
        'source_degraded': source_degraded,
    }


def _rebalance_pulse_api_health_gate():
    """Return (ok, skipped_reason) for the Pulse HTTP/API health gate.

    PLAT-916: the rebalance sweep measures queues and agent state through Pulse's
    API-backed model. During an API outage, missing/limited task movement is
    unmeasurable, not evidence of true idleness. Skip the entire cycle rather than
    redistributing work from stale/incomplete signals.

    PLAT-954: upgraded to probe the DATA-PLANE health endpoint (/api/health/data/)
    rather than the process-only /api/health/. A Django process that is up but
    whose DB connection is severed (Postgres failover, network partition) returns
    200 from /api/health/ while all ORM reads fail — the sweep would then act on
    stale/empty queue data and misroute tasks. The data-plane endpoint performs a
    cheap Item.objects.exists() probe and returns 503 + data_plane=error on DB
    failure, which this gate treats as degraded and suppresses the sweep.

    Resolution: PULSE_API_DATA_HEALTH_URL (default: derived from
    PULSE_API_HEALTH_URL by replacing /api/health/ with /api/health/data/).
    Set PULSE_API_DATA_HEALTH_URL=disabled to opt out (falls back to process-only
    check for backwards compatibility with deployments that haven't rolled the new
    endpoint yet).
    """
    import os
    base_url = os.environ.get('PULSE_API_HEALTH_URL', 'http://127.0.0.1:8002/api/health/').strip()
    if not base_url or base_url.lower() in {'disabled', 'off', 'none'}:
        return True, None

    # PLAT-954: derive the data-plane URL from the base URL unless explicitly set.
    data_url_env = os.environ.get('PULSE_API_DATA_HEALTH_URL', '').strip()
    if data_url_env and data_url_env.lower() not in {'disabled', 'off', 'none'}:
        url = data_url_env
    elif data_url_env.lower() in {'disabled', 'off', 'none'}:
        # Operator explicitly opted out of data-plane check — fall back to process check.
        url = base_url
    else:
        # Derive: replace trailing /api/health/ with /api/health/data/
        url = base_url.rstrip('/')
        if url.endswith('/api/health'):
            url = url + '/data/'
        elif url.endswith('/api/health/'):
            url = url + 'data/'
        else:
            # Unexpected format — append data/ suffix best-effort.
            url = url.rstrip('/') + '/data/'

    try:
        _probe = requests.get(url, timeout=5)
        status = int(getattr(_probe, 'status_code', 200) or 200)
        if status < 200 or status >= 500:
            logger.warning(
                "sweep_rebalance_pods: Pulse API data-plane health check returned %s "
                "— aborting sweep because agent idleness is unmeasurable (skipped:api_degraded)",
                status,
            )
            return False, 'api_degraded'
        # PLAT-954: also check the data_plane field in the response body.
        try:
            import json as _json
            _parsed = _json.loads(_probe.text)
            if _parsed.get('data_plane') not in (None, 'ok'):
                logger.warning(
                    "sweep_rebalance_pods: Pulse API data-plane health check returned "
                    "data_plane=%r — aborting sweep (skipped:api_degraded)",
                    _parsed.get('data_plane'),
                )
                return False, 'api_degraded'
        except Exception:
            # Non-JSON or unreadable body from process-only endpoint — treat as ok
            # (backwards-compat: the old /api/health/ returns plain JSON without data_plane).
            pass
        return True, None
    except Exception as _exc:
        logger.warning(
            "sweep_rebalance_pods: Pulse API health check unreachable (%r) "
            "— aborting sweep because agent idleness is unmeasurable",
            _exc,
        )
        return False, 'api_unreachable'


def _is_cos_pinned(item):
    """PLAT-979: CoS-pinned tasks must NEVER be rebalanced/drained away — they are
    operator-directed standing ownership (e.g. CTX-111 Cluster Lease Board, which the
    PLAT-236 sweep kept yanking from the CoS into engineering). Pinned iff a
    `cos-pinned` label is present, OR team=admin-ops AND the assignee is the CoS.
    Generic so any future CoS-owned anchor is protected by the same rule."""
    from .services.workflow_service import WorkflowService
    labels = getattr(item, 'labels', None) or []
    if isinstance(labels, dict):
        labels = list(labels.keys())
    elif isinstance(labels, str):
        labels = [labels]
    if any(str(l).strip().lower() == 'cos-pinned' for l in labels):
        return True
    grp = (getattr(item, 'assignment_group', '') or '').strip().lower()
    ae = (getattr(item, 'assignee_email', '') or '').strip().lower()
    cos = (getattr(WorkflowService, '_COS_EMAIL', '') or '').strip().lower()
    return grp == 'admin-ops' and bool(cos) and ae == cos


def _decision_pin_metadata(item):
    md = getattr(item, 'metadata', None) or {}
    if not isinstance(md, dict):
        return {}
    da = md.get('direct_assignment')
    return da if isinstance(da, dict) else {}


def _is_decision_pinned(item):
    """PLS-233: a task explicitly direct-assigned WITH A REASON to a decision-owner
    must NOT be re-resolved back to the symptom-team pool by the rebalance sweep.

    The pin is honored only while the item's current assignee still equals the pinned
    email. A later real reassignment or status transition clears/auto-releases it.
    """
    da = _decision_pin_metadata(item)
    pinned = (da.get('assignee_email') or '').strip().lower()
    if not pinned:
        return False
    cur = (getattr(item, 'assignee_email', '') or '').strip().lower()
    return bool(cur) and cur == pinned


def _decision_pinned_team(item):
    if not _is_decision_pinned(item):
        return ''
    return (_decision_pin_metadata(item).get('pinned_team') or '').strip().lower()


def _repin_decision_assignment(item, assignee_email):
    """Keep a decision pin active after failover within the pinned decision team.

    PLS-233: the pin predicate intentionally requires metadata.assignee_email to
    match the current assignee so normal explicit reassignments auto-release the
    pin. Availability failover within the same decision team is not such a
    release; update the metadata in the same row-lock transaction as the
    assignee change so the next sweep still treats the item as decision-pinned
    instead of bouncing it back to the symptom team.
    """
    md = dict(getattr(item, 'metadata', None) or {})
    da = md.get('direct_assignment')
    if not isinstance(da, dict):
        return False
    da = dict(da)
    da['assignee_email'] = (assignee_email or '').strip().lower()
    da['at'] = timezone.now().isoformat()
    md['direct_assignment'] = da
    item.metadata = md
    return True


def _resolve_decision_pin_failover_target(item, down_email):
    pinned_team = _decision_pinned_team(item)
    if not pinned_team:
        return None, None
    from .services.workflow_service import WorkflowService
    return WorkflowService._resolve_team_assignee(
        pinned_team,
        exclude_email=down_email,
        enforce_cap=False,
        item=item,
    )


def _create_system_comment_deduped(item, content, window_minutes=30):
    """PLS-198 (F5): backstop dedupe for sweep-generated system comments.

    Skip posting when an identical system@pulse comment already exists on the
    item within the window. The primary guards are the single-flight lock (F2)
    and the per-item ownership recheck (F4); this backstop keeps a comment storm
    impossible even when both fail open (e.g. Redis outage during stacked beat
    runs — the BKST-1 2000+-comment incident). Returns the created Comment or
    None when deduped/failed; never raises (comments are best-effort)."""
    from datetime import timedelta

    from .models import Comment
    try:
        cutoff = timezone.now() - timedelta(minutes=window_minutes)
        if Comment.objects.filter(item=item, author_email='system@pulse',
                                  content=content,
                                  created_at__gte=cutoff).exists():
            return None
        return Comment.objects.create(
            item=item, author_id=0, author_email='system@pulse', content=content)
    except Exception:
        return None


_DOWNSTREAM_PIPELINE_LANES = frozenset({'qa', 'merge'})


def _authoritative_pipeline_lane(item):
    """Return the workflow-owned lane for WIP recovery.

    ``in_review`` is shared by code Review and post-merge operational
    verification.  A current linked ``(repo, PR, head SHA)`` tuple is explicit
    code-review provenance and repairs stale prior-stage routing to Review.
    Without that tuple, the current assignment group is typed routing state,
    not a value that can be reconstructed from the status slug.  The dedicated
    QA and merge statuses remain unambiguous and intentionally repair a stale
    prior-stage group.

    CON-8745: a forward pipeline handoff recorded in the team field is
    authoritative over the status-derived lane.  ``qa`` and ``merge`` are the
    downstream lanes that legitimately receive ``in_review`` items (QA
    verification, MERGE-COMMIT); their team fields are typed handoff state,
    not stale upstream routing, so the linked-PR provenance heuristic must not
    reclaim them.  A merge-pending item always carries a linked unmerged PR,
    which would otherwise make the team-field branch unreachable for exactly
    the items that were explicitly handed forward (PLAT-8519: nine capacity
    pulls in one day after the formal APPROVED).  Handoffs only ever go
    forward in the pipeline; the PR-presence heuristic exists to catch stale
    upstream team fields (engineering/devops), and forward-lane team fields
    are not that.
    """
    from .services.workflow_service import WorkflowService

    status = WorkflowService._normalize_status_slug(item.status)
    current_group = WorkflowService._canonical_team_name(
        item.assignment_group,
    )
    if status == 'in_review':
        if current_group in _DOWNSTREAM_PIPELINE_LANES:
            return current_group
        has_current_code_review_target = any(
            str(pr.get('repo') or '').strip()
            and pr.get('number') not in (None, '')
            and str(pr.get('head_sha') or pr.get('headRefOid') or '').strip()
            and str(pr.get('status') or '').strip().lower()
            not in {'closed', 'merged'}
            for pr in WorkflowService._linked_pr_entries(item)
        )
        if has_current_code_review_target:
            return 'review'
        if current_group:
            return current_group
    return {
        'in_review': 'review',
        'in_qa': 'qa',
        'awaiting_merge': 'merge',
    }.get(status, current_group)


def _reclaim_wip_overload():
    """Workflow-safe, deterministic overload recovery for PLS-509.

    One recent/urgent active item is preserved up to the roster's active cap.
    Excess literal ``in_progress`` work is moved through a legal todo-category
    transition; excess lane work (review/QA/merge) keeps its status and returns
    to the staffed team queue. Ready overflow is then unassigned by inherited
    blocker priority, closest-to-done stage, and FIFO.
    Blocked/scheduled/backlog ownership is untouched and nothing is closed.
    """
    import os
    from datetime import timedelta
    from django.db import transaction
    from django.utils import timezone
    from .models import Comment, Item, ItemActivity, Status, TeamRoster, Workflow, WorkflowTransition
    from .services.queue_priority import (
        active_blocked_descendant_exists,
        effective_blocker_priority_rank,
        queue_stage_rank_value,
    )
    from .services.operator_wait import exclude_operator_wait_items
    from .services.provider_wait import mark_sweep_seen
    from .models import ProviderWaitMarker
    from .services.routing_authority import (
        clear_assignment_to_group_queue,
        reroute_assignment_to_group_queue,
    )
    from .services.workflow_service import WorkflowService
    from .user_service import user_service

    priority = {'urgent': 4, 'high': 3, 'medium': 2, 'normal': 2, 'low': 1}
    ttl_minutes = int(os.environ.get('PULSE_ACTIVE_ACTIVITY_TTL_MINUTES', '30'))
    cutoff = timezone.now() - timedelta(minutes=max(1, ttl_minutes))
    # PLAT-390 two-tier: capacity reclaim reads the review pipeline as READY
    # (in-flight, not hands-on) so a review-lane item is reclaimed once on the
    # ready dimension, never twice (active + ready). _slot_statuses('active')
    # keeps the pipeline for focus-guard seat protection; capacity paths use
    # this capacity view.
    active_statuses = (
        WorkflowService._slot_statuses('active')
        - WorkflowService._REVIEW_PIPELINE_STATUSES
    )
    ready_statuses = WorkflowService._slot_statuses('ready')
    reclaimed_active = queued_active = queued_ready = 0

    # First repair stale prior-stage ownership independently of overload. A QA
    # item owned by a Review seat is wrong even when that reviewer has only one
    # active item; leaving it assigned would hide it from QA's staffed queue.
    lane_by_status = {
        'in_review': 'review',
        'in_qa': 'qa',
        'awaiting_merge': 'merge',
    }
    for stale in Item.objects.filter(
        mode='task', resolution__isnull=True,
        status__in=tuple(lane_by_status),
    ).exclude(assignee_email__isnull=True).exclude(assignee_email=''):
        with transaction.atomic():
            fresh = Item.objects.select_for_update().get(pk=stale.pk)
            if fresh.status not in lane_by_status or not fresh.assignee_email:
                continue
            # PLAT-7392: a PLS-233 decision pin (direct assignment WITH a
            # recorded reason) is a deliberate routing decision, not stale
            # prior-stage ownership. The rebalance sweep (tasks.py) and
            # availability failover already honor the pin; this repair sweep
            # was the one path missing the check, so fresh capability-matched
            # direct assignments on in_review/in_qa/awaiting_merge items were
            # swept within minutes and the Critical-unassigned override
            # re-assigned an unresolvable identity — the strand-and-repeat
            # loop rei documented on the review lane. The pin auto-releases on
            # real reassignment/status transition, so the exemption cannot
            # accumulate stale pins.
            if _is_decision_pinned(fresh):
                continue
            # PLS-581: the unlocked scan is only a candidate list.  Derive the
            # lane and every owner/team eligibility decision from the locked
            # row so a concurrent Review -> QA correction cannot be overwritten
            # while both stages share the ``in_review`` status slug.
            lane_team = _authoritative_pipeline_lane(fresh)
            metadata = fresh.metadata or {}
            if (
                fresh.source == 'focused-qa-automation'
                and fresh.status == 'in_review'
                and metadata.get('focused_qa_routing_mode')
                == 'origin-team-fallback'
            ):
                recorded_group = WorkflowService._canonical_team_name(
                    metadata.get('focused_qa_assignment_group') or '',
                )
                recorded_tiers, _ = WorkflowService._team_roster_entry_for_item(
                    recorded_group,
                    fresh,
                )
                if recorded_group and recorded_tiers:
                    # A tenant without staffed QA/Review lanes may explicitly
                    # keep focused verification with its originating team.
                    lane_team = recorded_group
            current_team = WorkflowService._canonical_team_name(
                fresh.assignment_group,
            )
            owner = (fresh.assignee_email or '').strip().lower()
            lane_tiers, _ = WorkflowService._team_roster_entry_for_item(
                lane_team,
                fresh,
            )
            exact_lane_members = {
                email.strip().lower()
                for email in WorkflowService._flat_roster(lane_tiers or [])
            }
            # Rosters are authored as @shizuha.com. Assignment persists the
            # @agents.shizuha.io alias. An exact string compare treated every
            # live reviewer as a prior-stage stranger and returned their
            # in_review items to the unassigned queue (Reika, 2026-09-24).
            owner_is_lane_member = any(
                WorkflowService._emails_same_agent_identity(owner, member)
                for member in exact_lane_members
            )
            if current_team == lane_team and owner_is_lane_member:
                continue
            # PLAT-8821: no actionable work left — a merged-PR/no-open-PR item
            # must not have its stale-lane owner re-routed (the PLAT-8787 hop
            # class); its own workflow owns the next step.
            if not WorkflowService._item_failover_actionable(fresh):
                continue
            reroute_assignment_to_group_queue(
                fresh, lane_team, reason='wip_overload_reclaim', save=True,
            )
        queued_active += 1
        _create_system_comment_deduped(
            fresh,
            f'Removed stale prior-stage owner and returned this item to the '
            f'**{lane_team}** lane queue (PLS-509).',
        )

    # One person may sit on multiple team rosters. Capacity is per agent, not
    # per membership: use the strictest positive cap across their roles and
    # reclaim once, across every assignment group they currently own.
    # PLS-922: capacity is ALSO per organization. The policy key is the exact
    # (organization_id, email) pair — never a bare email — so a member on both
    # the global `shizuha::devops` roster (ready_wip_cap=5) and an exact-org
    # roster (e.g. `shizuha-trading::devops` with ready_wip_cap=0/disabled) is
    # evaluated under EACH org's own cap. The base query below is org-scoped to
    # match: the global roster's cap must never reclaim a different org's items
    # (the false pull->return oscillation behind the capacity-starve page).
    policies = {}
    for roster in TeamRoster.objects.filter(is_active=True, wip_enforced=True):
        org_id = roster.organization_id
        if org_id is None:
            # Legacy rows may predate the org-id column; resolve from slug.
            org_id = {
                'shizuha': 1,
                'shizuha-digital': 2,
                'shizuha-trading': 3,
                'personal-3': 4,
            }.get((roster.organization_slug or '').strip().lower())
        for email in WorkflowService._flat_roster(roster.tiers or []):
            key = (org_id, email.strip().lower())
            current = policies.get(key)
            active_cap = int(roster.active_wip_cap or 0)
            ready_cap = int(roster.ready_wip_cap or 0)
            if current is None:
                policies[key] = (active_cap, ready_cap)
            else:
                policies[key] = (
                    min(v for v in (current[0], active_cap) if v > 0)
                    if current[0] > 0 and active_cap > 0 else max(current[0], active_cap),
                    min(v for v in (current[1], ready_cap) if v > 0)
                    if current[1] > 0 and ready_cap > 0 else max(current[1], ready_cap),
                )

    for (org_id, email), (active_cap, ready_cap) in policies.items():
        aliases = WorkflowService._agent_email_aliases(email)
        base = exclude_operator_wait_items(Item.objects.filter(
            mode='task', resolution__isnull=True,
            assignee_email__in=aliases,
        ).exclude(WorkflowService._umbrella_item_q()))
        # PLAT-10083: provider_wait items are legible waiting state — excluded
        # from capacity reclaim (never swept around), and the sweep's
        # observation of their markers is recorded for the manager lane.
        provider_wait_items = list(
            ProviderWaitMarker.objects.filter(
                state='active', item__assignee_email__in=aliases,
            ).values_list('item_id', flat=True)
        )
        if provider_wait_items:
            base = base.exclude(id__in=provider_wait_items)
            mark_sweep_seen([Item(id=i) for i in provider_wait_items])
        if org_id is not None:
            # PLS-922: reclaim is org-scoped. The global shizuha::devops cap
            # applies only to shizuha-org items; an exact-org roster with
            # ready_wip_cap=0 (disabled) leaves that org's ready items alone.
            base = base.filter(project__org_id=org_id)

        active = list(
            base.filter(status__in=active_statuses).annotate(
                _queue_is_blocker_root=active_blocked_descendant_exists(),
                _queue_effective_priority_rank=effective_blocker_priority_rank(),
            )
        )
        logger.debug(
            'PLS-509 recovery scan email=%s active_cap=%s active=%s',
            email, active_cap,
            [it.item_key or it.pk for it in active],
        )
        if active_cap > 0 and len(active) > active_cap:
            recent_ids = set(ItemActivity.objects.filter(
                item_id__in=[it.pk for it in active],
                user_email__in=aliases,
                created_at__gte=cutoff,
                action__in=('status_changed', 'commented', 'completed'),
            ).values_list('item_id', flat=True))
            # Blocker roots are non-sheddable WIP: removing/demoting them can
            # deadlock every descendant. They inherit descendant urgency and
            # remain concrete even when that temporarily exceeds the nominal
            # cap; reclaim unrelated work around them instead.
            active.sort(key=lambda it: (
                0 if it._queue_is_blocker_root else 1,
                it._queue_effective_priority_rank,
                0 if it.pk in recent_ids else 1,
                -priority.get((it.priority or '').lower(), 0),
                it.updated_at,
                it.pk,
            ))
            protected = [it for it in active if it._queue_is_blocker_root]
            ordinary = [it for it in active if not it._queue_is_blocker_root]
            keep_ordinary = max(0, active_cap - len(protected))
            for stale in ordinary[keep_ordinary:]:
                # PLS-1433: a deliberate direct override (direct_assignment
                # metadata) on a seat that is live right now is a routing
                # decision, not sheddable slack — recovery must not unpin it.
                # Over-cap shed still applies to group-routed ordinary work.
                if (stale.metadata or {}).get('direct_assignment') and \
                        WorkflowService._is_agent_available_for_assignment(
                            (stale.assignee_email or '').strip().lower(),
                            require_live_evidence=True,
                        ):
                    continue
                if (stale.status or '').strip().lower() == 'in_progress':
                    wf = Workflow.resolve(stale.workflow_name) if stale.workflow_name else None
                    transition = None
                    if wf:
                        todo_slugs = set(Status.objects.filter(category='todo').values_list('slug', flat=True))
                        transition = (
                            WorkflowTransition.objects.filter(
                                workflow=wf,
                                from_status=stale.status,
                                to_status__in=todo_slugs,
                            ).order_by('position').first()
                        )
                    if transition:
                        try:
                            # Recovery is a batch repair, so each workflow stop
                            # must be all-or-nothing.  Ordinary transitions are
                            # intentionally not globally atomic because some
                            # validation paths persist a safe reroute before
                            # rejecting the caller; here, however, a failing
                            # post-function must not leave the status changed
                            # and abort every later agent's recovery.
                            with transaction.atomic():
                                WorkflowService.execute_transition(
                                    stale,
                                    transition.to_status,
                                    user=None,
                                    transition_id=str(transition.id),
                                )
                            reclaimed_active += 1
                            _create_system_comment_deduped(
                                stale,
                                'Stopped stale progress through the workflow-safe '
                                f'`{transition.name}` transition during PLS-509 WIP recovery.',
                            )
                            continue
                        except Exception:
                            logger.warning(
                                'PLS-509 could not stop stale progress for %s via %s',
                                stale.item_key or stale.pk, transition.name,
                                exc_info=True,
                            )
                # Review/QA/merge states represent a lane queue even though
                # their Status category is in_progress.  Preserve the lane
                # state and release only the concrete seat.
                with transaction.atomic():
                    fresh = Item.objects.select_for_update().get(pk=stale.pk)
                    if fresh.status not in active_statuses:
                        continue
                    lane_team = _authoritative_pipeline_lane(fresh)
                    current_team = WorkflowService._canonical_team_name(fresh.assignment_group)
                    if lane_team == current_team:
                        clear_assignment_to_group_queue(
                            fresh, reason='wip_overload_reclaim', save=True,
                        )
                    else:
                        reroute_assignment_to_group_queue(
                            fresh, lane_team,
                            reason='wip_overload_reclaim', save=True,
                        )
                queued_active += 1
                _create_system_comment_deduped(
                    fresh,
                    f'Returned to the **{lane_team}** team queue: active WIP cap '
                    f'is {active_cap} (PLS-509).',
                )

        ready = list(
            base.filter(status__in=ready_statuses)
            .annotate(
                _queue_is_blocker_root=active_blocked_descendant_exists(),
                _queue_effective_priority_rank=effective_blocker_priority_rank(),
            )
        )
        ready.sort(key=lambda it: (
            0 if it._queue_is_blocker_root else 1,
            it._queue_effective_priority_rank,
            queue_stage_rank_value(it.status),
            it.created_at,
            it.pk,
        ))
        protected_ready = [it for it in ready if it._queue_is_blocker_root]
        ordinary_ready = [it for it in ready if not it._queue_is_blocker_root]
        keep_ordinary_ready = max(0, ready_cap - len(protected_ready))
        ready_overflow = (
            ordinary_ready[keep_ordinary_ready:] if ready_cap > 0 else []
        )
        for excess in ready_overflow:
            with transaction.atomic():
                fresh = Item.objects.select_for_update().get(pk=excess.pk)
                if fresh.status not in ready_statuses:
                    continue
                clear_assignment_to_group_queue(
                    fresh, reason='wip_ready_overflow', save=True,
                )
            queued_ready += 1
            _create_system_comment_deduped(
                fresh,
                f'Returned to the **{fresh.assignment_group}** team queue by effective priority/stage/FIFO order: '
                f'assigned-ready cap is {ready_cap} (PLS-509).',
            )
    return {
        'active_stopped': reclaimed_active,
        'active_queued': queued_active,
        'ready_queued': queued_ready,
    }


def _auto_failover_recently_recorded(item, window_hours=None):
    """PLS-235: do not fail over the same item repeatedly within a cooldown.

    PLS-198 dedupes identical system comments; PLS-235 is broader: any recent
    auto-failover on the item means the outage/failover state is already visible
    and another sweep should not ping-pong the concrete assignee or post a new
    variant comment for the same item until the cooldown expires.
    """
    from datetime import timedelta

    from .models import Comment
    try:
        hours = float(
            window_hours
            if window_hours is not None
            else os.environ.get('PULSE_FAILOVER_COOLDOWN_HOURS', '4')
        )
        if hours <= 0:
            return False
        cutoff = timezone.now() - timedelta(hours=hours)
        return Comment.objects.filter(
            item=item,
            author_email='system@pulse',
            content__startswith='Auto-failover',
            created_at__gte=cutoff,
        ).exists()
    except Exception:
        return False


_ROSTER_RECONCILIATION_ALERT_SOURCE = 'pulse.roster_reconciliation'
_ROSTER_RECONCILIATION_ALERT_SOURCE_ID = 'roster-membership-reconciliation-failure'


def _record_roster_reconciliation_health(*, state, reconciled=0, cap_hit=False, error=''):
    """Expose one authoritative PLS-408 run outcome.

    The liveness heartbeat is a SUCCESS heartbeat: a failed reconciliation must
    not refresh it and make the mechanism look healthy.  The cache payloads keep
    the latest attempt plus the latest success/failure independently observable
    through ``/statusz`` even when the surrounding multi-purpose sweep continues.
    """
    from django.core.cache import cache
    from . import liveness

    payload = {
        'state': str(state),
        'reconciled': int(reconciled or 0),
        'cap_hit': bool(cap_hit),
        'error': str(error or '')[:500],
        'at': timezone.now().isoformat(),
        'invariant': (
            'one item-scoped roster authority selects and fences the canonical '
            'owner; linked-blocked hard-down ownership clears to its group queue '
            'without a replacement lease'
        ),
    }
    try:
        if payload['state'] == 'success':
            payload['last_success_ts'] = liveness.record_heartbeat(
                liveness.ROSTER_RECONCILIATION,
            )
        cache.set('pulse:roster_reconciliation:last', payload, timeout=None)
        cache.set(
            f"pulse:roster_reconciliation:last_{payload['state']}",
            payload,
            timeout=None,
        )
    except Exception:
        logger.exception('roster-reconciliation health artifact write failed')
    logger.warning(
        'metric:roster_reconciliation_health state=%s reconciled=%d cap_hit=%s error=%s',
        payload['state'], payload['reconciled'], payload['cap_hit'], payload['error'],
    )
    return payload


def _notify_roster_reconciliation_owner(payload):
    """Create one deduplicated Engineering alert and trigger its owner DM path."""
    try:
        from .services.deduplication import AlertDeduplicationService

        alert, is_new = AlertDeduplicationService.submit_alert({
            'item_type': 'alert.error',
            'title': 'Pulse roster-membership reconciliation failed',
            'description': (
                'The PLS-408/PLS-524 roster-membership reconciliation raised and '
                'did not emit a success heartbeat. The surrounding rebalance sweep '
                'continued, but this mechanism is unhealthy until a later successful '
                'run resolves this alert.\n\n'
                f"- error: `{payload.get('error') or 'unknown'}`\n"
                f"- reconciled before failure: `{payload.get('reconciled', 0)}`\n"
                '- owner: `engineering`\n'
                '- liveness: `/statusz` → '
                '`roster_reconciliation_last_success_ts` and '
                '`roster_reconciliation_last_failure`'
            ),
            'source': _ROSTER_RECONCILIATION_ALERT_SOURCE,
            'source_id': _ROSTER_RECONCILIATION_ALERT_SOURCE_ID,
            'severity': 'error',
            'priority': 'urgent',
            'assignment_group': 'engineering',
            'labels': {
                'mechanism': 'roster_reconciliation',
                'owner_group': 'engineering',
            },
            'payload': {
                'state': payload.get('state'),
                'error': payload.get('error'),
                'failed_at': payload.get('at'),
            },
        })
        if is_new:
            send_alert_notification.delay(alert.id)
        return alert
    except Exception:
        logger.exception('roster-reconciliation owner alert failed')
        return None


def _resolve_roster_reconciliation_failure_alert():
    """Close the active failure incident after the next successful run."""
    try:
        from .models import Item

        for alert in Item.objects.filter(
            mode='alert',
            source=_ROSTER_RECONCILIATION_ALERT_SOURCE,
            source_id=_ROSTER_RECONCILIATION_ALERT_SOURCE_ID,
            status__in=['firing', 'acknowledged'],
        ):
            alert.resolve(resolved_by='sweep_rebalance_pods:roster_reconciliation_recovered')
    except Exception:
        logger.exception('roster-reconciliation recovery alert resolve failed')


def _assign_critical_unassigned() -> int:
    """Force-assign urgent/high items that have no concrete owner, overriding
    normal assignment exclusions (blocked, scheduled, etc.) and WIP caps.

    A stranded urgent task with no assignee is worse than temporarily exceeding
    WIP caps. This runs before the main rebalance loop so critical work lands
    on a warm seat immediately rather than waiting for a normal assignment cycle.

    Returns the number of items successfully force-assigned.
    """
    from django.db import transaction as _db_transaction
    from .models import Item, Comment as _ItemComment
    from .services.workflow_service import WorkflowService as _WS
    import os as _os

    # Terminal / settled statuses — never force-assign. Includes
    # resolved_elsewhere (PLS-1382 / PLAT-9072): deliberately NON-terminal for
    # blocker release (PLS-719) but never actionable seat demand — the override
    # used to treat it as a "normally-excluded" status and force-assign it.
    _TERMINAL = {'completed', 'done', 'closed', 'cancelled', 'canceled',
                 'rejected', 'resolved', 'expired', 'deferred', 'duplicate',
                 'merged', 'wont_fix', 'failed', 'resolved_elsewhere'}

    # Find urgent/high unassigned items in statuses the normal sweep skips.
    # Items in open/todo/in_progress/in_review are normally handled by the
    # regular queue-drain and rebalance sweep — the override only targets
    # statuses that are normally excluded: blocked, scheduled, backlog,
    # needs_triage, found, etc. This prevents double-assignment and test
    # failures.
    #
    # EXCEPTION: an urgent/high item in a normally-handled status that is a
    # BLOCKER-ROOT (it has active blocked descendants, i.e. it is a true
    # "ultimate blocker") must still get the critical override. The regular
    # queue-drain is cap-enforced (`enforce_cap=True` default) and fails-closed
    # when the whole live fleet is at WIP capacity — under that condition it
    # resolves (None, None) for EVERY open/todo/in_progress/in_review item, so
    # a stranded blocker-root has no assignment path at all and sits unassigned
    # for weeks while every descendant waits on it. Force-assigning just the
    # blocker-roots is safe: PLS-509 recovery treats blocker-roots as
    # non-sheddable WIP (it never reclaims them), so there is no assign/reclaim
    # churn, and unblocking them drains every blocked descendant downstream.
    from .services.queue_priority import (
        active_blocked_descendant_exists as _active_blocked_descendant_exists,
        active_upstream_blocker_exists as _active_upstream_blocker_exists,
    )

    _NORMALLY_HANDLED = {'open', 'todo', 'in_progress', 'in_review', 'in_qa',
                          'awaiting_merge', 'applied', 'needs_info'}
    _base_qs = (
        Item.objects.filter(
            resolution__isnull=True,
            assignee_email__isnull=True,
            assignee_id__isnull=True,
            priority__in=('urgent', 'high'),
            mode='task',
        )
        .exclude(project__isnull=True)
        .exclude(status='waiting_external')
        .exclude(status__in=_TERMINAL)
    )
    # Historical scope: statuses the regular sweep ignores entirely.
    _excluded_status_qs = _base_qs.exclude(status__in=_NORMALLY_HANDLED)
    # Normally-handled statuses that are stranded blocker-roots (see above).
    _blocker_root_normal_qs = (
        _base_qs
        .filter(status__in=_NORMALLY_HANDLED)
        .annotate(_is_blocker_root=_active_blocked_descendant_exists())
        .filter(_is_blocker_root=True)
    )
    # PLAT-5261: drop items that STILL have an active upstream blocker at the
    # SQL level, BEFORE the [:50] candidate cap. The assignment loop guard
    # skips them anyway (`_WS._has_active_blocker(fresh) -> continue`), so any
    # such item occupying one of the 50 slots is a slot wasted. In practice the
    # oldest critical unassigned backlog is dominated by stale `blocked` items
    # that still carry a live inbound block link — without this filter those 50
    # slots are consumed by items that can never be assigned and the newer
    # actionable blocker-roots starve outside the window (verified live:
    # 0/50 assignable in the current window while 82 stranded blocker-roots
    # waited outside it).
    candidates = list(
        (_excluded_status_qs | _blocker_root_normal_qs)
        .annotate(_has_upstream_blocker=_active_upstream_blocker_exists())
        .filter(_has_upstream_blocker=False)
        .order_by('-priority', 'created_at')[:50]
    )

    if not candidates:
        return 0

    assigned = 0
    for item in candidates:
        # PLS-1365: resolved_elsewhere whose linked parent is already terminal
        # is effect-terminal for assignment — never force-pull it.
        if _WS._is_assignment_effect_terminal(item):
            continue
        # PLS-1365: rate-limit force-pulls absent a status change.
        if _WS._force_pull_blocked_by_boundary(item):
            continue
        # Determine the team/group to route through. Prefer assignment_group,
        # fall back to the project's default group, then the item's workflow
        # team if available.
        team = (item.assignment_group or '').strip()
        if not team and item.project_id:
            try:
                team = (item.project.default_assignment_group or '').strip()
            except Exception:
                pass
        if not team:
            # Can't route without a team — leave for manual triage.
            continue

        # Refresh under lock so two concurrent sweeps don't double-assign.
        try:
            with _db_transaction.atomic():
                # Re-annotate the single row with its blocker-root state so the
                # HIVE-1828 fallback decision below is a genuine boolean, not a
                # bare Exists() expression (which is always truthy).
                fresh = (
                    Item.objects.filter(pk=item.pk)
                    .annotate(_is_blocker_root=_active_blocked_descendant_exists())
                    .select_for_update()
                    .first()
                )
                if fresh is None:
                    continue
                if fresh.assignee_id is not None or (fresh.assignee_email or '').strip():
                    continue  # claimed by another sweep tick
                if fresh.resolution_id is not None:
                    continue
                if (fresh.status or '').strip().lower() in _TERMINAL:
                    continue
                # Skip items with active linked blockers — they are
                # legitimately waiting on the blocker to resolve, whether or
                # not their status slug is literally 'blocked' (a stranded
                # blocker-root can sit in open/todo/in_progress with a live
                # inbound block link still attached).
                if _WS._has_active_blocker(fresh):
                    continue
                # PLS-1365: re-check under lock — parent may have terminalized
                # between the unlocked candidate scan and this write.
                if _WS._is_assignment_effect_terminal(fresh):
                    continue
                if _WS._force_pull_blocked_by_boundary(fresh):
                    continue

                # PLAT-9067 (kai 09-18 scope extension): routing-hold gate —
                # if the resolver recorded a no-routable-candidate hold for
                # this item inside the dwell window (PLS-278, persisted as a
                # system comment with origin_event='resolver.routability_hold',
                # deduped per task/team/reason-set/source), the override must
                # NOT force-assign against it. The hold is the resolver's
                # verdict that no live-capable seat exists — its reason
                # classes include the capability walls
                # ('seat_incapable', 'seat_incapable_substrate') and capacity
                # ('over_capacity'). Force-assigning anyway manufactures
                # exactly the override↔removal fight observed live on
                # HIVE-748 (urgent in_review, visual critique capability-walled
                # on ALL qa seats, explicitly routed to a vision-capable
                # reviewer — yet the override force-assigned text-only qa
                # seats ~30× overnight, alternating with PLS-509 reclaims).
                # The hold system re-fires on the next routing attempt, so
                # once the hold ages past the dwell window the override may
                # legitimately retry (seats may have come online or the hold
                # cleared).
                _dwell_minutes = int(
                    _os.environ.get('PULSE_CRITICAL_OVERRIDE_DWELL_MINUTES', '30')
                )
                if _dwell_minutes > 0 and (_ItemComment.objects
                        .filter(
                            item=fresh,
                            origin_event='resolver.routability_hold',
                            created_at__gte=(
                                timezone.now() - timedelta(minutes=_dwell_minutes)
                            ),
                        )
                        .exists()):
                    continue

                # PLAT-9067 + PLS-1365: force-pull rate-limit is applied above
                # via ``_force_pull_blocked_by_boundary`` (covers critical
                # override notices AND capacity-queue pulls; requires a status
                # change to re-fire inside the boundary window).

                # HIVE-1828: prefer a seat with ACTUAL admission headroom so
                # the critical override cannot flood one saturated seat with
                # work it can never start. Capacity-aware resolution mints a
                # WIP reservation and skips seats at/over the positive cap,
                # spreading the override across the team's real headroom.
                # Previously this used enforce_cap=False unconditionally, so a
                # seat could be piled to dozens of tasks and then deny its own
                # Start/Start Work with WIP_ACTIVE_CAPACITY_DENIED (assignment
                # override ≠ admission budget), with no reclaim relief for
                # recently-force-assigned rows.
                uid, email = _WS._resolve_team_assignee(
                    team,
                    item=fresh,
                    enforce_cap=True,
                    require_live_evidence=False,
                )
                if not email and bool(getattr(fresh, '_is_blocker_root', False)):
                    # A stranded blocker-root is PLS-509 non-sheddable WIP: if
                    # the whole team is at hard capacity it must still get a
                    # concrete owner (its descendants wait on it). Only this
                    # blocker-root case keeps the unconditional override.
                    uid, email = _WS._resolve_team_assignee(
                        team,
                        item=fresh,
                        enforce_cap=False,
                        require_live_evidence=False,
                        allow_operator=True,
                    )
                if not email:
                    # Non-blocker-root (or an unresolvable team): leave the item
                    # group-queued for the cap-aware rebalance sweep instead of
                    # piling it onto an admission-saturated seat.
                    continue

                # Apply assignment.
                fields = []
                _uid, _email = _WS._apply_resolved_assignee(
                    fresh, uid, email, fields,
                )
                if not _email:
                    continue

                # Set assignment_group if it was missing so normal routing
                # works going forward.
                if not (fresh.assignment_group or '').strip():
                    fresh.assignment_group = team
                    fields.append('assignment_group')

                fresh._activity_actor = (0, 'system@pulse')
                fresh._substrate_write_intent = 'system'
                fresh.save(update_fields=list(dict.fromkeys(
                    fields + ['updated_at'],
                )))

                # Post a system comment explaining the force-assignment.
                # PLAT-10519: this path does not call record_direct_handoff, so
                # the same-key watchdog must run here or force-assign loops stay
                # invisible (architecture review on pulse#759).
                _force_assign_posted = False
                try:
                    from .views import _FORCE_ASSIGN_COMMENT_PREFIX
                    _ItemComment.objects.create(
                        item=fresh, author_id=0, author_email='system@pulse',
                        content=(
                            f'{_FORCE_ASSIGN_COMMENT_PREFIX}: force-assigned to '
                            f'**{_email}** ({team}) because this urgent/high item had no '
                            f'owner and was in a normally-excluded status '
                            f'(`{fresh.status}`). WIP cap overridden for this item.'
                        ),
                    )
                    _force_assign_posted = True
                except Exception:
                    pass
                if _force_assign_posted:
                    try:
                        from .views import _watch_override_refires
                        _watch_override_refires(fresh, actor_email='system@pulse')
                    except Exception:
                        logger.warning(
                            '_assign_critical_unassigned: override watchdog failed '
                            'for %s',
                            getattr(fresh, 'item_key', fresh.pk),
                            exc_info=True,
                        )

                assigned += 1
                logger.info(
                    '_assign_critical_unassigned: force-assigned %s (%s) -> %s '
                    '(status=%s, priority=%s)',
                    fresh.item_key or fresh.pk, team, _email,
                    fresh.status, fresh.priority,
                )
        except Exception as exc:
            logger.warning(
                '_assign_critical_unassigned: failed for item %s: %s',
                getattr(item, 'item_key', item.pk), exc,
            )
            continue

    if assigned:
        logger.info(
            '_assign_critical_unassigned: assigned %d of %d critical unassigned candidates',
            assigned, len(candidates),
        )
    return assigned


def _sweep_rebalance_pods_impl(max_skew: int | None = None, only_agent_email: str | None = None):
    """
    Autonomous task distribution ("program management"): within each pod, keep
    workable load roughly even across available concrete members so no single
    agent (especially a throttled-backend one) becomes load-bearing while peers
    starve.

    Workable load excludes terminal, blocked, and scheduled work. When a member
    is above the pod's fair share, OPEN tasks (then unstarted in_progress — no
    linked PR) are reassigned to the lightest peer(s). Tasks that unblock the
    most active downstream work move first. The task's workflow status is
    preserved; rebalancing only changes owner. Idempotent; logs a comment per
    move. Direct-by-name assignments are caught here too, since this rebalances
    on assignee load regardless of how the task was assigned.
    """
    import os
    from django.db import transaction
    from .services.workflow_service import WorkflowService
    from .services.routing_authority import clear_assignment_to_group_queue
    from .user_service import user_service
    from .models import Item, ItemLink, Comment, TeamRoster
    from .services.operator_wait import (
        exclude_operator_wait_items,
        record_operator_wait_health,
        reconcile_operator_wait_markers,
        report_operator_wait_failure,
    )

    # PLS-529: converge the durable wait proof before any capacity, roster,
    # failover, or rebalance caller projects runnable demand. The sweep remains
    # available on failure, but PLAT-1254 requires durable health evidence and a
    # named-owner DM rather than a log-only exception.
    try:
        operator_wait_result = reconcile_operator_wait_markers()
        record_operator_wait_health(
            operator_wait_result, source='sweep_rebalance_pods',
        )
    except Exception as exc:
        logger.exception('PLS-529 operator-wait reconciliation failed')
        report_operator_wait_failure('sweep_rebalance_pods', exc)

    try:
        from .services.focused_qa import reconcile_focused_qa_routes
        repaired_focused_qa = reconcile_focused_qa_routes()
        if repaired_focused_qa:
            logger.info(
                'sweep_rebalance_pods: repaired %d organization-local focused-QA route(s)',
                repaired_focused_qa,
            )
    except Exception:
        logger.warning(
            'sweep_rebalance_pods: focused-QA route reconciliation failed',
            exc_info=True,
        )

    scoped_email = _canonical_agent_email(only_agent_email)
    scoped_aliases = _agent_email_aliases_for_heartbeat(scoped_email)

    # EVOL-20 Ph3 (PLAT-1304): when the membership rebalancer is enabled, the
    # CAPACITY balancing in this sweep MOVES MEMBERSHIPS, NOT TASKS — it grants/
    # expires time-boxed secondary memberships instead of shuffling tasks between
    # peers to even load (the churn class the HLD §5 kills). This flag ONLY gates
    # the capacity task-movers (the PLAT-424 shed + the skew load-balance loop
    # below). The FAILOVER DRAIN — evacuating a down/stopped/disabled agent's
    # already-assigned work onto a live peer — and the PLAT-236 queued-drain must
    # STILL run under the flag (a real availability requirement, aoi ruling on
    # kei's P1). The rebalance_memberships() pass runs once AFTER the per-pod
    # failover drains. Flag off (default) → the whole sweep runs unchanged.
    from .services import membership_rebalancer
    _membership_mode = membership_rebalancer.rebalancer_enabled()

    skew = int(max_skew) if max_skew is not None else int(os.environ.get('REBALANCE_MAX_SKEW', '1'))
    PRIORITY_WEIGHT = {
        'urgent': 4,
        'high': 3,
        'medium': 2,
        'normal': 2,
        'low': 1,
    }
    total_moves = 0
    details = {}

    # ── Fix 1: critical-unassigned override — force-assign urgent/high items
    # that have no concrete owner, even if they are in normally-excluded statuses
    # (blocked, scheduled, etc.). A stranded urgent task with no assignee is
    # worse than overriding WIP caps for one item. Runs before any other
    # rebalance logic so critical work lands on a warm seat immediately.
    _critical_assigned = _assign_critical_unassigned()
    if _critical_assigned:
        logger.info(
            'sweep_rebalance_pods: force-assigned %d critical unassigned '
            'item(s) before main rebalance loop',
            _critical_assigned,
        )
        total_moves += _critical_assigned

    def suppress_bulk_assignment_dm(item):
        """Keep automated queue repair observable without one Connect DM per row.

        The Item signal still records assignment activity and emits the Home
        activity event.  Pulse heartbeats discover the resulting queue.  Only
        the generic per-item notification fan-out is suppressed; otherwise a
        recovered/down reviewer with dozens of queued items receives dozens of
        system turns and burns context merely acknowledging the same sweep.
        """
        item._suppress_standard_notifications = True

    excluded_statuses = WorkflowService._assignment_excluded_statuses()
    # PLS-733: assignment exclusion is not terminality. waiting_external,
    # blocked and scheduled are parked but authoritative live work, and must
    # still participate in dependent counts, drain/re-home, and ghost detection.
    terminal_statuses = set(WorkflowService.TERMINAL_STATUS_SLUGS)
    # PLAT-390 two-tier: balance on HANDS-ON (active) load and bound total
    # in-flight. Review-pipeline work (in_review/in_qa/awaiting_merge/applied) is
    # in-flight but NOT hands-on, so it doesn't make a member look heavy for
    # balancing nor "full" for the capacity-queue drain (the PLAT-307/375 fix).
    review_pipeline = WorkflowService._REVIEW_PIPELINE_STATUSES
    pipeline_queue_lanes = {
        'in_review': 'review',
        'in_qa': 'qa',
        'awaiting_merge': 'merge',
    }
    active_cap = WorkflowService._wip_cap()
    inflight_cap = WorkflowService._inflight_cap()

    def _scope_items(qs, organization_id):
        if organization_id is not None:
            return qs.filter(project__org_id=organization_id)
        return qs

    def workable_qs(user_id, email=None, organization_id=None):
        return exclude_operator_wait_items(_scope_items((
            Item.objects
            .filter(WorkflowService._agent_load_q(user_id, email), resolution__isnull=True)
            .exclude(status__in=excluded_statuses)
            # PLS-25: epics/umbrellas aren't pullable work — don't count them toward
            # an owner's load, or a pinned-epic owner reads at/over cap and the sweep
            # stops pulling real queued tasks for them.
            .exclude(WorkflowService._umbrella_item_q())
        ), organization_id))

    def active_qs(user_id, email=None, organization_id=None):
        return workable_qs(
            user_id,
            email,
            organization_id,
        ).exclude(status__in=review_pipeline)

    def has_linked_pr(item):
        return bool((item.metadata or {}).get('pull_requests'))

    def active_downstream_count(item):
        count = 0
        links = ItemLink.objects.filter(
            source_item=item,
            link_type='blocks',
        ).select_related('target_item__resolution')
        links = list(links) + list(
            ItemLink.objects.filter(
                target_item=item,
                link_type='is_blocked_by',
            ).select_related('source_item__resolution')
        )
        for link in links:
            downstream = link.target_item if link.source_item_id == item.id else link.source_item
            if downstream.resolution_id:
                continue
            if (downstream.status or '').strip().lower() in terminal_statuses:
                continue
            count += 1
        return count

    # Unstarted states across workflows: 'open' (autonomous-dev) and 'todo'
    # (simple). Both are not-yet-started and equally movable; only started
    # (in_progress) work needs the no-linked-PR guard.
    UNSTARTED_STATUSES = ('open', 'todo')

    def move_rank(item):
        status_rank = 0 if item.status in UNSTARTED_STATUSES else 1
        return (
            status_rank,
            -active_downstream_count(item),
            -PRIORITY_WEIGHT.get(item.priority, 0),
            item.updated_at,
            -item.id,
        )

    def owner_started_current_assignment(item):
        """Whether the current concrete owner has acted since receiving ``item``.

        Review/QA/merge statuses are team queues even though they carry a concrete
        assignee so heartbeats can discover them. A recovered reviewer may therefore
        safely receive an untouched queued review, but we must never yank a review
        after its current owner has started commenting or transitioning it.
        """
        from .models import ItemActivity
        assignment = (
            ItemActivity.objects
            .filter(item_id=item.pk, action='assigned')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
        if assignment is None:
            # Legacy rows without an assignment boundary are ambiguous. Preserve
            # ownership rather than guessing that active work is still untouched.
            return True
        aliases = WorkflowService._agent_email_aliases(item.assignee_email)
        if not aliases:
            return True
        owner_q = Q()
        for alias in aliases:
            owner_q |= Q(user_email__iexact=alias)
        return (
            ItemActivity.objects
            .filter(item_id=item.pk, created_at__gt=assignment)
            .filter(owner_q)
            .exclude(action='assigned')
            .exists()
        )

    # PLS-565 freshness guard: tasks assigned within this window are not
    # movable even if the owner hasn't acted yet. Prevents the rebalance
    # sweep from "stealing" work that was just assigned moments ago.
    _FRESH_ASSIGN_WINDOW = timedelta(minutes=30)

    def _is_freshly_assigned(item):
        """True if the item was assigned within the freshness window."""
        from .models import ItemActivity as _ItemActivity
        last_assign = (
            _ItemActivity.objects
            .filter(item_id=item.pk, action='assigned')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
        if last_assign is None:
            return False
        cutoff = timezone.now() - _FRESH_ASSIGN_WINDOW
        return last_assign > cutoff

    def movable_items(user_id, pod=None, email=None, organization_id=None):
        # 'todo' and 'open' are both unstarted/non-terminal initial statuses
        # depending on the item's workflow; both must be rebalanceable (PLS-18),
        # else 'todo'-initial work strands on one owner forever (e.g. kai @ 49 todo).
        #
        # PLAT-916: hard team-boundary predicate — only move tasks that
        # explicitly belong to THIS pod. A blank/NULL assignment_group is not
        # proof of ownership; treating it as every pod's work caused cross-team
        # misroutes during API/outage windows. Such rows must be fixed by explicit
        # workflow/triage routing, not inferred by the rebalance sweep.
        from django.db.models import Q
        # PLAT-8222: capability gate — an item carrying high-risk labels demands
        # the capabilities mapped in HIGH_RISK_LABEL_CAPABILITY_MAP; a pod whose
        # team does not hold them must SKIP the item (never downweight: a
        # downweight still routes eventually and reproduces the misroute
        # ping-pong this defect documents). Fail-open per PLS-198: profile-less
        # teams are un-gated (see seat_team_satisfies). The gate lives INSIDE
        # the existing candidate filter on the PLS-198 incident surface — no
        # new sweep pass. Resolved once per call (pod-level, not per-item).
        from .services.focused_qa import item_required_capabilities, seat_team_satisfies
        _cap_cache = {}
        _cap_skips = []

        def _capability_admits(item):
            required = item_required_capabilities(item)
            if not required:
                return True
            key = tuple(sorted(required))
            verdict = _cap_cache.get(key)
            if verdict is None:
                verdict = seat_team_satisfies(pod, required)
                _cap_cache[key] = verdict
            if not verdict:
                # item_key is nullable (manual/non-project epics) — fall back to
                # the pk for display (same convention as the PLS-25 skip log).
                _cap_skips.append((getattr(item, 'item_key', None) or f'#{item.pk}', sorted(required)))
            return verdict
        group_q = Q(assignment_group__iexact=pod) if pod is not None else Q(pk__in=[])
        # PLS-25: epic/umbrella tracking items (item_type 'epic' / 'task.epic*') are
        # containers whose CHILDREN carry the work. Moving the container enables zero
        # work — it just churns assignment notifications and breaks owner continuity
        # (SCLI-1/SCLI-3 bounced 3x in 24h between agents). Exclude them from the
        # movable set on BOTH the rebalance and failover-drain paths; standing team
        # anchors stay pinned to the owning lead/architect until a human moves them.
        umbrella_q = WorkflowService._umbrella_item_q()  # PLS-25 (shared predicate)
        # PLAT-390 (Codex P2): match the email-primary load predicate so the movable
        # set covers email-owned/drifted rows the load counts include — else a member
        # can read at/over cap from email-owned work the sweep can never move.
        base_q = WorkflowService._agent_load_q(user_id, email) & Q(resolution__isnull=True) & group_q
        unstarted_qs = exclude_operator_wait_items(_scope_items(
            Item.objects.filter(
                base_q & Q(mode='task') & Q(status__in=UNSTARTED_STATUSES)
            ),
            organization_id,
        ))
        # item_key is nullable (manual/non-project epics) — a None in the list would
        # make sorted() raise TypeError (None vs str) and abort the whole sweep, so
        # fall back to the pk for display (Codex P2).
        skipped = list(unstarted_qs.filter(umbrella_q).values_list('item_key', 'pk'))
        if skipped:
            skipped_keys = sorted((k or f'#{pk}') for k, pk in skipped)
            logger.info(
                "sweep_rebalance_pods: skipped %d epic/umbrella item(s) as non-movable "
                "(PLS-25, not pullable work): %s", len(skipped), skipped_keys
            )
        unstarted = [
            i for i in unstarted_qs.exclude(umbrella_q)
            if not _is_cos_pinned(i) and not _is_decision_pinned(i)  # PLAT-979 / PLS-233
            and _capability_admits(i)  # PLAT-8222
        ]
        in_progress = [
            item
            for item in exclude_operator_wait_items(_scope_items(
                Item.objects.filter(
                    base_q & Q(mode='task') & Q(status='in_progress')
                ),
                organization_id,
            ).exclude(umbrella_q))
            if not has_linked_pr(item) and not _is_cos_pinned(item) and not _is_decision_pinned(item) and not _is_freshly_assigned(item)  # PLAT-979 / PLS-233 / PLS-565
            and _capability_admits(item)  # PLAT-8222
        ]
        # A concrete assignee on review/QA/merge is the discoverable holder of a
        # team queue item, not proof that work has begun. Membership-mode correctly
        # avoids shuffling engineering implementation work, but it cannot repair a
        # review lane after a transient outage drains or newly routes every queued
        # review to one surviving reviewer. Include only untouched pipeline items;
        # owner_started_current_assignment() protects in-flight human/agent work.
        pipeline = [
            item
            for item in _scope_items(
                Item.objects.filter(
                    base_q & Q(mode='task') & Q(status__in=review_pipeline)
                ),
                organization_id,
            ).exclude(umbrella_q)
            if pipeline_queue_lanes.get(
                WorkflowService._normalize_status_slug(item.status)
            ) == pod
            and not WorkflowService._explicit_merge_handoff_email(item)
            and not _is_cos_pinned(item)
            and not _is_decision_pinned(item)
            and not owner_started_current_assignment(item)
            and _capability_admits(item)  # PLAT-8222
        ]
        if _cap_skips:
            logger.info(
                "sweep_rebalance_pods: capability-gated skip of %d item(s) for pod %r "
                "(PLAT-8222 — team lacks the mapped capabilities, item NOT downweighted): %s",
                len(_cap_skips), pod,
                sorted(_cap_skips),
            )
        return sorted(unstarted + in_progress + pipeline, key=move_rank)

    def drain_all_items(user_id, email=None, pod=None, organization_id=None):
        # PLAT-350: a DISABLED agent (operator toggle off) is persistently not-running,
        # so ALL its non-terminal work strands — in_progress incl. open-PR, in_review,
        # blocked — not just the unstarted set movable_items returns. Drain everything
        # non-terminal, team-gated, so nothing is left on a disabled assignee (AC1/AC2).
        # aoi P2: also match assignee_email — legacy/fallback assignments (the
        # workflow_service post-function path) can store the disabled agent by email only
        # with no concrete assignee_id, and would otherwise survive the drain.
        from django.db.models import Q
        # PLAT-916: full-drain still respects the owning-team boundary. Unknown
        # ownership (NULL/blank assignment_group) is not every pod's work. The
        # one exception: a down/hibernated owner whose UNIQUE org roster team
        # is this pod — Admin Ops escalations on simple workflows often land
        # with a concrete assignee and a blank group, then stick on a
        # hibernated member while a live teammate idles.
        group_q = Q(assignment_group__iexact=pod) if pod is not None else Q(pk__in=[])
        who = Q(assignee_id=user_id)
        if email:
            who = who | Q(assignee_email__iexact=email)
            for alias in WorkflowService._agent_email_aliases(email):
                who = who | Q(assignee_email__iexact=alias)
        # Scheduled tasks are intentionally non-actionable until their due cycle.
        # Blocked work is different: provider/offline failover may update the
        # concrete holder while the blocker remains authoritative (see
        # workflow_service's pre-block owner capture). Preserve the narrow
        # PLS-250 carve-out for operator/admin-ops held blockers only.
        drain_excluded_statuses = set(terminal_statuses) | {'scheduled'}
        # PLAT-8821: deliberate parks never enter the failover rotation. A
        # backlog/deferred ticket (or one carrying a recent operator/lead
        # park disposition) stays put when its assignee goes offline — the
        # CON-389 anti-pattern (5 failover hops in one day against an
        # explicit operator park directive) must be structurally impossible.
        items = [
            i for i in exclude_operator_wait_items(_scope_items(Item.objects.filter(
                who & Q(mode='task') & Q(resolution__isnull=True) & group_q
            ), organization_id).exclude(status__in=drain_excluded_statuses))
            if not (
                (i.status or '').strip().lower() == 'blocked'
                and (i.metadata or {}).get('blocked_via') == 'admin_ops_escalation'
            ) and not WorkflowService._item_parked_from_failover(i)
        ]
        unique_pod = _unique_org_roster_team(email, organization_id) if email else None
        if (
            unique_pod
            and pod is not None
            and unique_pod == WorkflowService._canonical_team_name(pod)
        ):
            blank_group = Q(assignment_group__isnull=True) | Q(assignment_group='')
            seen = {i.pk for i in items}
            for blank in _scope_items(Item.objects.filter(
                who & Q(mode='task') & Q(resolution__isnull=True) & blank_group
                & Q(status__in=UNSTARTED_STATUSES)
            ), organization_id):
                if blank.pk not in seen:
                    items.append(blank)
                    seen.add(blank.pk)
        # A DISABLED/STOPPED agent (or a superseded failover tier) must FULLY evacuate —
        # epics/umbrellas INCLUDED (operator directive 2026-06-23). A standing anchor
        # pinned to a stopped agent is dead weight: no one decomposes it, and its blocked
        # children never auto-resume to a working owner. So the full-drain path moves epics
        # too, onto an active team member (the lead). The SCLI-1/3 anti-bounce umbrella
        # exclusion stays in movable_items() (active-agent LOAD rebalancing) ONLY — there it
        # stops churning epics between LIVE peers; here it is a ONE-TIME evacuation off a
        # dead owner onto the stable active one, so there is no bounce to reintroduce.
        # Blank-group items above are unstarted only — blocked/scheduled blanks stay.
        return sorted(items, key=move_rank)

    _api_ok, _api_skip_reason = _rebalance_pulse_api_health_gate()
    if not _api_ok:
        return {'moves': 0, 'pods': {}, 'skipped': _api_skip_reason}

    # PLAT-319: gate the sweep on the health exporter being reachable.
    # When the exporter is down, _is_agent_available_for_assignment falls open
    # (returns True for everyone) and no failover fires — that's safe. But if
    # the exporter just recovered with stale samples, the availability map may
    # mark live agents as down and trigger incorrect failover drains. Abort the
    # sweep until we can confirm the source of truth is fresh and reachable.
    _health_url = os.environ.get('PULSE_HEALTH_EXPORTER_URL', '').strip()
    if _health_url and _health_url.lower() not in {'disabled', 'off', 'none'}:
        try:
            _probe = requests.get(_health_url, timeout=5)
            if _probe.status_code >= 500:
                logger.warning(
                    f"sweep_rebalance_pods: health exporter returned {_probe.status_code} "
                    "— aborting sweep to avoid stale-signal failover")
                return {'moves': 0, 'pods': {}, 'skipped': 'exporter_unhealthy'}
            # PLAT-348 (PLAT-319 follow-up): a 200 with an empty/malformed body
            # passes the status check but carries no availability data — verify the
            # expected metric is actually present before trusting the signal for
            # failover decisions (an exporter starting up / serving a wrong endpoint
            # can 200 with no samples, which would otherwise drive a blind drain).
            _body = _probe.text
            if 'shizuha_agent_process_up' not in _body:
                logger.warning(
                    "sweep_rebalance_pods: health exporter 200 but no "
                    "shizuha_agent_process_up sample in body — aborting sweep to "
                    "avoid failover on an empty/malformed availability signal")
                return {'moves': 0, 'pods': {}, 'skipped': 'exporter_no_metric'}
        except Exception as _exc:
            logger.warning(
                f"sweep_rebalance_pods: health exporter unreachable ({_exc!r}) "
                "— aborting sweep to avoid blind failover drain")
            return {'moves': 0, 'pods': {}, 'skipped': 'exporter_unreachable'}

    # PLS-188: normalize status-owned lanes before capacity queue drain,
    # failover, or skew balancing sees stale assignment_group values. An
    # awaiting_merge task is Merge-lane work even if its pre-block state or a
    # previous review handoff left assignment_group=review / assignee=Reika.
    _roster_reconciliation_health = None
    _reconciled = 0
    _reconcile_cap_hit = False
    try:
        lane_statuses = list(getattr(WorkflowService, '_STATUS_LANE_ASSIGNMENT_GROUPS', {}).keys())
        if lane_statuses:
            # PLS-411 live gate: linked blockers retain their underlying lane in
            # pre_block_state.  Include blocked items here so an awaiting_merge
            # handoff is protected before the generic unavailable-owner drain.
            from django.db.models import Q as _LaneQ
            _lane_items = Item.objects.filter(
                _LaneQ(status__in=lane_statuses)
                | _LaneQ(
                    status='blocked',
                    metadata__pre_block_state__status__in=lane_statuses,
                )
                | _LaneQ(
                    status='blocked',
                    metadata__previous_status__in=lane_statuses,
                ),
                mode='task', resolution__isnull=True,
            )
            if scoped_aliases:
                _lane_items = _lane_items.filter(assignee_email__in=scoped_aliases)
            for _it in _lane_items:
                _effective_status = WorkflowService._effective_status_lane_status(_it)
                if _effective_status not in lane_statuses:
                    continue
                # PLAT-8821: no actionable work left — a merged-PR/no-open-PR
                # item must not have its lane owner re-resolved (the PLAT-8787
                # assignee-hop class); its own workflow owns the next step.
                if not WorkflowService._item_failover_actionable(_it):
                    continue
                _fields = []
                _old_group = _it.assignment_group
                _old_email = _it.assignee_email
                # Use the durable pre-mutation row version in the event identity.
                # A transaction retry sees the same value; a later independent
                # recurrence sees the newer committed value and therefore mints
                # a distinct lifecycle event.
                _prior_updated_at = _it.updated_at.isoformat()
                _lane, _changed = WorkflowService._enforce_status_lane_owner(
                    _it, _effective_status, _fields,
                )
                if _changed and _fields:
                    try:
                        from django.db import transaction as _lane_transaction
                        from .services.mechanical_comments import (
                            MechanicalCommentFactory,
                            deterministic_event_id,
                        )
                        with _lane_transaction.atomic():
                            _it._activity_actor = (0, 'system@pulse')
                            _it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): status-lane owner re-enforce sweep
                            suppress_bulk_assignment_dm(_it)
                            _it.save(update_fields=list(dict.fromkeys(_fields + ['updated_at'])))
                            _hold = ((_it.metadata or {}).get('merge_sod_hold') or {})
                            if _hold:
                                _event = 'status_lane.merge_sod_hold'
                                _payload = {
                                    'status': str(_it.status),
                                    'reason': str(_hold.get('reason') or ''),
                                    'current_head_reviewers': list(_hold.get('current_head_reviewers') or []),
                                    'excluded_assignees': list(_hold.get('excluded_assignees') or []),
                                }
                            else:
                                _event = 'status_lane.normalized'
                                _payload = {
                                    'status': str(_it.status),
                                    'old_group': str(_old_group or ''),
                                    'old_email': str(_old_email or ''),
                                    'new_group': str(_lane),
                                    'new_email': str(_it.assignee_email or ''),
                                }
                            MechanicalCommentFactory.create(
                                item=_it,
                                origin_kind='WORKFLOW_LIFECYCLE',
                                origin_event=_event,
                                template_version=1,
                                structured_payload=_payload,
                                origin_event_id=deterministic_event_id(
                                    _event, _it, _it.status, _old_group, _old_email,
                                    _lane, _it.assignee_email, _prior_updated_at,
                                ),
                            )
                    except Exception:
                        logger.exception(
                            'sweep_rebalance_pods: atomic status-lane audit failed for %s',
                            getattr(_it, 'item_key', _it.pk),
                        )
                        continue
                    total_moves += 1
    except Exception:
        logger.exception('sweep_rebalance_pods: status-lane normalization failed')

    # PLS-408: roster-membership reconciliation. The team-membership invariant
    # (a concrete AGENT assignee must be a member of the item's assignment_group
    # — operator 2026-06-23) is enforced at the assignee WRITE
    # (_apply_resolved_assignee) and, for status-owned lanes, by the PLS-188
    # pass above. Items assigned BEFORE the write-guard existed (or whose owner
    # later left the roster) keep a non-member owner forever — e.g. an
    # in_review task still owned by its author never surfaces in any
    # review-team member's queue, and team-scoped workload views count it
    # against the wrong team. Reconcile queue-visible statuses only:
    # in_progress is deliberately left alone (never yank in-flight work; the
    # next workflow transition re-resolves the owner), terminal/parked statuses
    # don't route, and `scheduled` stays with drain_all_items' carve-out
    # (non-actionable until the due cycle; recurring ownership has its own
    # sweep_recurring_owner_team_drift). Human/operator pins are skipped
    # (PLAT-785: a human IS the intended owner), CoS/decision pins are sacred
    # (PLAT-979 / PLS-233 — deliberate direct assignment must not bounce back
    # to the symptom-team pool), and an unstaffed/unknown team resolves to
    # nobody — the current owner is then kept rather than stranding the item
    # unassigned (staffing gaps are sweep_org_health/PLS-197 findings).
    try:
        _RECONCILE_STATUSES = ('open', 'todo', 'backlog', 'blocked', 'in_review')
        _reconcile_cap = int(os.environ.get('PULSE_ROSTER_RECONCILE_CAP', '25') or 25)
        _lane_set = set(lane_statuses or [])
        _reconcile_qs = exclude_operator_wait_items(
            Item.objects
            .filter(mode='task', resolution__isnull=True, is_archived=False,
                    status__in=[s for s in _RECONCILE_STATUSES if s not in _lane_set])
            .exclude(assignment_group__isnull=True).exclude(assignment_group='')
            .exclude(assignee_email__isnull=True).exclude(assignee_email='')
            .order_by('updated_at')
        )
        if scoped_aliases:
            _reconcile_qs = _reconcile_qs.filter(assignee_email__in=scoped_aliases)

        def _assignment_actor_is_person(item) -> bool:
            """PLAT-11229: True when the item's most recent ``assigned`` activity
            boundary was recorded against a named (non-system) actor — i.e. the
            concrete owner was set directly by a person (department-internal
            delegation, lead override, direct-handoff), not by a sweep/lease
            path. Rebalance/failover/re-lease writes stamp
            ``_activity_actor = (0, 'system@pulse')`` before ``save()``, so a
            system actor on the last assignment boundary is the discriminator.
            No recorded boundary (legacy row) → not a person override; keep the
            pre-existing reconciliation behavior rather than guessing."""
            from .models import ItemActivity
            last = (
                ItemActivity.objects
                .filter(item_id=item.pk, action='assigned')
                .order_by('-created_at')
                .values_list('user_email', 'user_id')
                .first()
            )
            if last is None:
                return False
            _actor_email, _actor_id = last
            _actor_email = (_actor_email or '').strip().lower()
            if not _actor_email or _actor_email == 'system@pulse':
                return False
            if _actor_email == 'scheduler@pulse.shizuha.com':
                # Scheduler/recurrence machinery also stamps user_id 0 with a
                # machine identity (recurring truth-park, scheduler logging) —
                # system actor, keep legacy reconciliation behavior.
                return False
            # PLAT-11701 follow-up (reika review on PLAT-11229): the previous
            # blanket ``user_id == 0 → not a person`` reject also dropped agent
            # MCP assignments that stamp id 0 with a real agent email (the ID-
            # service user is not resolvable from the writing seat). An id-0
            # boundary carrying any other (real) email IS a person-made
            # assignment — honor it. This is the 2026-10-04 incident class:
            # PLAT-11217/11218 were direct-assigned mid-session and twice
            # cleared back to `found`/unassigned by this reconciliation.
            return True

        def _locked_roster_repair(_pk):
            """PLS-524 P1: run the eligibility revalidation AND the clear/replace
            write under ONE ``select_for_update`` lock, so the write fence and the
            decision that authorizes it share a single item snapshot. The gates in
            the loop below are only an UNLOCKED pre-filter; a workflow transition,
            blocker auto-resume, or concurrent reassignment landing between that
            stale read and the clear could otherwise strand a NEWLY-VALID owner
            (or lease over a just-resolved blocker). Returns moves applied (0/1).
            """
            with transaction.atomic():
                _it = Item.objects.select_for_update().filter(pk=_pk).first()
                if _it is None:
                    return 0
                _email = (_it.assignee_email or '').strip().lower()
                _grp = (_it.assignment_group or '').strip()
                if (not _it.assignee_id and not _email) or not _grp:
                    return 0
                # Revalidate every eligibility gate on the freshly-locked row —
                # the current owner may differ from the pre-filter snapshot.
                if WorkflowService._is_team_member(_email, _grp, item=_it):
                    return 0
                if WorkflowService._is_human_account(_email):
                    return 0
                if WorkflowService._is_operator_target(_it.assignee_id, _email):
                    return 0
                if _is_cos_pinned(_it) or _is_decision_pinned(_it):
                    return 0
                # PLAT-11229: an explicit person-made direct assignment (a
                # concrete owner set by a named actor — e.g. a security lead's
                # department-internal delegation during a WIP-cap hold) is an
                # ownership decision the sweep must NOT silently revert even
                # when the owner is not on the row's team roster. The 2026-10-04
                # incident: PLAT-11217/11218 were direct-assigned mid-session and
                # twice cleared back to `found`/unassigned by this reconciliation.
                # Honor the override (metadata-stamped so the audit comment posts
                # once, not per sweep tick); system-actor assignments (rebalance
                # re-leases, capacity pulls, failover drains) keep the existing
                # reconciliation behavior, and the separate ghost/down-owner
                # drains still evacuate a departed or unavailable direct owner.
                if _assignment_actor_is_person(_it):
                    _meta = dict(_it.metadata or {})
                    if not _meta.get('direct_override_honored_at'):
                        _meta['direct_override_honored_at'] = timezone.now().isoformat()
                        _it.metadata = _meta
                        _it.save(update_fields=['metadata', 'updated_at'])
                        _create_system_comment_deduped(
                            _it,
                            f'Honoring direct override: `{_email}` is not a member of the '
                            f'**{_grp}** team, but the current concrete assignment was '
                            'made directly by a person — the rebalance sweep did not '
                            'revert same-session direct ownership (PLAT-11229).',
                        )
                    return 0
                _effective_status = WorkflowService._effective_status_lane_status(_it)
                _explicit_merge_handoff = (
                    WorkflowService._explicit_merge_handoff_email(_it)
                    if WorkflowService._status_lane_assignment_group(
                        _effective_status
                    ) == 'merge'
                    else ''
                )
                if (
                    _explicit_merge_handoff
                    and WorkflowService._emails_same_agent_identity(
                        _email, _explicit_merge_handoff,
                    )
                ):
                    return 0
                _old_email = _it.assignee_email
                # Linked-blocked: clear the ineligible seat, no replacement lease.
                if ((_it.status or '').strip().lower() == 'blocked'
                        and WorkflowService._active_blockers_for(_it)):
                    if clear_assignment_to_group_queue(
                        _it, reason='routability_hold', save=True,
                    ):
                        _create_system_comment_deduped(
                            _it,
                            f'Cleared ineligible concrete owner `{_old_email}` to '
                            f'the **{_grp}** group queue while active linked blockers '
                            'remain; no replacement lease was issued (PLS-524).',
                        )
                        return 1
                    return 0
                _fields = []
                _resolved_uid, _resolved_email = WorkflowService._resolve_team_assignee(
                    _grp,
                    project_key=WorkflowService._item_project_key(_it),
                    item=_it,
                )
                if not _resolved_email:
                    if clear_assignment_to_group_queue(
                        _it, reason='routability_hold', save=True,
                    ):
                        _create_system_comment_deduped(
                            _it,
                            f'Cleared ineligible concrete owner `{_old_email}` to '
                            f'the **{_grp}** group queue because no eligible '
                            'replacement was resolvable (PLS-408 / PLS-524).',
                        )
                        return 1
                    return 0
                _uid, _new_email = WorkflowService._apply_resolved_assignee(
                    _it, _resolved_uid, _resolved_email, _fields)
                if (_new_email or '').strip().lower() == _email:
                    return 0
                _it._activity_actor = (0, 'system@pulse')
                _it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): unstaffed-team re-resolve sweep
                suppress_bulk_assignment_dm(_it)
                _it.save(update_fields=list(dict.fromkeys(_fields + ['updated_at'])))
                try:
                    Comment.objects.create(
                        item=_it, author_id=0, author_email='system@pulse',
                        content=(
                            f'Re-leased to a `{_grp}` roster member: `{_old_email}` is not a '
                            f'member of this task\'s team → `{_it.assignee_email}` '
                            f'(PLS-408 roster-membership reconciliation).'
                        ),
                    )
                except Exception:
                    pass
                return 1

        for _it in _reconcile_qs.iterator():
            if _reconciled >= _reconcile_cap:
                _reconcile_cap_hit = True
                logger.info(
                    'sweep_rebalance_pods: roster reconciliation cap (%s) reached '
                    '— remainder handled next run', _reconcile_cap)
                break
            _email = (_it.assignee_email or '').strip().lower()
            _grp = (_it.assignment_group or '').strip()
            if WorkflowService._is_team_member(_email, _grp, item=_it):
                continue
            if WorkflowService._is_human_account(_email):
                continue
            if WorkflowService._is_operator_target(_it.assignee_id, _email):
                continue
            if _is_cos_pinned(_it) or _is_decision_pinned(_it):
                continue
            # PLAT-8821: no actionable work left — a merged-PR/no-open-PR item
            # must not have its non-member owner replaced (PLAT-8787 hop class);
            # its own workflow owns the next step.
            if not WorkflowService._item_failover_actionable(_it):
                continue
            # PLS-411: status-lane normalization may have just preserved a
            # signed explicit merge handoff.  Membership reconciliation is not
            # an availability authority and must never consume/clear that
            # deliberate SoD pin.  The status-lane pre-pass is the item-scoped
            # live authority; a later roster snapshot may not overturn it.
            _effective_status = WorkflowService._effective_status_lane_status(_it)
            _explicit_merge_handoff = (
                WorkflowService._explicit_merge_handoff_email(_it)
                if WorkflowService._status_lane_assignment_group(
                    _effective_status
                ) == 'merge'
                else ''
            )
            if (
                _explicit_merge_handoff
                and WorkflowService._emails_same_agent_identity(
                    _email, _explicit_merge_handoff,
                )
            ):
                continue
            # PLS-524 P1: the gates above are an UNLOCKED pre-filter only. The
            # authoritative eligibility revalidation AND the clear/replace write
            # run together under one row lock in _locked_roster_repair, so a
            # concurrent owner change between this pre-filter and the write fence
            # can never strand a newly-valid owner or lease over a just-resolved
            # blocker.
            _delta = _locked_roster_repair(_it.pk)
            _reconciled += _delta
            total_moves += _delta
    except Exception as exc:
        logger.exception('sweep_rebalance_pods: roster-membership reconciliation failed')
        _roster_reconciliation_health = _record_roster_reconciliation_health(
            state='failure', reconciled=_reconciled,
            cap_hit=_reconcile_cap_hit, error=exc,
        )
        _notify_roster_reconciliation_owner(_roster_reconciliation_health)
    else:
        _roster_reconciliation_health = _record_roster_reconciliation_health(
            state='success', reconciled=_reconciled,
            cap_hit=_reconcile_cap_hit,
        )
        _resolve_roster_reconciliation_failure_alert()

    # ProjectTeamLead is durable project affinity, not runtime health state.
    # Resolver calls spill individual items around an unavailable lead; roster
    # removal deactivates the record authoritatively. Never destroy the election
    # from one transient exporter/provider sample during a rollout.

    try:
        recovery = _reclaim_wip_overload()
        if any(recovery.values()):
            logger.warning('PLS-509 WIP overload reclaimed: %s', recovery)
            total_moves += sum(recovery.values())
    except Exception:
        logger.exception('PLS-509 WIP overload recovery failed')

    roster_entries = {}
    for roster in TeamRoster.objects.filter(
        is_active=True,
        organization_id__isnull=False,
    ).order_by('organization_id', 'team_name'):
        pod = WorkflowService._canonical_team_name(roster.team_name)
        scope_org_id = int(roster.organization_id)
        roster_entries[(scope_org_id, pod)] = {
            'organization_id': scope_org_id,
            'organization_slug': roster.organization_slug,
            'team_name': pod,
            'tiers': roster.tiers or [],
            'wip_enforced': bool(roster.wip_enforced),
        }

    for roster_entry in roster_entries.values():
        pod = roster_entry['team_name']
        emails = roster_entry['tiers']
        roster_org_id = roster_entry['organization_id']
        detail_key = (
            pod
            if roster_org_id is None
            else f'{roster_entry["organization_slug"]}:{pod}'
        )
        if scoped_aliases and not any(
            WorkflowService._agent_email_aliases(email) & scoped_aliases
            for email in WorkflowService._flat_roster(emails)
        ):
            continue
        # Resolve ALL roster members -> (email, uid, available?). We need the
        # unavailable ones too, to drain their stranded work.
        # _flat_roster() flattens tiered values ([[tier0], [tier1]]) to a flat
        # email list — without it, `e` is a list for tiered pods and
        # get_user_by_email raises, silently skipping the pod (aoi P1).
        # PLAT-355 aoi P1: respect FAILOVER-ONLY tier semantics. Primary tier
        # receives new work; failover tiers only engage when the ENTIRE primary
        # is unavailable (not merely at-cap). _flat_roster() still drives
        # `roster`/`down` so ghost-drain inspects all-tier members.
        _tiers = WorkflowService._roster_tiers(emails)
        _active_tier_emails: set = set()
        _active_tier_idx = None
        for _i, _tier in enumerate(_tiers):
            if any(WorkflowService._is_agent_available_for_assignment(e) for e in _tier):
                _active_tier_emails = {e.lower() for e in _tier}
                _active_tier_idx = _i
                break
        # PLAT-421: emails in tiers ABOVE the active tier form a fully-down PRIMARY
        # tier that the failover promoted past. Their in-flight work — including
        # in_review they OWN as a reviewer — won't progress on the dead tier, so it
        # must FULLY drain to the live tier (drain_all_items), not just the unstarted
        # set movable_items returns. A down member of the STILL-ACTIVE tier keeps the
        # conservative drain (it may return; its in_review legitimately waits).
        _superseded_tier_emails: set = set()
        if _active_tier_idx:  # truthy => idx > 0 => a higher-priority tier was skipped
            for _tier in _tiers[:_active_tier_idx]:
                _superseded_tier_emails |= {e.lower() for e in _tier}

        roster = []
        for e in WorkflowService._flat_roster(emails):
            try:
                info = user_service.get_user_by_email(e)
                uid = info.get('id') if info else None
                if uid and str(uid).isdigit():
                    roster.append((e, int(uid),
                                   bool(WorkflowService._is_agent_available_for_assignment(e))))
            except Exception:
                continue
        members = [(e, uid) for e, uid, ok in roster if ok and e.lower() in _active_tier_emails]
        down = [(e, uid) for e, uid, ok in roster if not ok]
        if scoped_aliases:
            down = [
                (e, uid) for e, uid in down
                if WorkflowService._agent_email_aliases(e) & scoped_aliases
            ]

        def _clear_linked_blocked_down_owner(candidate, down_email, down_uid):
            """Release a hard-down blocked owner before target resolution.

            This intentionally runs before every candidate/no-peer early exit:
            linked-blocked work issues no replacement lease, so finding a target
            is irrelevant to releasing the dead concrete owner.
            """
            with transaction.atomic():
                fresh = Item.objects.select_for_update().filter(pk=candidate.pk).first()
                if fresh is None or _is_terminal_status(fresh.status):
                    return False, False
                fresh_status = (fresh.status or '').strip().lower()
                if (
                    fresh_status != 'blocked'
                    or (fresh.metadata or {}).get('blocked_via') == 'admin_ops_escalation'
                    or not WorkflowService._active_blockers_for(fresh)
                ):
                    return False, False
                # PLS-411: the status-lane pre-pass has already live-checked a
                # signed explicit merge handoff.  That item-scoped decision is
                # stronger than the later roster snapshot, so do not consume a
                # second availability sample or clear the deliberate SoD pin.
                effective_status = WorkflowService._effective_status_lane_status(fresh)
                explicit_merge_handoff = (
                    WorkflowService._explicit_merge_handoff_email(fresh)
                    if WorkflowService._status_lane_assignment_group(
                        effective_status
                    ) == 'merge'
                    else ''
                )
                if explicit_merge_handoff:
                    return True, False
                still_owned = (
                    (down_uid is not None and fresh.assignee_id == down_uid)
                    or WorkflowService._emails_same_agent_identity(
                        fresh.assignee_email, down_email,
                    )
                )
                moved = False
                if still_owned:
                    moved = clear_assignment_to_group_queue(
                        fresh,
                        reason='routability_hold',
                        save=True,
                    )
                    if moved:
                        # PLS-1302: stamp the cycle guard so the PLS-188
                        # status-lane normalizer leaves this item group-queued
                        # for the dwell window instead of re-materializing a
                        # concrete owner on the next sweep tick (the
                        # clear→normalize feedback loop).
                        if WorkflowService._stamp_routability_hold_guard(fresh):
                            fresh.save(update_fields=['metadata', 'updated_at'])
                            # PLS-1337: alert exactly once per breaker episode —
                            # the guard engaged (MAX_CYCLES clears within the
                            # sliding window) and this is the first clear since.
                            _rg = ((getattr(fresh, 'metadata', None) or {})
                                   .get('routability_hold_guard') or {})
                            if _rg.get('breaker_engaged_at') and not _rg.get('breaker_alerted_at'):
                                _rg['breaker_alerted_at'] = _rg['breaker_engaged_at']
                                _meta = dict(getattr(fresh, 'metadata', None) or {})
                                _meta['routability_hold_guard'] = _rg
                                fresh.metadata = _meta
                                fresh.save(update_fields=['metadata', 'updated_at'])
                                _create_system_comment_deduped(
                                    fresh,
                                    'PLS-1302 loop breaker engaged: this item '
                                    'has been cleared-and-renormalized '
                                    f"{WorkflowService.ROUTABILITY_HOLD_MAX_CYCLES}+ "
                                    'times within the sliding window; holding '
                                    'it group-queued instead of churning '
                                    'ownership (PLS-1337). A real workflow '
                                    'transition or manual assignment '
                                    're-resolves ownership.',
                                )
                        _create_system_comment_deduped(
                            fresh,
                            'Cleared unavailable concrete owner to the '
                            f'**{pod}** group queue while active linked '
                            'blockers remain; no failover lease was issued '
                            '(PLS-524 / PLS-204).',
                        )
                return True, moved

        def _clear_linked_blocked_for_down_sources():
            cleared = 0
            for down_email, down_uid in down:
                if WorkflowService._is_human_account(down_email):
                    continue
                if _soft_unavailable_owner_protected(down_email):
                    continue
                for candidate in drain_all_items(
                    down_uid, down_email, pod, roster_org_id,
                ):
                    handled, moved = _clear_linked_blocked_down_owner(
                        candidate, down_email, down_uid,
                    )
                    if handled and moved:
                        cleared += 1
            return cleared

        if not members:
            # PLS-524: a sole-member/all-down team still has one safe mutation:
            # release linked-blocked work from the hard-down concrete owner. Do
            # this before the no-peer exit because this path never needs a target.
            blocked_clears = _clear_linked_blocked_for_down_sources()
            if blocked_clears:
                details[detail_key] = {
                    'moves': blocked_clears,
                    'load_after': {},
                }
                total_moves += blocked_clears
            # PLS-881: an EMPTY-tier roster row (e.g. a same-named org-2 shadow
            # pod with tiers=[[]]) has no members and no down identities — it is
            # not a stranded-work situation and must not emit a misleading
            # "no live member" verdict on every sweep run. Only log when there
            # are actually down identities whose work cannot be drained.
            if down:
                logger.info(
                    "sweep_rebalance_pods: pod '%s' has no live member — linked-blocked "
                    "hard-down owners were released; no replacement leases are possible",
                    pod,
                )
            continue

        # PLS-157: determine which active members are "native" to this pod vs
        # cross-team failover agents. A "native" member is either (a) in tier 0
        # of THIS pod, or (b) not in tier 0 of ANY other pod (neutral failover).
        # A "cross-team" member (e.g. hiro: tier-0 architecture, tier-1 devops)
        # must not RECEIVE devops tasks in the skew loop when native peers exist —
        # routing devops work to an architecture agent causes capability-mismatch
        # bounce loops. The skew loop / shed prefer native targets and only fall
        # back to cross-team members when no native member is under-cap.
        _pod_tier0_lower = {e.lower() for e in (_tiers[0] if _tiers else [])}
        _cross_pod_tier0: set = set()
        for _other_entry in roster_entries.values():
            _other_pod = _other_entry['team_name']
            _other_emails = _other_entry['tiers']
            if (
                _other_pod == pod
                or _other_entry['organization_id'] != roster_org_id
            ):
                continue
            _other_tiers = WorkflowService._roster_tiers(_other_emails)
            if _other_tiers:
                _cross_pod_tier0 |= {e.lower() for e in _other_tiers[0]}

        def _is_native_target(email: str) -> bool:
            el = email.lower()
            return el in _pod_tier0_lower or el not in _cross_pod_tier0

        def _prefer_native(candidates: list) -> list:
            """Return native-only sublist if non-empty, else the full list."""
            native = [c for c in candidates if _is_native_target(c[1])]
            return native if native else candidates

        # PLAT-390: balance on active (hands-on); inflight is the runaway bound.
        active_load = {
            uid: active_qs(uid, e, roster_org_id).count()
            for e, uid in members
        }
        inflight_load = {
            uid: workable_qs(uid, e, roster_org_id).count()
            for e, uid in members
        }
        moves = 0

        # ── PLAT-236 drain (b), the backstop: assign group-only QUEUED tasks
        # (assignment_group=pod, no concrete assignee) to under-cap members,
        # least-loaded first, highest-priority task first. Catches whatever the
        # completion-hook missed (cap raised, agent restarted, hook error).
        # Also the queue's observability point: logs depth + oldest age, and
        # age-flags the oldest item past the SLA — that age IS the capacity-
        # deficit signal feeding the PLAT-244 hire trigger.
        _cap_enforced_pod = (
            roster_entry['wip_enforced']
            or WorkflowService._cap_applies(pod)
        )
        queued = (
            []
            if scoped_aliases
            else WorkflowService._queued_tasks_for_team(
                pod,
                organization_id=roster_org_id,
            )
        )
        if queued:
            for it in list(queued):
                # PLAT-9181/9182: skip items whose finding identity already has
                # an active or terminal record — the rebalance pull must not
                # re-pull already-triaged duplicates (the 09-14x incident: two
                # redundant disposition cycles on the same CVE).
                _identity = _finding_identity_from_item(it)
                if _identity and _finding_identity_has_record(it, _identity):
                    logger.info(
                        'rebalance: skipping %s — finding identity %s already has a record (plat9181)',
                        getattr(it, 'item_key', getattr(it, 'pk', '?')), _identity,
                    )
                    continue
                # PLS-1365: do not force-pull the same item again inside the
                # boundary window unless its status changed since the last pull.
                if WorkflowService._force_pull_blocked_by_boundary(it):
                    queued.remove(it)
                    continue
                if WorkflowService._routability_enforced():
                    # PLS-278: use the composed routability predicate for queue drain
                    # target selection, so rebalancer pulls obey the same staffed ∧
                    # capable ∧ live ∧ under-cap contract as direct assignment.
                    # PLS-1433: also require the shared slot verdict. The routability
                    # band check can still see headroom the PLS-509 cap rejects.
                    under = []
                    candidate_reasons = []
                    for e, uid in members:
                        if not WorkflowService._email_in_current_team_active_tier(pod, e, item=it):
                            continue
                        verdict = WorkflowService._seat_routability_verdict(
                            it, pod, e, intent='new_assignment', enforce_cap=_cap_enforced_pod,
                            user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                        )
                        if not WorkflowService._seat_assignment_allowed(verdict):
                            candidate_reasons.append((e, verdict.reasons))
                            continue
                        if _cap_enforced_pod and not WorkflowService._assignment_ignores_wip_cap(it):
                            seat_full = WorkflowService._slot_at_capacity(
                                it, pod, e, user_id=uid,
                            )
                            if seat_full:
                                candidate_reasons.append((e, ('over_capacity',)))
                                continue
                        under.append((WorkflowService._route_load_key(pod, active_load[uid], inflight_load[uid], 0), e, uid))
                    if not under:
                        WorkflowService._record_routability_hold(
                            it, pod, sorted({r for _e, rs in candidate_reasons for r in (rs or [])}) or ['no_candidate_evidence'],  # PLS-721: empty = no seat scored, never fabricate seat_not_live
                            source='rebalance-capacity-pull', candidate_reasons=candidate_reasons,
                        )
                        break
                else:
                    # PLS-380: PLS-258 capability matching must apply even when
                    # the broader PLS-278 routability predicate is still in
                    # shadow mode. The queue-drain path used to check only
                    # status-role/capacity and could pull `needs:kubeconfig`
                    # work onto a sandboxed/tokenless peer.
                    _required_caps = WorkflowService._task_required_capabilities(it)
                    _capability_skips = []
                    candidate_reasons = []
                    under = []
                    for e, uid in members:
                        if not WorkflowService._email_in_current_team_active_tier(pod, e, item=it):
                            continue
                        verdict = WorkflowService._seat_routability_verdict(
                            it, pod, e, intent='new_assignment', enforce_cap=_cap_enforced_pod,
                            user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                        )
                        if not WorkflowService._seat_assignment_allowed(verdict):
                            candidate_reasons.append((e, verdict.reasons))
                            continue
                        if not WorkflowService._member_can_execute_from_status(e, it, user_id=uid):
                            continue
                        if not WorkflowService._seat_satisfies_required_capability(e, _required_caps, item=it):
                            _capability_skips.append(e)
                            continue
                        if _cap_enforced_pod and not WorkflowService._assignment_ignores_wip_cap(it):
                            # PLS-1433: shared capacity verdict — the same
                            # assigned-ready/active counts the reservation path
                            # enforces, so a seat the cap would reject is never
                            # a pull target. Without a roster config, defer to
                            # the legacy process-wide counters (PLS-380).
                            seat_full = WorkflowService._slot_at_capacity(
                                it, pod, e, user_id=uid,
                            )
                            if seat_full is None:
                                if WorkflowService._at_capacity(active_load[uid], inflight_load[uid]):
                                    continue
                            elif seat_full:
                                continue
                        under.append((
                            WorkflowService._route_load_key(pod, active_load[uid], inflight_load[uid], 0),
                            e, uid,
                        ))
                    if not under:
                        if candidate_reasons:
                            WorkflowService._record_routability_hold(
                                it, pod,
                                sorted({r for _e, rs in candidate_reasons for r in (rs or [])}),
                                source='rebalance-capacity-pull',
                                candidate_reasons=candidate_reasons,
                            )
                        if _required_caps and _capability_skips:
                            WorkflowService._record_capability_routing_hold(
                                it, pod, _required_caps, source='rebalance-capacity-pull',
                            )
                        break
                _, to_email, to_uid = min(under)
                write_verdict = WorkflowService._seat_routability_verdict(
                    it, pod, to_email, intent='new_assignment', enforce_cap=_cap_enforced_pod,
                    user_id=to_uid, active_load=active_load[to_uid], inflight_load=inflight_load[to_uid],
                )
                if not WorkflowService._seat_assignment_allowed(write_verdict):
                    WorkflowService._record_routability_hold(
                        it, pod, write_verdict.reasons,
                        source='rebalance-capacity-write-guard',
                        attempted_email=to_email,
                        candidate_reasons=[(to_email, write_verdict.reasons)],
                    )
                    break
                capacity_denied = False
                try:
                    # _queued_tasks_for_team deliberately recognizes legacy
                    # aliases, but a concrete assignment must persist the
                    # canonical roster key or the database membership guard
                    # correctly rejects it.  Keep each queue pull isolated so
                    # one corrupt/legacy row cannot abort every later pod.
                    # PLS-912: mint/commit the reservation before the Item
                    # mutation transaction.  Nesting the capacity-row mutex in
                    # this outer atomic block kept it held through Item.save's
                    # wider routing/substrate machinery and stalled unrelated
                    # Start Progress requests for the same seat.
                    from .services.workflow_service import CAPACITY_UNAVAILABLE
                    admitted = WorkflowService._reserve_wip_slot(
                        it, to_uid, to_email,
                    )
                    if admitted is False or admitted == CAPACITY_UNAVAILABLE:
                        capacity_denied = True
                        capacity_deny_reason = (
                            CAPACITY_UNAVAILABLE
                            if admitted == CAPACITY_UNAVAILABLE
                            else 'over_capacity'
                        )
                    else:
                        capacity_deny_reason = 'over_capacity'
                        with transaction.atomic():
                            it.assignment_group = pod
                            it.assign({'id': to_uid, 'email': to_email}, save=False)
                            it._activity_actor = (0, 'system@pulse')
                            it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): rebalance/failover sweep
                            it.save(update_fields=[
                                'assignment_group', 'assignee_id',
                                'assignee_email', 'updated_at',
                            ])
                except Exception:
                    WorkflowService._release_unconsumed_wip_reservation(it)
                    logger.exception(
                        'sweep_rebalance_pods: capacity pull write failed '
                        'item=%s team=%s assignee=%s',
                        it.item_key or it.pk, pod, to_email,
                    )
                    try:
                        it.refresh_from_db()
                        WorkflowService._record_routability_hold(
                            it, pod, ['assignment_write_failed'],
                            source='rebalance-capacity-write',
                            attempted_email=to_email,
                        )
                    except Exception:
                        logger.exception(
                            'sweep_rebalance_pods: could not record capacity '
                            'pull write hold for item=%s',
                            it.item_key or it.pk,
                        )
                    continue
                if capacity_denied:
                    deny_reason = locals().get('capacity_deny_reason', 'over_capacity')
                    WorkflowService._record_routability_hold(
                        it, pod, [deny_reason],
                        source='rebalance-capacity-reservation',
                        attempted_email=to_email,
                        candidate_reasons=[(to_email, (deny_reason,))],
                    )
                    continue
                try:
                    Comment.objects.create(item=it, author_id=0, author_email='system@pulse',
                        content=(f'Pulled from the **{pod}** capacity queue to **{to_email}** '
                                 f'(rebalance sweep found headroom — PLAT-236/PLAT-867).'))
                except Exception:
                    pass
                queued.remove(it)
                if it.status not in review_pipeline:
                    active_load[to_uid] += 1
                inflight_load[to_uid] += 1
                moves += 1
            if queued and _cap_enforced_pod:
                oldest = min(queued, key=lambda i: i.created_at)
                age_h = (timezone.now() - oldest.created_at).total_seconds() / 3600
                sla_h = float(os.environ.get('PULSE_QUEUE_AGE_SLA_HOURS', '96'))
                logger.warning(
                    f"PLAT-236 capacity queue '{pod}': depth={len(queued)}, "
                    f"oldest={oldest.item_key} age={age_h:.1f}h (SLA {sla_h}h) — "
                    f"all members at capacity (active_cap={active_cap}, inflight_cap={inflight_cap})")
                if age_h > sla_h:
                    meta = dict(oldest.metadata or {})
                    if not meta.get('capacity_queue_sla_flagged'):
                        meta['capacity_queue_sla_flagged'] = timezone.now().isoformat()
                        oldest.metadata = meta
                        oldest.save(update_fields=['metadata'])
                        try:
                            Comment.objects.create(item=oldest, author_id=0, author_email='system@pulse',
                                content=(f'⏰ Capacity-deficit signal: queued in the **{pod}** capacity queue for '
                                         f'{age_h:.0f}h (SLA {sla_h:.0f}h) with all members at capacity '
                                         f'(active≥{active_cap} or in-flight≥{inflight_cap}). '
                                         f'Capacity — not prioritization — is the binding constraint; escalate per '
                                         f'the PLAT-237 starvation policy / PLAT-244 hire trigger. [PLAT-236]'))
                        except Exception:
                            pass

        # kei P2: detect members removed from the DB roster who still have work
        # assigned under this pod's assignment_group. They won't appear in
        # _flat_roster(emails) so they'd never reach `down` — their work strands.
        # Append them to `down` now so the failover drain below picks them up.
        # PLAT-355 aoi P3: case-insensitive ghost detection. DB emails may differ
        # in case from roster emails — exact-match exclude() would miss them.
        # sora P1: must include ALL roster members (flat across all tiers) — not just
        # `members` (active-tier only) + `down` (unavailable). Available failover-tier
        # members are in neither set, so they'd be falsely detected as ghosts and their
        # work drained away. Use the full flat roster as the "known pod members" set.
        effective_emails_lower = {e.lower() for e in WorkflowService._flat_roster(emails)}
        _all_pod_emails = list(
            _scope_items(Item.objects
            .filter(assignment_group=pod, assignee_email__isnull=False)
            .exclude(status__in=terminal_statuses)
            , roster_org_id)
            .values_list('assignee_email', flat=True)
            .distinct()
        )
        ghost_emails = [
            ge for ge in _all_pod_emails
            # Empty string is the canonical persisted representation for a
            # team-owned queue item with no concrete assignee.  It is not a
            # departed runtime identity and must never enter the failover
            # drain as a synthetic "down agent".
            if ge and ge.strip()
            and ge.lower() not in effective_emails_lower
            and not WorkflowService._is_human_account(ge)
            and (
                not scoped_aliases
                or WorkflowService._agent_email_aliases(ge) & scoped_aliases
            )
        ]
        for ge in ghost_emails:
            try:
                info = user_service.get_user_by_email(ge)
                gid = info.get('id') if info else None
                if gid and str(gid).isdigit():
                    down.append((ge, int(gid)))
                else:
                    # Identity-drifted legacy rows can be email-owned by an
                    # address that no longer exists in shizuha-id (for example
                    # old provider/account aliases). Still drain by
                    # assignee_email; _agent_load_q(email=ge) covers this path.
                    down.append((ge, None))
            except Exception:
                down.append((ge, None))

        # ── Failover drain: any unavailable non-human member's non-terminal work
        # is reassigned to the lightest available peer so an offline/quota-limited
        # owner never strands its pod. Earlier versions only full-drained explicitly
        # disabled agents or superseded primary tiers and left blocked/in_review/
        # open-PR in_progress on same-tier unavailable members. Provider quota
        # exhaustion now deliberately reports those agents as "unavailable"; treat
        # that state as a full drain source and let the normal same-team/cap gates
        # decide which live peer receives the work. Capacity queues still apply
        # to new/unowned work, but already-owned work must not remain pinned to an
        # unavailable concrete assignee when a live same-team member exists.
        # Also drains work from members removed from the DB roster (ghost members).
        # Fail over only to an under-cap target.  Moving work from an unavailable
        # owner to a saturated peer does not create execution capacity; it merely
        # destroys the current ownership/review context and overloads another seat.
        # Accepted PLS-277 therefore treats under-capacity as part of routability
        # for every new concrete assignment, including failover.  If every live
        # same-team peer is full, preserve the current owner and retry after a
        # capacity-change event or the next reconciliation sweep.
        # ── PLS-198 (F3): resolve `down` to one entry per RUNTIME IDENTITY and
        # never treat an alias of a down identity as a live failover candidate.
        # The two banto spellings (@shizuha.com / @agents.shizuha.io) resolve to
        # the same shizuha-id uid; without this fold, the drain repeatedly
        # "failed over" work from one alias to the other — the same unavailable
        # runtime — and re-fired every tick (BKST-1).
        _down_uids = {duid for _, duid in down if duid is not None}
        _down_alias_emails: set = set()
        _deduped_down, _seen_down_idents = [], set()
        for de, duid in down:
            _ident = (('uid', duid) if duid is not None
                      else ('email', frozenset(WorkflowService._agent_email_aliases(de))))
            if _ident in _seen_down_idents:
                continue
            _seen_down_idents.add(_ident)
            _deduped_down.append((de, duid))
            _down_alias_emails |= WorkflowService._agent_email_aliases(de)
        down = _deduped_down
        _live_members = [
            (e, uid) for e, uid in members
            if uid not in _down_uids
            and not (WorkflowService._agent_email_aliases(e) & _down_alias_emails)
        ]
        if len(_live_members) != len(members):
            logger.warning(
                "sweep_rebalance_pods: excluded %s from pod '%s' failover candidates — "
                "alias of a down identity (PLS-198 F3)",
                sorted({e for e, _ in members} - {e for e, _ in _live_members}), pod)
        members = _live_members

        if not members:
            blocked_clears = _clear_linked_blocked_for_down_sources()
            if blocked_clears:
                details[detail_key] = {
                    'moves': blocked_clears,
                    'load_after': {},
                }
                total_moves += blocked_clears
            # PLS-881: an EMPTY-tier roster row (e.g. a same-named org-2 shadow
            # pod with tiers=[[]]) has no members and no down identities — it is
            # not a stranded-work situation and must not emit a spurious
            # "no live peer" verdict on every sweep run. Only warn when there
            # are actually down identities whose work cannot be drained.
            if down:
                logger.warning(
                    "sweep_rebalance_pods: pod '%s' has no live peer distinct from its down "
                    "identities — leaving work in place rather than self-assigning (PLS-198 F3)",
                    pod)
            continue

        disabled_emails = WorkflowService._disabled_agent_emails()
        for de, duid in down:
            if WorkflowService._is_human_account(de):
                continue
            # Full-drain hard-down agents. PLS-204 + PLS-494 split target eligibility
            # from failover-drain eligibility: provider/quota/capacity-limited AND
            # stale-`offline` owners remain excluded from NEW assignments, but recent
            # authenticated Pulse activity (a fresh pulse:agent_hb, which an offline
            # process cannot produce) proves the current owner is alive enough to keep
            # already-owned work. Genuinely hard-off states still drain: operator-
            # disabled/service-denied, `wedged` (polls but zero work — heartbeat does
            # not refute the wedge), and any down owner with NO recent heartbeat.
            if _soft_unavailable_owner_protected(de):
                logger.info(
                    "sweep_rebalance_pods: protected soft-unavailable owner %s in pod '%s' "
                    "from failover drain because recent Pulse heartbeat is inside grace",
                    de, pod)
                continue
            # PLAT-390: pass email=de so movable_items counts email-primary ownership
            # consistently with the active/inflight load dicts (the email-primary match).
            picks = drain_all_items(duid, de, pod, roster_org_id)
            for it in picks:
                # PLAT-940: safety guard — never re-route terminal-status tasks even
                # if they slipped through the queryset exclusion (race condition where
                # a task was completed after the query ran, or a workflow-specific slug
                # not yet in the static exclusion set e.g. 'applied' on change-request).
                if _is_terminal_status(it.status):
                    logger.info(
                        "PLAT-940: skipping terminal-status item %s (%s) from failover drain for %s",
                        it.item_key or it.pk, it.status, de,
                    )
                    continue
                # PLS-488 / HIVE-626: a direct Architecture assignee on an
                # architecture-review item must not be replaced by an
                # Engineering implementer while that assignee is unavailable.
                # PLS-421 AC1: substitute another resident Architecture-seat
                # peer when one exists; AC2: hold fail-loud when none does,
                # never falling back to the workflow's generic team.
                direct_review_failover = False
                if _hive626_direct_review_hold(it, de, pod):
                    _sub_uid, _sub_email = _resolve_direct_review_failover_target(
                        it, de)
                    if not _sub_email:
                        logger.warning(
                            "HIVE-626: holding %s — architecture-review direct assignee %s "
                            "is unavailable and no compatible Architecture substitute exists; "
                            "refusing Engineering implementer failover",
                            it.item_key or it.pk, de,
                        )
                        WorkflowService._record_routability_hold(
                            it, pod, ['direct_review_authority'],
                            source='hive-626-direct-review',
                            attempted_email=de,
                        )
                        continue
                    to_uid, to_email = _sub_uid, _sub_email
                    direct_review_failover = True
                _linked_blocked_handled, _linked_blocked_moved = (
                    _clear_linked_blocked_down_owner(it, de, duid)
                )
                if _linked_blocked_handled:
                    if _linked_blocked_moved:
                        moves += 1
                    continue
                # PLAT-8821: no actionable work left — a candidate whose linked
                # PR set is fully merged (no open PR) must not chain failover
                # hops (PLAT-8787: 4-deep chain on an already-merged PR). The
                # item's own workflow (QA/merge transitions) owns the next step.
                if not WorkflowService._item_failover_actionable(it):
                    logger.info(
                        "PLAT-8821: skipping failover for %s from %s — linked PR "
                        "merged with no open PR; no actionable work left",
                        it.item_key or it.pk, de,
                    )
                    continue
                # PLS-411 live gate: the roster snapshot that put ``de`` in
                # ``down`` may be stale by the time this item is drained.  A
                # signed explicit merge handoff is stronger than that snapshot:
                # re-check the target live and preserve it when available.  If
                # it is still unavailable, route through the merge-lane SoD
                # resolver (which clears the pin and excludes current-head
                # reviewers) instead of the generic failover candidate picker.
                _effective_status = WorkflowService._effective_status_lane_status(it)
                _explicit_merge_handoff = (
                    WorkflowService._explicit_merge_handoff_email(it)
                    if WorkflowService._status_lane_assignment_group(_effective_status) == 'merge'
                    else ''
                )
                if _explicit_merge_handoff:
                    if WorkflowService._is_agent_available_for_assignment(
                        _explicit_merge_handoff
                    ):
                        logger.info(
                            'PLS-411: preserving explicit merge handoff for %s on %s '
                            'after live availability re-check',
                            it.item_key or it.pk, _explicit_merge_handoff,
                        )
                        continue
                    _handoff_fields = []
                    _lane, _handoff_changed = WorkflowService._enforce_status_lane_owner(
                        it, _effective_status, _handoff_fields,
                    )
                    if _handoff_changed and _handoff_fields:
                        it._activity_actor = (0, 'system@pulse')
                        it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): merge-handoff lane re-enforce sweep
                        suppress_bulk_assignment_dm(it)
                        it.save(update_fields=list(dict.fromkeys(
                            _handoff_fields + ['updated_at']
                        )))
                        _hold = ((it.metadata or {}).get('merge_sod_hold') or {})
                        if _hold:
                            _create_system_comment_deduped(
                                it,
                                'Merge SoD hold during unavailable explicit-handoff '
                                'failover: no distinct current-head merger is available; '
                                f'reviewers=`{",".join(_hold.get("current_head_reviewers") or []) or "unknown"}`; '
                                'task remains on the `merge` group queue (PLS-411).',
                            )
                        total_moves += 1
                    continue
                # PLS-233: a task explicitly direct-assigned (with a reason) to a
                # decision-owner must never bounce back to the symptom-team pool. If
                # the pinned owner is hard-down, fail over within the pinned decision
                # team only; if no decision-team peer is available, leave it in place.
                pinned_failover = False
                if not direct_review_failover:
                    pinned_failover = _is_decision_pinned(it)
                    if pinned_failover:
                        to_uid, to_email = _resolve_decision_pin_failover_target(it, de)
                        if not to_email:
                            logger.warning(
                                "PLS-233: decision-pinned item %s could not fail over within "
                                "pinned team %r from down owner %s — leaving in place",
                                it.item_key or it.pk, _decision_pinned_team(it), de,
                            )
                            continue
                # Require an under-cap live peer. PLS-157: within the eligible
                # list, prefer native members before cross-team failover members.
                # PLS-198 (F3): belt-and-braces per-candidate identity check on top
                # of the members-level exclusion above.
                if not pinned_failover and not direct_review_failover:
                    if WorkflowService._routability_enforced():
                        live_candidates = []
                        candidate_reasons = []
                        for e, uid in members:
                            if uid == duid or WorkflowService._emails_same_agent_identity(e, de):
                                continue
                            verdict = WorkflowService._seat_routability_verdict(
                                it, pod, e, intent='new_assignment', enforce_cap=False,
                                user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                            )
                            if WorkflowService._seat_assignment_allowed(verdict):
                                live_candidates.append((WorkflowService._route_load_key(pod, active_load[uid], inflight_load[uid], 0), e, uid))
                            else:
                                candidate_reasons.append((e, verdict.reasons))
                        if not live_candidates:
                            logger.warning(
                                "sweep_rebalance_pods: no routable live peer distinct from down identity "
                                "%s in pod '%s' — leaving its work in place (PLS-278)", de, pod)
                            WorkflowService._record_routability_hold(
                                it, pod, sorted({r for _e, rs in candidate_reasons for r in (rs or [])}) or ['no_candidate_evidence'],  # PLS-721: empty = no seat scored, never fabricate seat_not_live
                                source='failover-target', candidate_reasons=candidate_reasons,
                            )
                            break
                    else:
                        # PLS-380: failover is another concrete-assignee changer;
                        # it must honor explicit/derived capability requirements
                        # even while composed routability enforcement is shadowed.
                        _required_caps = WorkflowService._task_required_capabilities(it)
                        _capability_skips = []
                        candidate_reasons = []
                        live_candidates = []
                        for e, uid in members:
                            if uid == duid or WorkflowService._emails_same_agent_identity(e, de):
                                continue
                            verdict = WorkflowService._seat_routability_verdict(
                                it, pod, e, intent='new_assignment', enforce_cap=False,
                                user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                            )
                            if not WorkflowService._seat_assignment_allowed(verdict):
                                candidate_reasons.append((e, verdict.reasons))
                                continue
                            if not WorkflowService._member_can_execute_from_status(e, it, user_id=uid):
                                continue
                            if not WorkflowService._seat_satisfies_required_capability(e, _required_caps, item=it):
                                _capability_skips.append(e)
                                continue
                            live_candidates.append((
                                WorkflowService._route_load_key(pod, active_load[uid], inflight_load[uid], 0),
                                e, uid,
                            ))
                        if not live_candidates:
                            if candidate_reasons:
                                WorkflowService._record_routability_hold(
                                    it, pod,
                                    sorted({r for _e, rs in candidate_reasons for r in (rs or [])}),
                                    source='failover-target', candidate_reasons=candidate_reasons,
                                )
                            if _required_caps and _capability_skips:
                                WorkflowService._record_capability_routing_hold(
                                    it, pod, _required_caps, source='failover-target',
                                    attempted_email=', '.join(sorted(_capability_skips)),
                                )
                            logger.warning(
                                "sweep_rebalance_pods: no live peer distinct from down identity "
                                "%s in pod '%s' — leaving its work in place (PLS-198 F3)", de, pod)
                            break
                    # Capacity is part of the target's routability contract, not
                    # merely a preference.  Re-evaluate it through the composed
                    # predicate so organization-local TeamRoster caps and the
                    # legacy active/in-flight caps agree with every other writer.
                    under = []
                    for row in live_candidates:
                        if not _cap_enforced_pod:
                            under.append(row)
                            continue
                        _load_key, _candidate_email, _candidate_uid = row
                        capacity_verdict = WorkflowService._seat_routability_verdict(
                            it,
                            pod,
                            _candidate_email,
                            intent='new_assignment',
                            enforce_cap=True,
                            user_id=_candidate_uid,
                            active_load=active_load[_candidate_uid],
                            inflight_load=inflight_load[_candidate_uid],
                        )
                        if 'over_capacity' not in set(capacity_verdict.reasons or ()):
                            under.append(row)
                    if _cap_enforced_pod and not under:
                        logger.warning(
                            "sweep_rebalance_pods: aborting failover for %s from %s "
                            "in pod '%s' — all routable live peers are at capacity; "
                            "preserving current owner (PLAT-5032)",
                            it.item_key or it.pk, de, pod,
                        )
                        continue
                    _, to_email, to_uid = min(_prefer_native(under))
                # PLS-198 (F4): per-item idempotency under a row lock. A stacked/
                # concurrent sweep run (or an earlier tick whose reassignment
                # persisted) must observe the CURRENT owner and no-op: re-fetch
                # FOR UPDATE, confirm the item is still owned by the down identity
                # and non-terminal, and skip no-op reassignments to the current
                # owner. This is what stops the every-tick re-fire.
                failback_receipt = None
                with transaction.atomic():
                    fresh = Item.objects.select_for_update().filter(pk=it.pk).first()
                    if fresh is None or _is_terminal_status(fresh.status):
                        continue
                    _fresh_status = (fresh.status or '').strip().lower()
                    _admin_blocked = (
                        _fresh_status == 'blocked'
                        and (fresh.metadata or {}).get('blocked_via') == 'admin_ops_escalation'
                    )
                    if _fresh_status == 'scheduled' or _admin_blocked:
                        logger.info(
                            "PLS-250/PLS-524: skipping %s from failover drain for %s because "
                            "status %s is non-actionable until unblock/reschedule",
                            fresh.item_key or fresh.pk, de, fresh.status,
                        )
                        continue
                    _still_owned = (
                        (duid is not None and fresh.assignee_id == duid)
                        or WorkflowService._emails_same_agent_identity(fresh.assignee_email, de)
                    )
                    if not _still_owned:
                        continue  # another worker/tick already failed this over
                    # The owner's authenticated activity may land after the pod
                    # snapshot and before this item-level write.  Re-check at the
                    # locked mutation boundary so one stale verdict cannot drain
                    # the rest of a live agent's queue while the agent is actively
                    # using Pulse.  Hard operator/runtime OFF sources still return
                    # False from this predicate and continue to evacuate.
                    if _soft_unavailable_owner_protected(de):
                        logger.info(
                            'PLAT-5032: aborting stale failover write for %s from %s; '
                            'newer authenticated Pulse activity now protects the owner',
                            fresh.item_key or fresh.pk, de,
                        )
                        continue
                    if _auto_failover_recently_recorded(fresh):
                        logger.info(
                            "sweep_rebalance_pods: skipped failover for %s from %s — "
                            "recent auto-failover comment is still inside cooldown (PLS-235)",
                            fresh.item_key or fresh.pk, de,
                        )
                        continue
                    if (fresh.assignee_id == to_uid
                            or WorkflowService._emails_same_agent_identity(fresh.assignee_email, to_email)):
                        continue  # no-op reassignment — already on the target identity
                    # PLS-421 AC1: the write-guard must score the substitute seat
                    # against the seat's OWN roster team, not the item's
                    # in-progress assignment_group. An Architecture reviewer seat
                    # is never a member of the implementation pod, so scoring
                    # nova against `engineering` always yields
                    # `seat_not_team_member` and silently defeated substitution
                    # while AC2's fail-loud hold looked correct (premerge run
                    # 9574 census: resolver returned nova, verdict rejected the
                    # write on membership). Live/capability/capacity checks are
                    # unchanged — only the roster-team context of the verdict.
                    _seat_team = pod
                    if direct_review_failover:
                        _seat_team = (
                            _direct_review_failover_seat_team(de) or pod
                        )
                        # Re-scope the lane BEFORE the seat verdict and the WIP
                        # reservation mint: the reservation records the team it
                        # was minted against and Item.save's consumption check
                        # requires team == reservation.assignment_group — a
                        # post-mint rescope reads as
                        # `wip_reservation_target_mismatch` (premerge run 9580).
                        # Rescoped here, the reservation, the PLAT-4213
                        # membership fence, and the PLS-408 reconciliation all
                        # see the same Architecture lane.
                        fresh.assignment_group = _seat_team
                    _group_changed = direct_review_failover
                    write_verdict = WorkflowService._seat_routability_verdict(
                        fresh,
                        _seat_team,
                        to_email,
                        intent='new_assignment',
                        enforce_cap=_cap_enforced_pod,
                        user_id=to_uid,
                        active_load=WorkflowService._agent_active_load(
                            to_uid, to_email,
                        ),
                        inflight_load=WorkflowService._agent_workable_load(
                            to_uid, to_email,
                        ),
                    )
                    if (
                        not WorkflowService._seat_assignment_allowed(write_verdict)
                        or 'over_capacity' in set(write_verdict.reasons or ())
                    ):
                        WorkflowService._record_routability_hold(
                            fresh, pod, write_verdict.reasons,
                            source='failover-write-guard', attempted_email=to_email,
                            candidate_reasons=[(to_email, write_verdict.reasons)],
                        )
                        continue
                    admitted = WorkflowService._reserve_wip_slot(
                        fresh, to_uid, to_email,
                        # PLS-421 AC1: the direct-review substitution crosses
                        # lanes (engineering -> architecture). Reserve against
                        # the EFFECTIVE destination — the same override the
                        # routing-authority REROUTE path uses — or Item.save()'s
                        # reservation consumption compares the seat team against
                        # a mint-time engineering reservation and rejects with
                        # `wip_reservation_target_mismatch` (premerge run 9580).
                        assignment_group_override=_seat_team,
                    )
                    if admitted is False:
                        logger.warning(
                            "sweep_rebalance_pods: aborting failover for %s from %s "
                            "in pod '%s' — target %s lost the atomic WIP slot "
                            "reservation; preserving current owner (PLAT-5032)",
                            fresh.item_key or fresh.pk, de, pod, to_email,
                        )
                        WorkflowService._record_routability_hold(
                            fresh,
                            pod,
                            ['over_capacity'],
                            source='failover-write-capacity-reservation',
                            attempted_email=to_email,
                            candidate_reasons=[(to_email, ('over_capacity',))],
                        )
                        continue
                    original_assignee_id = fresh.assignee_id
                    fresh.assign({'id': to_uid, 'email': to_email}, save=False)
                    fresh._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): failover sweep
                    update_fields = ['assignee_id', 'assignee_email', 'updated_at']
                    if not (fresh.assignment_group or '').strip():
                        fresh.assignment_group = pod
                        _group_changed = True
                    if _group_changed:
                        update_fields.append('assignment_group')
                    # A stale marker must never survive a generic failover with
                    # an assignee it no longer authenticates.  Otherwise a later
                    # sweep can resurrect the old pin or leave contradictory
                    # direct-assignment evidence in metadata.
                    _fresh_meta = dict(fresh.metadata or {})
                    if 'explicit_merge_handoff' in _fresh_meta:
                        _fresh_meta.pop('explicit_merge_handoff', None)
                        fresh.metadata = _fresh_meta
                        update_fields.append('metadata')
                    if pinned_failover and _repin_decision_assignment(fresh, to_email):
                        update_fields.append('metadata')
                    # PLAT-5032: soft/stale failovers retain a structured,
                    # immutable restoration receipt.  A later authenticated
                    # activity event can fail back only while the item remains
                    # on THIS automatic target, its status is unchanged, and no
                    # newer assignment/status activity superseded the receipt.
                    # Deliberate hard-OFF failovers never arm automatic failback.
                    if _agent_unavailability_activity_refutable(de):
                        import uuid

                        failback_receipt = {
                            'schema': 'pulse.auto-failover-restore.v1',
                            'state': 'active',
                            'receipt_id': str(uuid.uuid4()),
                            'from_email': (de or '').strip().lower(),
                            'from_id': original_assignee_id or duid,
                            'to_email': (to_email or '').strip().lower(),
                            'to_id': to_uid,
                            'status': fresh.status,
                            'decision_pinned': bool(pinned_failover),
                            'source_reason': _agent_unavailability_reason(de),
                            'recorded_at': timezone.now().isoformat(),
                        }
                        _fresh_meta = dict(fresh.metadata or {})
                        _fresh_meta['auto_failover_restore'] = failback_receipt
                        fresh.metadata = _fresh_meta
                        if 'metadata' not in update_fields:
                            update_fields.append('metadata')
                    fresh._activity_actor = (0, 'system@pulse')
                    suppress_bulk_assignment_dm(fresh)
                    fresh.save(update_fields=update_fields)
                    if failback_receipt is not None:
                        # The receipt audit is created after Item.save's normal
                        # assignment activity, in the same DB transaction.  At
                        # failback time, any later Assignee/Status activity proves
                        # another writer crossed the boundary and wins over this
                        # stale recovery intent.
                        from .models import ItemActivity

                        ItemActivity.objects.create(
                            item=fresh,
                            action='updated',
                            field_name='Auto Failover Receipt',
                            old_value='',
                            new_value=failback_receipt['receipt_id'],
                            user_id=0,
                            user_email='system@pulse',
                        )
                # Arm the event only after the durable ownership receipt commits.
                # The original owner's next successful authenticated request
                # consumes this hint and runs the row-locked restoration check.
                if failback_receipt is not None:
                    try:
                        from .agent_liveness import mark_agent_failback_pending
                        mark_agent_failback_pending(de)
                    except Exception:
                        logger.exception(
                            'PLAT-5032: could not arm failback event for %s; '
                            'the structured Item receipt remains authoritative',
                            de,
                        )
                # PLS-198 (F5): dedupe backstop — never post an identical
                # auto-failover system comment twice within the window.
                if direct_review_failover:
                    _create_system_comment_deduped(
                        fresh,
                        f'Auto-failover to **{to_email}** — independent Architecture review '
                        f'seat preserved: primary **{de}** is unavailable and the substitute '
                        f'satisfies the architecture-review seat constraints (PLS-421).')
                else:
                    _create_system_comment_deduped(
                        fresh,
                        f'Auto-failover to **{to_email}** — primary **{de}** is unavailable '
                        f'(offline/provider-unavailable/operator-disabled/hibernated).')
                # PLAT-390 (Codex P2): only count it as ACTIVE load if the
                # drained item is hands-on. drain_all_items() also moves
                # review-pipeline work (in_review/in_qa/awaiting_merge), which
                # is in-flight but NOT active — counting it as active falsely
                # marks an otherwise-idle target at ACTIVE_CAP.
                if it.status not in excluded_statuses and it.status not in review_pipeline:
                    active_load[to_uid] = active_load.get(to_uid, 0) + 1
                if it.status not in excluded_statuses:
                    inflight_load[to_uid] = inflight_load.get(to_uid, 0) + 1
                moves += 1

        # Availability-change events reconcile only the affected owner's work.
        # Periodic capacity balancing remains a slow safety-net; it must not be
        # pulled into this event path and move unrelated healthy owners.
        if scoped_aliases:
            if moves:
                details[detail_key] = {
                    'moves': moves,
                    'load_after': {e: active_load[uid] for e, uid in members},
                }
                total_moves += moves
            continue

        movable_by_uid = {
            uid: movable_items(
                uid,
                pod,
                email=e,
                organization_id=roster_org_id,
            )
            for e, uid in members
        }

        # ── PLAT-424: in_progress-overload shed ──
        # The skew loop below evens RELATIVE load and stops once the max-min gap is
        # within `skew`, so a HEALTHY member sitting OVER the active cap can stay
        # over it (the gap to its peers may already be <= skew). Explicitly shed
        # each cap-enforced member's EXCESS active work (active_load - ACTIVE_CAP)
        # down to the cap, onto the lightest UNDER-cap peers. movable_by_uid is
        # already least-progressed-first (move_rank) and EXCLUDES linked-PR
        # in_progress (has_linked_pr) + epics — i.e. we never yank substantial /
        # PR-attached progress. Bounded per member per run to avoid thrash, and we
        # never push a peer over either cap (_at_capacity gate). Cap-enforced pods
        # with a positive active cap only.
        if not _membership_mode and WorkflowService._cap_applies(pod) and active_cap > 0:
            SHED_MAX_PER_MEMBER = 2  # NEXT_BATCH — bound moves/run
            for from_uid in sorted(
                (uid for _, uid in members if active_load[uid] > active_cap),
                key=lambda uid: -active_load[uid],  # worst overload first
            ):
                shed = 0
                while (active_load[from_uid] > active_cap
                       and shed < SHED_MAX_PER_MEMBER
                       and movable_by_uid.get(from_uid)):
                    it = movable_by_uid[from_uid][0]
                    _required_caps = WorkflowService._task_required_capabilities(it)
                    _capability_skips = []
                    candidate_reasons = []
                    under = []
                    for e, uid in members:
                        if uid == from_uid:
                            continue
                        if WorkflowService._at_capacity(active_load[uid], inflight_load[uid]):
                            continue
                        verdict = WorkflowService._seat_routability_verdict(
                            it, pod, e, intent='new_assignment', enforce_cap=True,
                            user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                        )
                        if not WorkflowService._seat_assignment_allowed(verdict):
                            candidate_reasons.append((e, verdict.reasons))
                            continue
                        if not WorkflowService._member_can_execute_from_status(e, it, user_id=uid):
                            continue
                        if not WorkflowService._seat_satisfies_required_capability(e, _required_caps, item=it):
                            _capability_skips.append(e)
                            continue
                        under.append((
                            WorkflowService._route_load_key(pod, active_load[uid], inflight_load[uid], 0),
                            e, uid,
                        ))
                    if not under:
                        if candidate_reasons:
                            WorkflowService._record_routability_hold(
                                it, pod,
                                sorted({r for _e, rs in candidate_reasons for r in (rs or [])}),
                                source='rebalance-overload-shed',
                                candidate_reasons=candidate_reasons,
                            )
                        if _required_caps and _capability_skips:
                            WorkflowService._record_capability_routing_hold(
                                it, pod, _required_caps, source='rebalance-overload-shed',
                                attempted_email=', '.join(sorted(_capability_skips)),
                            )
                        break  # no under-cap peer — leave it rather than push a peer over cap
                    _, to_email, to_uid = min(_prefer_native(under))  # PLS-157: native first
                    it = movable_by_uid[from_uid].pop(0)
                    write_verdict = WorkflowService._seat_routability_verdict(
                        it, pod, to_email, intent='new_assignment', enforce_cap=True,
                        user_id=to_uid, active_load=active_load[to_uid], inflight_load=inflight_load[to_uid],
                    )
                    if not WorkflowService._seat_assignment_allowed(write_verdict):
                        WorkflowService._record_routability_hold(
                            it, pod, write_verdict.reasons,
                            source='rebalance-overload-write-guard', attempted_email=to_email,
                            candidate_reasons=[(to_email, write_verdict.reasons)],
                        )
                        break
                    it.assign({'id': to_uid, 'email': to_email}, save=False)
                    it._activity_actor = (0, 'system@pulse')
                    it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): rebalance sweep
                    suppress_bulk_assignment_dm(it)
                    it.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
                    try:
                        Comment.objects.create(
                            item=it, author_id=0, author_email='system@pulse',
                            content=f'Auto-rebalanced to **{to_email}** '
                                    f'(PLAT-424: shed in-progress overload — owner over the active WIP cap).')
                    except Exception:
                        pass
                    active_load[to_uid] += 1
                    inflight_load[to_uid] += 1
                    active_load[from_uid] -= 1
                    inflight_load[from_uid] -= 1
                    moves += 1
                    shed += 1

        # Hand movable tasks from the heaviest member to the lightest member until
        # the pod's max-minus-min workable-load gap is within `skew`.
        # PLAT-1304: under the membership rebalancer, engineering capacity skew is
        # corrected by granting secondary memberships, NOT by moving implementation
        # tasks between peers. Review/QA/merge are different: their concrete owners
        # are discoverable holders of a team queue. Untouched pipeline items must be
        # balanced after a reviewer recovers or one transient outage leaves the
        # surviving reviewer with the entire lane.
        _pipeline_queue_pod = pod in {'review', 'qa', 'merge'}
        while not _membership_mode or _pipeline_queue_pod:
            balance = (lambda uid: active_load[uid]) if WorkflowService._cap_applies(pod) else (lambda uid: inflight_load[uid])
            ordered = sorted((balance(uid), e, uid) for e, uid in members)
            low_load = ordered[0][0]
            high_load, _, from_uid = ordered[-1]
            if high_load - low_load <= skew:
                break
            sources = [
                (balance(uid), e, uid)
                for e, uid in members
                if balance(uid) - low_load > skew and movable_by_uid.get(uid)
            ]
            if not sources:
                break
            _, _, from_uid = max(sources, key=lambda row: (row[0], -len(movable_by_uid[row[2]])))
            it = movable_by_uid[from_uid][0]

            # PLS-203: choose the target after the item is known so role-gated
            # workflows never move work to a member who cannot execute any next
            # non-terminal transition for that specific item.
            _required_caps = WorkflowService._task_required_capabilities(it)
            _capability_skips = []
            candidate_reasons = []
            target_rows = []
            for e, uid in members:
                if uid == from_uid:
                    continue
                verdict = WorkflowService._seat_routability_verdict(
                    it, pod, e, intent='new_assignment', enforce_cap=False,
                    user_id=uid, active_load=active_load[uid], inflight_load=inflight_load[uid],
                )
                if not WorkflowService._seat_assignment_allowed(verdict):
                    candidate_reasons.append((e, verdict.reasons))
                    continue
                if not WorkflowService._member_can_execute_from_status(e, it, user_id=uid):
                    continue
                if not WorkflowService._seat_satisfies_required_capability(e, _required_caps, item=it):
                    _capability_skips.append(e)
                    continue
                target_rows.append((balance(uid), e, uid))
            # PLS-157: the OPTIONAL load-balancer must NEVER move a pod's work
            # onto a cross-pod failover member — that re-creates the capability-
            # mismatch bounce (e.g. devops work landing on an arch-only agent).
            # Unlike the MANDATORY failover drain above (which must place a down
            # owner's work somewhere, so it falls back to cross-pod to avoid
            # stranding), a benign load imbalance here is strictly better than a
            # bounce, so restrict balancing targets to NATIVE members and skip the
            # move entirely when none exist. Without this, draining a down owner's
            # work to the sole native peer and then "balancing" it right back out
            # to the cross-pod member undoes the drain's native-aware placement.
            target_rows = sorted(t for t in target_rows if _is_native_target(t[1]))
            if not target_rows:
                if candidate_reasons:
                    WorkflowService._record_routability_hold(
                        it, pod,
                        sorted({r for _e, rs in candidate_reasons for r in (rs or [])}),
                        source='rebalance-load-balance',
                        candidate_reasons=candidate_reasons,
                    )
                if _required_caps and _capability_skips:
                    WorkflowService._record_capability_routing_hold(
                        it, pod, _required_caps, source='rebalance-load-balance',
                        attempted_email=', '.join(sorted(_capability_skips)),
                    )
                break
            low_load, to_email, to_uid = target_rows[0]
            if high_load - low_load <= skew:
                break
            if WorkflowService._cap_applies(pod) and (
                (active_cap > 0 and low_load >= active_cap)
                or (inflight_cap > 0 and inflight_load[to_uid] >= inflight_cap)
            ):
                break
            it = movable_by_uid[from_uid].pop(0)

            write_verdict = WorkflowService._seat_routability_verdict(
                it, pod, to_email, intent='new_assignment', enforce_cap=False,
                user_id=to_uid, active_load=active_load[to_uid], inflight_load=inflight_load[to_uid],
            )
            if not WorkflowService._seat_assignment_allowed(write_verdict):
                WorkflowService._record_routability_hold(
                    it, pod, write_verdict.reasons,
                    source='rebalance-load-write-guard', attempted_email=to_email,
                    candidate_reasons=[(to_email, write_verdict.reasons)],
                )
                break

            it.assign({'id': to_uid, 'email': to_email}, save=False)
            it._activity_actor = (0, 'system@pulse')
            it._substrate_write_intent = 'system'  # HIVE-1153 P1#1(b): decision-pin failover sweep
            suppress_bulk_assignment_dm(it)
            it.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])
            try:
                Comment.objects.create(item=it, author_id=0, author_email='system@pulse',
                    content=f'Auto-rebalanced to **{to_email}** (pod load-balancing — peer was overloaded).')
            except Exception:
                pass
            if it.status not in review_pipeline:
                active_load[to_uid] += 1
                active_load[from_uid] -= 1
            inflight_load[to_uid] += 1
            inflight_load[from_uid] -= 1
            moves += 1

        if moves:
            details[detail_key] = {
                'moves': moves,
                'load_after': {e: active_load[uid] for e, uid in members},
            }
            total_moves += moves

    if total_moves:
        logger.info(f"sweep_rebalance_pods: moved {total_moves} tasks; {details}")

    # PLAT-1304: capacity balancing via memberships (once, after every pod's
    # failover drain has run). `moves` above therefore reflect ONLY failover
    # drains + the PLAT-236 queued-drain under the flag — the capacity task-movers
    # were skipped; skew is corrected by these membership grants/expiries instead.
    if _membership_mode and not scoped_aliases:
        summary = membership_rebalancer.rebalance_memberships()
        return {
            'moves': total_moves,
            'mode': 'membership_rebalance',
            'memberships_added': len(summary.get('added', [])),
            'memberships_expired': summary.get('expired', 0),
            'failover_moves': total_moves,
            'pods': details,
            'membership_details': summary,
            'roster_reconciliation': _roster_reconciliation_health,
        }

    return {
        'moves': total_moves,
        'pods': details,
        'roster_reconciliation': _roster_reconciliation_health,
    }


# PLS-198 (F2): cross-worker single-flight lock key/TTL for the mutating sweep.
# TTL is deliberately BELOW the */10 beat cadence so a crashed holder can never
# block the next scheduled run.
_REBALANCE_SWEEP_LOCK_KEY = 'pulse:lock:sweep_rebalance_pods'
_REBALANCE_SWEEP_LOCK_TTL = 570


def _rebalance_sweep_lock_key():
    """Return the production lock key or a per-test-database namespace.

    Django's parallel runner gives each worker an isolated database clone, but
    CI intentionally uses one job-local Redis process.  A production-global
    key therefore made independent test workers suppress each other's sweeps,
    creating nondeterministic routing assertions.  Keep the real key global;
    only hermetic test workers derive a stable namespace from their clone.
    """
    if not getattr(settings, 'TESTING', False):
        return _REBALANCE_SWEEP_LOCK_KEY
    database_name = str(connection.settings_dict.get('NAME') or 'default')
    namespace = hashlib.sha256(database_name.encode('utf-8')).hexdigest()[:12]
    return f'{_REBALANCE_SWEEP_LOCK_KEY}:test:{namespace}'


_CREATOR_TEAM_FALLBACK_TAG = 'org-team-coordination-fallback'
_CREATOR_TEAM_FALLBACK_STATUSES = frozenset({
    'backlog', 'needs_triage', 'open', 'todo',
})


def _restore_recorded_org_team_fallback(item):
    """Preserve a previously established exact-org fallback across lane changes.

    Workflow transitions may replace the owning team with ``qa``/``review``.
    When that lane has no roster in the task's organization, an unassigned task
    must return to its already-audited tenant-local fallback instead of becoming
    stranded on a phantom team.  The fallback is never inferred here: it must
    have been recorded by ``_repair_phantom_team_to_creator_org_roster`` and its
    destination must still have an active exact-org roster.
    """
    from .models import TeamRoster
    from .services.workflow_service import WorkflowService

    if (
        item.assignee_id is not None
        or (item.assignee_email or '').strip()
        or not item.project_id
        or not (item.assignment_group or '').strip()
    ):
        return None

    metadata = item.metadata or {}
    fallback = (
        metadata.get('org_team_coordination_fallback')
        if isinstance(metadata, dict) else None
    )
    if not isinstance(fallback, dict):
        return None

    organization_id = int(getattr(item.project, 'org_id', 0) or 0)
    try:
        recorded_org_id = int(fallback.get('organization_id') or 0)
        recorded_version = int(fallback.get('version') or 0)
    except (TypeError, ValueError):
        return None
    creator_email = (item.created_by_email or '').strip().lower()
    recorded_creator = (fallback.get('creator_email') or '').strip().lower()
    tags = {
        value.strip()
        for value in str(item.tags or '').split(',')
        if value.strip()
    }
    current_team = WorkflowService._canonical_team_name(
        item.assignment_group,
    )
    fallback_team = WorkflowService._canonical_team_name(
        fallback.get('to_team') or '',
    )
    if (
        not organization_id
        or recorded_org_id != organization_id
        or recorded_version != 1
        or fallback.get('reason') != 'task_team_has_no_exact_org_roster'
        or _CREATOR_TEAM_FALLBACK_TAG not in tags
        or not creator_email
        or recorded_creator != creator_email
        or not fallback_team
        or fallback_team == current_team
    ):
        return None

    # A real tenant-local lane takes precedence over the historical fallback.
    if TeamRoster.objects.filter(
        organization_id=organization_id,
        team_name=current_team,
        is_active=True,
    ).exists():
        return None
    fallback_roster = TeamRoster.objects.filter(
        organization_id=organization_id,
        team_name=fallback_team,
        is_active=True,
    ).first()
    if not fallback_roster:
        return None
    fallback_members = TeamRoster._flat_member_emails(
        fallback_roster.tiers,
        fallback_roster.junior_emails,
    )
    if creator_email not in fallback_members:
        return None

    item.assignment_group = fallback_team
    item._activity_actor = (0, 'system@pulse')
    item.save(update_fields=['assignment_group', 'updated_at'])
    _create_system_comment_deduped(
        item,
        (
            f'Org-local lane recovery: workflow team `{current_team}` has no '
            f'roster in organization {organization_id}. Restored the existing '
            f'audited tenant-local `{fallback_team}` fallback; no '
            'cross-organization team or agent was borrowed.'
        ),
    )
    return current_team, fallback_team


def _repair_phantom_team_to_creator_org_roster(item):
    """Return an unowned coordination task to its creator's one local team.

    Organization isolation intentionally forbids borrowing a same-named team or
    agent from another tenant.  Historical tasks can still name a team that has
    no roster in their own organization, leaving them permanently group-queued.
    At an early coordination stage, a unique exact-org team membership for the
    creator is a safe owner-of-coordination fallback.  Never guess when the
    creator has zero/multiple local teams, and never override a real (including
    temporarily unavailable) exact-org team.
    """
    from .models import TeamRoster
    from .services.workflow_service import WorkflowService

    if (
        item.status not in _CREATOR_TEAM_FALLBACK_STATUSES
        or item.assignee_id is not None
        or (item.assignee_email or '').strip()
        or not item.project_id
        or not (item.assignment_group or '').strip()
    ):
        return None

    organization_id = int(getattr(item.project, 'org_id', 0) or 0)
    creator_email = (item.created_by_email or '').strip().lower()
    if not organization_id or not creator_email or creator_email == 'system@pulse':
        return None

    current_team = WorkflowService._canonical_team_name(
        item.assignment_group,
    )
    if TeamRoster.objects.filter(
        organization_id=organization_id,
        team_name=current_team,
        is_active=True,
    ).exists():
        return None

    creator_teams = {}
    for roster in TeamRoster.objects.filter(
        organization_id=organization_id,
        is_active=True,
    ).order_by('id'):
        members = TeamRoster._flat_member_emails(
            roster.tiers,
            roster.junior_emails,
        )
        if creator_email in members and members:
            canonical = WorkflowService._canonical_team_name(roster.team_name)
            creator_teams.setdefault(canonical, roster)
    if len(creator_teams) != 1:
        return None

    fallback_team = next(iter(creator_teams))
    if not fallback_team or fallback_team == current_team:
        return None

    metadata = dict(item.metadata or {})
    metadata['org_team_coordination_fallback'] = {
        'version': 1,
        'organization_id': organization_id,
        'from_team': current_team,
        'to_team': fallback_team,
        'creator_email': creator_email,
        'reason': 'task_team_has_no_exact_org_roster',
        'at': timezone.now().isoformat(),
    }
    tags = [
        value.strip()
        for value in str(item.tags or '').split(',')
        if value.strip()
    ]
    if _CREATOR_TEAM_FALLBACK_TAG not in tags:
        tags.append(_CREATOR_TEAM_FALLBACK_TAG)

    item.assignment_group = fallback_team
    item.metadata = metadata
    item.tags = ','.join(tags)
    item._activity_actor = (0, 'system@pulse')
    item.save(update_fields=[
        'assignment_group', 'metadata', 'tags', 'updated_at',
    ])
    _create_system_comment_deduped(
        item,
        (
            f'Org-local routing repair: team `{current_team}` has no roster in '
            f'organization {organization_id}. Returned this unassigned '
            f'coordination-stage task to the creator’s exact-org '
            f'`{fallback_team}` team; no cross-organization team or agent was '
            'borrowed.'
        ),
    )
    return current_team, fallback_team


# Capability and status mismatches do not move a task off its team
# (operator 2026-09-27). An available member of that team is the assignee.

_REPEATED_HOLD_ESCALATE_THRESHOLD = 6  # retained for older hold-count fixtures


def _maybe_escalate_repeated_hold(item, *, immediate=False):
    """Do not move a task off its team because the roster lacks a capability.

    Operator 2026-09-27: the task stays on the team it was given. An
    available member of that team is the assignee. If they cannot do the
    work, they reroute it and can file the corrective bug. ``immediate``
    is accepted so older callers stay import-compatible and still do not
    move the team.
    """
    return


def _queue_assign_still_open(current, actionable):
    """Whether this row is still a group-queued task the sweep may seat."""
    return (
        current.status in actionable
        and current.mode == 'task'
        and bool((current.assignment_group or '').strip())
        and current.assignee_id is None
        and not (current.assignee_email or '').strip()
    )


# PLAT-9027: stage-state group-queued items that sit UNASSIGNED past this
# freshness window are reconciler-miss candidates — their merge/deploy event
# was dropped (PLAT-9010 class: `apply_merge_event_transition returned None`
# and its historical cohort), not live queue worth draining. Re-binding them
# onto the owning team's stage seat loops the graveyard at sweep scale
# (observed 2026-09-17: ~70 merge-gated tasks dating to July/August flooded
# the merge seat in one actor=0 direct-write wave from the group-queue drain
# paths). Stale stage candidates are EXCLUDED from re-bind and surfaced for
# reconciliation instead — replay the missing supported transition
# idempotently; never skip to terminal, never reset durable state.
STAGE_ASSIGN_STATUSES = ('awaiting_deploy', 'awaiting_merge')
STAGE_ASSIGN_MAX_AGE_DAYS = 14.0


def _stage_assign_cutoff(now=None):
    """Freshness cutoff for re-binding unassigned stage-state group queues.

    ``updated_at`` is a conservative staleness proxy: any real stage touch
    (transition, comment, evidence) refreshes it, so genuinely live stage
    work stays inside the window; a dropped-event corpse does not.
    """
    import os

    default = STAGE_ASSIGN_MAX_AGE_DAYS
    try:
        max_age = float(
            os.environ.get('PULSE_QUEUE_ASSIGN_STAGE_MAX_AGE_DAYS', default)
        )
        if max_age < 0:
            max_age = default
    except (TypeError, ValueError):
        max_age = default
    return (now or timezone.now()) - timedelta(days=max_age)


def _is_stale_stage_candidate(item, cutoff):
    """Whether this item is an unassigned stage-state reconciler-miss candidate."""
    return (
        item.status in STAGE_ASSIGN_STATUSES
        and item.updated_at is not None
        and item.updated_at < cutoff
    )


def _queue_assign_miss_reason(item):
    """Reason on a routability hold written by the resolve that just returned nobody."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import Comment

    hold = (
        Comment.objects.filter(
            item_id=item.pk,
            origin_event='resolver.routability_hold',
            created_at__gte=timezone.now() - timedelta(seconds=30),
        )
        .order_by('-id')
        .first()
    )
    reasons = ((hold.origin_payload or {}).get('reason_classes') if hold else None) or []
    if reasons:
        return ','.join(str(reason) for reason in reasons[:4])
    return 'no_assignee'


def _release_queue_assign_reservation(item):
    """Drop a WIP reservation when the sweep does not save the assignee."""
    if not getattr(item, '_wip_reservation_token', None):
        return
    from .services.workflow_service import WorkflowService
    try:
        WorkflowService._release_unconsumed_wip_reservation(item)
    except Exception:
        logger.exception(
            'queue-assign reservation release failed for %s',
            getattr(item, 'item_key', None) or getattr(item, 'pk', None),
        )


@shared_task(bind=True)
def sweep_assign_group_queued(
    self,
    limit: int = 0,
    only_agent_email: str | None = None,
    max_seconds: float | None = None,
):
    """Assign group-queued ACTIONABLE tasks to concrete team members (operator
    2026-07-11). The membership rebalancer moves project memberships, not tasks —
    nothing pushed group-queued (assignment_group set, assignee NULL) work onto
    agents, so it piled in team queues while members idled and in-progress tasks
    orphaned. Resolves each via WorkflowService._resolve_team_assignee (the same
    capability/org/tier-aware resolver create+transition use); tasks with no
    capable available member are LEFT queued + logged (never force-assigned to an
    agent that cannot execute them).

    ``only_agent_email`` is the event-driven recovery path fired when one agent
    becomes available.  It examines only exact team lanes whose current
    candidate source contains that identity; the unscoped periodic invocation
    remains a recovery backstop.  Blocked tasks are never selected.

    ``max_seconds`` bounds the pass's wall-clock occupancy of its worker slot
    (PLS-1415): the resolver does serial per-item capability/permission
    filtering, and with a large group-queue backlog a single unbounded pass
    held the (single-slot) fork worker for ~25 minutes while every other
    sweep — recurring timers, escalation outbox, org health, liveness-critical
    probes — queued behind it, ballooning pass duration past the 300s bound
    and firing PulseSweepLivenessStale on one replica per cycle. The periodic
    pass defaults to PULSE_QUEUE_ASSIGN_MAX_SECONDS (120s); unassigned items
    are simply re-examined next tick (the PLS-667 skip-set already keeps
    non-urgent misses from being re-attempted for 5 minutes). Pass 0 to
    disable the budget (manual/management-command drain paths).

    Kill-switch PULSE_QUEUE_ASSIGN_ENABLED=0.
    """
    import os
    from django.db.models import Q
    from .models import Item
    from .services.queue_priority import effective_blocker_priority_rank, queue_stage_rank_value
    from .services.workflow_service import WorkflowService
    if os.environ.get('PULSE_QUEUE_ASSIGN_ENABLED', '1') != '1':
        return {'skipped': 'disabled'}
    if max_seconds is None:
        try:
            max_seconds = float(os.environ.get('PULSE_QUEUE_ASSIGN_MAX_SECONDS', '120'))
        except (TypeError, ValueError):
            max_seconds = 120.0
    _pass_deadline = (
        time.monotonic() + max_seconds if max_seconds and max_seconds > 0 else None
    )
    # needs_triage / doing are unstarted team work Hive already treats as
    # team_queue worthiness. Leaving them out of this drain meant an idle
    # DeepSeek holder (sara) sat queue_empty while Engineering's untriaged
    # pile never became a concrete assignment (operator 2026-08-22).
    # awaiting_deploy and awaiting_merge are hands-on for the team that owns
    # that stage. Leaving them out of this drain meant an urgent task sat
    # group-queued even though a direct resolve would seat a member.
    ACTIONABLE = (
        'open', 'todo', 'doing', 'needs_triage', 'in_progress', 'in_review',
        'awaiting_deploy', 'awaiting_merge',
    )
    qs = (Item.objects.filter(status__in=ACTIONABLE, mode='task')
          .exclude(assignment_group__isnull=True).exclude(assignment_group='')
          .filter(Q(assignee_email__isnull=True) | Q(assignee_email=''))
          .filter(assignee_id__isnull=True)
          .annotate(_queue_effective_priority_rank=effective_blocker_priority_rank()))
    queued = sorted(qs, key=lambda i: (
        i._queue_effective_priority_rank,
        queue_stage_rank_value(i.status),
        i.created_at,
        i.pk,
    ))
    # PLAT-9027: never re-bind stale stage-state corpses onto the stage seat.
    stage_cutoff = _stage_assign_cutoff()
    stale_stage = [i for i in queued if _is_stale_stage_candidate(i, stage_cutoff)]
    if stale_stage:
        logger.warning(
            'queue-assign reconciler-miss candidates excluded from re-bind '
            'count=%s sample=%s (replay the dropped merge/deploy event; do '
            'not re-seat and do not skip to terminal)',
            len(stale_stage),
            [
                (getattr(i, 'item_key', None) or i.pk, i.status, str(i.updated_at))
                for i in stale_stage[:20]
            ],
        )
        stale_stage_pks = {i.pk for i in stale_stage}
        queued = [i for i in queued if i.pk not in stale_stage_pks]
    scoped_email = (only_agent_email or '').strip().lower()
    scoped_aliases = (
        WorkflowService._agent_email_aliases(scoped_email)
        if scoped_email else set()
    )
    if scoped_aliases:
        # This task is dispatched after the durable active row commits, but a
        # long-lived Celery worker may still hold the prior five-second
        # AgentAvailability cache.  Invalidate only the local read caches; live
        # provider/daemon health remains authoritative.
        WorkflowService._db_avail_cache = None
        WorkflowService._disabled_emails_cache = None
    # PLS-667: Redis-backed skip set — items that failed assignment (no capable
    # available member) are skipped for 5 minutes so the sweep doesn't re-attempt
    # them every cycle, wasting CPU and creating noisy log spam.
    _SKIP_TTL = 300  # 5 minutes
    _SKIP_KEY = 'pulse:queue_assign_skip_until'
    _skip_until: dict = {}
    try:
        _skip_raw = WorkflowService._redis_client().get(_SKIP_KEY)
        if _skip_raw:
            import json as _json
            _skip_until = _json.loads(_skip_raw)
            if not isinstance(_skip_until, dict):
                _skip_until = {}
    except Exception:
        _skip_until = {}
    _now_ts_for_skip = time.time()
    _dirty_skip = False

    assigned = mismatched = rerouted = urgent_unseated = 0
    examined = 0
    budget_exhausted = False
    for item in queued:
        if limit and assigned >= limit:
            break
        if _pass_deadline is not None and time.monotonic() >= _pass_deadline:
            # PLS-1415: yield the worker slot. Unexamined/remaining items are
            # re-sorted next tick; nothing is lost, only deferred.
            budget_exhausted = True
            break
        examined += 1
        # A non-urgent miss is remembered for five minutes. An urgent miss is
        # not: the next pass must ask the resolver again. Drop a stale urgent
        # skip left by an older sweep.
        _pk_str = str(item.pk)
        _urgent = WorkflowService._assignment_ignores_wip_cap(item)
        if _urgent and _pk_str in _skip_until:
            del _skip_until[_pk_str]
            _dirty_skip = True
        elif _pk_str in _skip_until:
            try:
                if float(_skip_until[_pk_str]) > _now_ts_for_skip:
                    continue
            except (TypeError, ValueError):
                pass
            del _skip_until[_pk_str]
            _dirty_skip = True
        current = None
        try:
            # Resolve before locking the task row. The resolver calls identity
            # and may reserve a WIP slot. Holding SELECT FOR UPDATE across that
            # work makes the next sweep's lock wait hit statement_timeout, the
            # transaction rolls back, and the task stays unassigned.
            current = Item.objects.select_related('project').get(pk=item.pk)
            if not _queue_assign_still_open(current, ACTIONABLE):
                continue
            if (
                _restore_recorded_org_team_fallback(current)
                or _repair_phantom_team_to_creator_org_roster(current)
            ):
                rerouted += 1
                current.refresh_from_db()
                if not _queue_assign_still_open(current, ACTIONABLE):
                    continue
            if scoped_aliases:
                candidate_aliases = set()
                for candidate in WorkflowService._active_team_candidate_emails(
                    current.assignment_group,
                    item=current,
                ):
                    candidate_aliases |= WorkflowService._agent_email_aliases(
                        candidate,
                    )
                if not (scoped_aliases & candidate_aliases):
                    continue
            uid, email = WorkflowService._resolve_team_assignee(
                current.assignment_group,
                item=current,
                project_key=(
                    getattr(current.project, 'key', None)
                    if current.project_id else None
                ),
            )
            if not email:
                reason = _queue_assign_miss_reason(current)
                logger.info(
                    'queue-assign unseated item=%s team=%s status=%s urgent=%s reason=%s',
                    current.item_key, current.assignment_group, current.status,
                    _urgent, reason,
                )
                if not _urgent:
                    _skip_until[_pk_str] = str(_now_ts_for_skip + _SKIP_TTL)
                    _dirty_skip = True
                else:
                    urgent_unseated += 1
                mismatched += 1
                continue
            with transaction.atomic():
                # Project is nullable. Lock only the Item row: PostgreSQL
                # rejects FOR UPDATE across select_related()'s nullable
                # outer join.
                locked = (
                    Item.objects.select_for_update(of=('self',))
                    .get(pk=item.pk)
                )
                if not _queue_assign_still_open(locked, ACTIONABLE):
                    _release_queue_assign_reservation(current)
                    continue
                token = getattr(current, '_wip_reservation_token', None)
                if token:
                    locked._wip_reservation_token = token
                fields = []
                uid, email = WorkflowService._apply_resolved_assignee(
                    locked, uid, email, fields,
                )
                if not email:
                    _release_queue_assign_reservation(locked)
                    logger.info(
                        'queue-assign unseated item=%s team=%s status=%s urgent=%s reason=apply_refused',
                        locked.item_key, locked.assignment_group, locked.status, _urgent,
                    )
                    if not _urgent:
                        _skip_until[_pk_str] = str(_now_ts_for_skip + _SKIP_TTL)
                        _dirty_skip = True
                    else:
                        urgent_unseated += 1
                    mismatched += 1
                    continue
                if _pk_str in _skip_until:
                    del _skip_until[_pk_str]
                    _dirty_skip = True
                locked._activity_actor = (0, 'system@pulse')
                locked.save(update_fields=list(dict.fromkeys(
                    fields + ['updated_at'],
                )))
                assigned += 1
        except Exception:
            logger.exception('queue-assign resolve failed for %s', item.item_key)
            if current is not None:
                _release_queue_assign_reservation(current)
            continue

    # Persist the skip set back to Redis with a generous TTL so stale entries
    # eventually expire even if the sweep crashes after the write.
    if _dirty_skip:
        try:
            import json as _json
            WorkflowService._redis_client().setex(
                _SKIP_KEY, 3600, _json.dumps(_skip_until),
            )
        except Exception:
            pass

    if assigned or mismatched or rerouted or urgent_unseated or budget_exhausted:
        logger.info(
            'sweep_assign_group_queued: assigned=%s rerouted=%s '
            'unseated=%s urgent_unseated=%s of %s scoped_agent=%s '
            'examined=%s budget_exhausted=%s',
            assigned, rerouted, mismatched, urgent_unseated, len(queued),
            scoped_email or '-', examined, budget_exhausted,
        )
    return {
        'assigned': assigned,
        'rerouted': rerouted,
        'capability_mismatch': mismatched,
        'urgent_unseated': urgent_unseated,
        'queued': len(queued),
        'examined': examined,
        'budget_exhausted': budget_exhausted,
        'max_seconds': max_seconds,
        'only_agent_email': scoped_email or None,
    }


def _failover_target_started_after(item, target_email, recorded_at):
    """Whether the automatic replacement owner acted after the failover.

    Once the replacement has produced task-local work, ownership is no longer an
    untouched automation artifact and an automatic failback would destroy live
    context.  Comments and audit activities are both durable evidence; the
    system-authored assignment row itself is excluded by actor identity.
    """
    from django.db.models import Q
    from .models import Comment, ItemActivity
    from .services.workflow_service import WorkflowService

    aliases = WorkflowService._agent_email_aliases(target_email)
    if not aliases:
        return True
    actor_q = Q()
    for alias in aliases:
        actor_q |= Q(user_email__iexact=alias)
    comment_q = Q()
    for alias in aliases:
        comment_q |= Q(author_email__iexact=alias)
    return (
        ItemActivity.objects
        .filter(item_id=item.pk, created_at__gt=recorded_at)
        .filter(actor_q)
        .exists()
        or Comment.objects
        .filter(item_id=item.pk, created_at__gt=recorded_at)
        .filter(comment_q)
        .exists()
    )


def _failover_receipt_boundary(item, marker):
    """Return the immutable receipt event when no later owner/status write won.

    The JSON marker is useful for indexed lookup, but mutable JSON alone is not
    a CAS token: an item can be manually reassigned away and later land back on
    the same temporary target.  The synchronous ItemActivity receipt is written
    after the failover assignment in the same transaction.  Any later Assignee
    or Status activity supersedes it, even if the final row happens to resemble
    the old automatic move again.
    """
    from .models import ItemActivity

    receipt_id = str(marker.get('receipt_id') or '').strip()
    if not receipt_id:
        return None, 'receipt_id_missing'
    receipt_event = (
        ItemActivity.objects
        .filter(
            item_id=item.pk,
            action='updated',
            field_name='Auto Failover Receipt',
            new_value=receipt_id,
            user_email='system@pulse',
        )
        .order_by('-created_at')
        .first()
    )
    if receipt_event is None:
        return None, 'receipt_event_missing'
    if str(item.status or '') != str(marker.get('status') or ''):
        return receipt_event, 'status_changed'
    if ItemActivity.objects.filter(
        item_id=item.pk,
        created_at__gt=receipt_event.created_at,
        field_name__in=('Assignee', 'Status'),
    ).exists():
        return receipt_event, 'newer_assignment_or_status_activity'
    return receipt_event, None


@shared_task
def restore_false_positive_failovers_for_agent(only_agent_email: str):
    """Event-driven failback for untouched automatic moves (PLAT-5032).

    A successful authenticated Pulse request schedules this agent-scoped task.
    It restores only a structured failover receipt whose original owner is the
    caller, whose current owner is still the recorded automatic target, and whose
    target has produced no task-local work.  Row locking + the current-owner check
    make retries idempotent; manual or later automation ownership always wins.
    """
    from django.db import transaction
    from .models import Item
    from .services.workflow_service import WorkflowService

    canonical = _canonical_agent_email(only_agent_email)
    aliases = _agent_email_aliases_for_heartbeat(canonical)
    latest_hb = _agent_latest_pulse_heartbeat_ts(canonical)
    if not canonical or not aliases or latest_hb is None:
        return {
            'restored': 0,
            'eligible': 0,
            'waiting': 0,
            'only_agent_email': canonical or None,
        }

    restored = eligible = waiting = matched = 0
    candidates = Item.objects.filter(
        mode='task',
        resolution__isnull=True,
        metadata__auto_failover_restore__schema='pulse.auto-failover-restore.v1',
        metadata__auto_failover_restore__state='active',
    ).only(
        'id', 'item_key', 'status', 'assignment_group', 'assignee_id',
        'assignee_email', 'metadata', 'project_id', 'workflow_name', 'labels',
        'tags', 'title', 'description',
    )
    for candidate in candidates.iterator():
        marker = (candidate.metadata or {}).get('auto_failover_restore') or {}
        from_email = _canonical_agent_email(marker.get('from_email'))
        if from_email not in aliases:
            continue
        matched += 1

        with transaction.atomic():
            fresh = Item.objects.select_for_update().get(pk=candidate.pk)
            current_marker = (fresh.metadata or {}).get('auto_failover_restore') or {}
            from_email = _canonical_agent_email(current_marker.get('from_email'))
            if (
                current_marker.get('schema') != 'pulse.auto-failover-restore.v1'
                or current_marker.get('state') != 'active'
                or from_email not in aliases
                or not WorkflowService._emails_same_agent_identity(
                    fresh.assignee_email, current_marker.get('to_email')
                )
                or (
                    current_marker.get('to_id') is not None
                    and str(fresh.assignee_id) != str(current_marker.get('to_id'))
                )
            ):
                continue
            if _is_terminal_status(fresh.status):
                continue
            receipt_event, superseded_reason = _failover_receipt_boundary(
                fresh, current_marker,
            )
            if superseded_reason:
                logger.info(
                    'PLAT-5032: preserving current owner for %s because failback '
                    'receipt %s was superseded (%s)',
                    fresh.item_key or fresh.pk,
                    current_marker.get('receipt_id') or '-',
                    superseded_reason,
                )
                continue
            if _failover_target_started_after(
                fresh,
                current_marker.get('to_email'),
                receipt_event.created_at,
            ):
                logger.info(
                    'PLAT-5032: preserving failover target for %s because the target '
                    'started work after the automatic move',
                    fresh.item_key or fresh.pk,
                )
                continue
            # The failover event itself cannot prove recovery; the successful
            # authenticated observation must be strictly newer than the
            # transactionally anchored receipt.  A still-old heartbeat leaves
            # the pending marker armed for the owner's next request.
            if (
                latest_hb <= receipt_event.created_at.timestamp()
                or not _agent_has_recent_pulse_heartbeat(canonical)
            ):
                waiting += 1
                continue
            eligible += 1

            # A recent request refutes only soft/stale unavailability. Operator
            # disable, service denial, wedge, team/capability/status/SoD mismatch,
            # or a genuinely unavailable runtime must never be auto-restored.
            available = WorkflowService._is_agent_available_for_assignment(from_email)
            if not available and not _soft_unavailable_owner_protected(from_email):
                waiting += 1
                continue
            verdict = WorkflowService._seat_routability_verdict(
                fresh,
                fresh.assignment_group,
                from_email,
                intent='owner_drain',
                enforce_cap=False,
                user_id=current_marker.get('from_id'),
            )
            blocking_reasons = set(verdict.reasons or ()) - {'seat_not_live'}
            if blocking_reasons:
                logger.warning(
                    'PLAT-5032: refusing automatic failback for %s to %s; '
                    'blocking routability reasons=%s',
                    fresh.item_key or fresh.pk, from_email,
                    sorted(blocking_reasons),
                )
                waiting += 1
                continue
            from_id = current_marker.get('from_id')
            if from_id is None:
                continue

            metadata = dict(fresh.metadata or {})
            marker_done = dict(current_marker)
            marker_done.update({
                'state': 'restored',
                'restored_at': timezone.now().isoformat(),
                'restored_by': canonical,
                'recovery_heartbeat_ts': latest_hb,
            })
            metadata['auto_failover_restore'] = marker_done
            fresh.metadata = metadata
            fresh.assign({'id': from_id, 'email': from_email}, save=False)
            if current_marker.get('decision_pinned'):
                _repin_decision_assignment(fresh, from_email)
            fresh._activity_actor = (0, 'system@pulse')
            fresh._substrate_write_intent = 'system'
            fresh._suppress_standard_notifications = True
            fresh.save(update_fields=[
                'assignee_id', 'assignee_email', 'metadata', 'updated_at',
            ])
            _create_system_comment_deduped(
                fresh,
                f'Auto-failback restored **{from_email}** after newer authenticated '
                'Pulse activity disproved the prior soft/offline availability '
                f'verdict; untouched temporary owner was **{current_marker.get("to_email")}** '
                '(PLAT-5032).',
            )
            restored += 1

    if eligible or restored or waiting:
        logger.warning(
            'PLAT-5032 event-driven failback agent=%s matched=%s eligible=%s '
            'restored=%s waiting=%s',
            canonical, matched, eligible, restored, waiting,
        )
    # Clear only when this pass proved that no untouched receipt still needs a
    # newer activity/recovery event.  In particular, restoring an early receipt
    # must not erase the rendezvous marker for a later failover whose receipt is
    # newer than the same heartbeat.
    if matched == 0 or waiting == 0:
        try:
            from .agent_liveness import clear_agent_failback_pending
            clear_agent_failback_pending(canonical)
        except Exception:
            pass
    return {
        'restored': restored,
        'eligible': eligible,
        'waiting': waiting,
        'only_agent_email': canonical,
    }


@shared_task
def reconcile_group_owned_external_waits(
    organization_id: int | None = None,
    team_name: str | None = None,
    only_agent_email: str | None = None,
):
    """Event-driven exact-org holder repair for parked escalation children."""
    from .services.external_wait import reconcile_group_owned_human_waits

    return reconcile_group_owned_human_waits(
        organization_id=organization_id,
        team_name=team_name,
        only_agent_email=only_agent_email,
    )


@shared_task
def refresh_agent_health_and_assign_group_queue(only_agent_email: str):
    """Refresh fleet truth before draining one recovered agent's team queues.

    A productive bridge turn proves that the agent process and provider are
    live, but the shared availability map may still contain the pre-start
    offline/disabled sample. Running the scoped assignment first makes the
    event-driven recovery path lose that race and leaves work queued until the
    periodic sweeps happen to converge. Keep both steps in one Celery task so
    every recovery event observes a fresh snapshot before resolving work.
    """
    availability = sweep_agent_availability.run()
    failback = restore_false_positive_failovers_for_agent.run(only_agent_email)
    assignment = sweep_assign_group_queued.run(
        only_agent_email=only_agent_email,
    )
    external_wait_assignment = reconcile_group_owned_external_waits.run(
        only_agent_email=only_agent_email,
    )
    return {
        'availability': availability,
        'failback': failback,
        'assignment': assignment,
        'external_wait_assignment': external_wait_assignment,
    }


def _hive626_direct_review_hold(item, down_email, pod):
    """PLS-488 / HIVE-626 predicate.

    An in-progress Engineering-grouped architecture-review item whose down
    assignee is on the Architecture roster must not be failed over to an
    Engineering implementer.
    """
    from .services.workflow_service import WorkflowService
    if (getattr(item, 'status', '') or '') != 'in_progress':
        return False
    if WorkflowService._canonical_team_name(pod) != 'engineering':
        return False
    if not WorkflowService._item_has_label(item, 'architecture-review'):
        return False
    return bool(WorkflowService._is_architecture_team_email(down_email))


def _resolve_direct_review_failover_target(item, down_email):
    """PLS-421 AC1: substitute a seat that still satisfies the original direct
    assignee's seat constraints instead of holding unconditionally.

    The item's in-progress workflow group is the implementation team; failing
    over from it hands an independent Architecture exact-version review to an
    Engineering implementer (the live HIVE-626 regression). The only
    compatible substitute is another seat on the current Architecture roster
    that is not the down identity and not the item author (independence /
    no-self-review). Ranked by active then workable load. Returns (None, None)
    when no compatible substitute exists — the caller then holds fail-loud
    (AC2) instead of falling back to the workflow's generic team.
    """
    from .models import TeamRoster
    from .user_service import user_service
    from .services.workflow_service import WorkflowService

    try:
        rows = [
            roster for roster in TeamRoster.objects.filter(
                is_active=True,
                organization_slug='shizuha',
            )
            if WorkflowService._canonical_team_name(roster.team_name) == 'architecture'
        ]
        if len(rows) != 1:
            logger.error(
                'PLS-421: architecture roster claim denied: expected one current '
                'roster, got %d', len(rows),
            )
            return None, None
        emails = WorkflowService._flat_roster(rows[0].tiers or [])
        emails += list(rows[0].junior_emails or [])
    except Exception:
        logger.exception('PLS-421: architecture roster enumeration failed')
        return None, None

    author_email = (getattr(item, 'created_by_email', '') or '').strip().lower()
    best = (None, None)
    best_key = None
    for candidate in emails:
        candidate = (candidate or '').strip()
        if not candidate:
            continue
        if down_email and WorkflowService._emails_same_agent_identity(
                candidate, down_email):
            continue
        try:
            info = user_service.get_user_by_email(candidate)
            uid = info.get('id') if info else None
            if not uid or not str(uid).isdigit():
                continue
            uid = int(uid)
        except Exception:
            continue
        # A hibernated seat cannot act on the review; require a resident seat.
        if not WorkflowService._is_agent_available_for_assignment(candidate):
            continue
        required_caps = WorkflowService._task_required_capabilities(item)
        if not WorkflowService._member_can_execute_from_status(
                candidate, item, user_id=uid):
            continue
        if not WorkflowService._seat_satisfies_required_capability(
                candidate, required_caps, item=item):
            continue
        if author_email and WorkflowService._emails_same_agent_identity(
                candidate, author_email):
            continue  # no-self-review independence constraint
        key = (
            WorkflowService._agent_active_load(uid, candidate),
            WorkflowService._agent_workable_load(uid, candidate),
        )
        if best_key is None or key < best_key:
            best_key = key
            best = (uid, candidate)
    return best


def _direct_review_failover_seat_team(down_email):
    """PLS-421 AC1: the roster team a direct-review substitute seat belongs to.

    The failover write-guard must evaluate the substitute against the seat's
    OWN team context, not the item's in-progress ``assignment_group`` — a
    resident Architecture reviewer is never a member of the implementation
    pod, so an engineering-grouped verdict always yields
    ``seat_not_team_member`` and silently converts AC1 substitution into the
    AC2-style hold. Returns None when the down owner is not a direct-review
    Architecture seat (caller keeps the workflow group).
    """
    from .services.workflow_service import WorkflowService
    if WorkflowService._is_architecture_team_email(down_email):
        return 'architecture'
    return None


@shared_task(bind=True)
def sweep_rebalance_pods(
    self,
    max_skew: int | None = None,
    only_agent_email: str | None = None,
):
    """PLS-198 (F2): single-flight wrapper around the mutating rebalance/failover
    sweep. Celery beat (*/10) can stack runs behind a backlogged worker; without a
    cross-worker guard, back-to-back/concurrent runs each computed `down` from the
    same snapshot and each performed the reassign+comment — the duplicate
    auto-failover comment bursts on BKST-1 (2000+ comments). Django's cache can be
    per-process LocMem here, so the guard uses the shared Redis client
    (`SET NX EX`); fail-open on Redis errors per the codebase convention (a Redis
    outage must not freeze routing — the per-item recheck (F4) and comment dedupe
    (F5) still bound the damage of an unlocked overlap)."""
    import uuid
    from .services.workflow_service import WorkflowService

    token = uuid.uuid4().hex
    lock_key = _rebalance_sweep_lock_key()
    have_lock = False
    try:
        have_lock = bool(WorkflowService._redis_client().set(
            lock_key, token, nx=True,
            ex=_REBALANCE_SWEEP_LOCK_TTL))
        if not have_lock:
            if only_agent_email:
                raise self.retry(countdown=5, max_retries=6)
            logger.warning(
                'sweep_rebalance_pods: overlapping run skipped — single-flight '
                'lock held by another worker [PLS-198]')
            return {'moves': 0, 'pods': {}, 'skipped': 'sweep_already_running'}
    except Retry:
        # ``Task.retry`` raises this control-flow exception.  It must escape the
        # Redis fail-open handler or a scoped availability event would run
        # unlocked behind an active global sweep instead of retrying.
        raise
    except Exception as exc:
        logger.warning(
            f'sweep_rebalance_pods: single-flight lock unavailable ({exc!r}) '
            f'— running unlocked (fail-open) [PLS-198]')
        have_lock = False
    try:
        return _sweep_rebalance_pods_impl(max_skew, only_agent_email=only_agent_email)
    finally:
        if have_lock:
            try:
                client = WorkflowService._redis_client()
                raw = client.get(lock_key)
                held = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
                if held == token:
                    client.delete(lock_key)
            except Exception:
                pass  # TTL (< beat cadence) reclaims the lock


@shared_task(bind=True, max_retries=1)
def sweep_throughput_watchdog(self):
    """
    PLS-30: Fires an error-severity alert when org task-transition throughput flatlines
    fleet-wide — detecting the 2026-06-12 post-reboot 3h silent-stall pattern.

    Checks every 30 min: if zero agent status_changed/commented/completed ItemActivity
    events (on task items only) occurred in the last WATCHDOG_WINDOW_MINUTES (default 90)
    while >= WATCHDOG_MIN_TASKS (default 10) actionable non-blocked tasks are assigned,
    it fires a dedup'd error-severity alert to the devops team.
    Auto-resolves when any agent acts or when the actionable backlog drops below MIN_TASKS.

    Test: set WATCHDOG_WINDOW_MINUTES=5, wait, confirm alert appears.
    """
    import os
    from datetime import timedelta
    from django.db.models import Count
    from .models import Item, ItemActivity, Status
    from .services.deduplication import AlertDeduplicationService
    from .services.workflow_service import WorkflowService

    # THROUGHPUT_WATCHDOG_STALL_HOURS / THROUGHPUT_WATCHDOG_WINDOW_HOURS match
    # the deployment config knobs (3h default — the 2026-06-12 post-reboot
    # fleet-stall incident window). Falls back to legacy WATCHDOG_WINDOW_MINUTES
    # so existing deployments with that env var set are not broken.
    _stall_hours = float(
        os.environ.get('THROUGHPUT_WATCHDOG_STALL_HOURS')
        or os.environ.get('THROUGHPUT_WATCHDOG_WINDOW_HOURS')
        or (int(os.environ.get('WATCHDOG_WINDOW_MINUTES', '180')) / 60)
    )
    WINDOW_MINUTES = int(_stall_hours * 60)
    MIN_TASKS = int(os.environ.get('WATCHDOG_MIN_TASKS', '10'))
    ALERT_SOURCE = 'pulse.throughput_watchdog'
    COOLDOWN_HOURS = float(os.environ.get('THROUGHPUT_WATCHDOG_COOLDOWN_HOURS', '0'))

    # Positive agent-identity set from the LIVE DB-backed pod roster (not the
    # static _POD_ROSTER seed). Agents may act under either their @shizuha.com
    # primary identity or their @agents.shizuha.io internal alias; include both
    # so alias-based activity is not ignored, and fleet flatlines are not masked.
    _AGENTS_DOMAIN = '@agents.shizuha.io'
    _CANON_DOMAIN = '@shizuha.com'
    agent_emails: set = set()
    agent_ids: set = set()
    from .user_service import user_service as _user_svc
    for _val in WorkflowService._pod_roster().values():
        for _e in WorkflowService._flat_roster(_val):
            _canon = _e.lower()
            agent_emails.add(_canon)
            if _canon.endswith(_CANON_DOMAIN):
                agent_emails.add(_canon[:-len(_CANON_DOMAIN)] + _AGENTS_DOMAIN)
            # Collect numeric user IDs for legacy rows that have assignee_id
            # set but assignee_email blank/null (pre-email-schema assignments
            # that the watchdog email filter would miss). kai P2 — cycle-2.
            try:
                _info = _user_svc.get_user_by_email(_canon)
                if _info and _info.get('id'):
                    agent_ids.add(int(_info['id']))
            except Exception:
                pass

    window_start = timezone.now() - timedelta(minutes=WINDOW_MINUTES)

    # Count real agent activity on task items only in the rolling window.
    # Filter positively to known agent identities so human/PM transitions
    # cannot suppress a flatline alert while agents are actually stalled.
    recent_count = (
        ItemActivity.objects
        .filter(
            action__in=['status_changed', 'commented', 'completed'],
            created_at__gte=window_start,
            item__mode='task',
            user_email__in=agent_emails,
        )
        .count()
    )

    # Count actionable assigned-to-agent tasks. Combine category-based terminal
    # detection (catches custom workflow slugs) with the legacy enum set
    # (resolved, failed, expired, merged, wont_fix, etc.) to avoid gaps when
    # a status slug exists only in one list. Exclude epic/umbrella containers
    # (PLS-25) since their children carry the actual runnable work.
    # Include ID-backed assignments (assignee_id set, email blank/null) so
    # legacy rows still in the pre-email schema contribute to the backlog count.
    non_actionable_slugs = (
        set(
            Status.objects
            .filter(category__in=('done', 'scheduled', 'backlog'))
            .values_list('slug', flat=True)
        )
        | _TERMINAL_ENUM_SLUGS
        | {'blocked'}
    )
    from django.db.models import Q as _Q
    _agent_q = _Q(assignee_email__in=agent_emails)
    if agent_ids:
        _agent_q |= _Q(assignee_id__in=agent_ids) & (
            _Q(assignee_email__isnull=True) | _Q(assignee_email='')
        )
    actionable_qs = (
        Item.objects
        .filter(mode='task')
        .filter(_agent_q)
        .exclude(status__in=non_actionable_slugs)
        .exclude(WorkflowService._umbrella_item_q())
    )
    actionable_count = actionable_qs.count()

    if recent_count > 0 or actionable_count < MIN_TASKS:
        # Fleet is active OR insufficient backlog — resolve any existing flatline alert.
        reason = 'activity_detected' if recent_count > 0 else 'backlog_below_threshold'
        resolved = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged'],
            source=ALERT_SOURCE,
        ).update(
            status='resolved',
            resolved_at=timezone.now(),
            resolved_by=f'sweep_throughput_watchdog:{reason}',
        )
        if resolved:
            logger.info(
                f"sweep_throughput_watchdog: resolved flatline alert "
                f"({reason}, activity={recent_count}, actionable={actionable_count})"
            )
        return {'status': 'ok', 'activity_count': recent_count, 'actionable_count': actionable_count}

    # No activity and sufficient backlog — check cooldown before firing.
    # THROUGHPUT_WATCHDOG_COOLDOWN_HOURS (default 0 = disabled): after a flatline
    # alert is resolved, suppress new alerts for this many hours. Prevents a
    # transient recovery (brief activity burst, then another flatline) from
    # immediately paging DevOps again (kai P2 — cycle-2).
    if COOLDOWN_HOURS > 0:
        cooldown_start = timezone.now() - timedelta(hours=COOLDOWN_HOURS)
        in_cooldown = Item.objects.filter(
            mode='alert',
            source=ALERT_SOURCE,
            status='resolved',
            resolved_at__gte=cooldown_start,
        ).exists()
        if in_cooldown:
            logger.info(
                "sweep_throughput_watchdog: flatline detected but within cooldown "
                "window (%.1fh); skipping alert", COOLDOWN_HOURS
            )
            return {'status': 'cooldown', 'window_minutes': WINDOW_MINUTES, 'actionable_count': actionable_count}

    # No activity, sufficient backlog, and not in cooldown — fire (or update) the
    # flatline alert. Build per-agent stall detail for the alert body.
    agents = list(
        actionable_qs
        .values('assignee_email')
        .annotate(task_count=Count('id'))
        .order_by('-task_count')[:20]
    )
    stall_lines = []
    for a in agents:
        email = a['assignee_email']
        last_act = (
            ItemActivity.objects
            .filter(user_email=email, item__mode='task')
            .order_by('-created_at')
            .values_list('created_at', flat=True)
            .first()
        )
        last_str = last_act.isoformat() if last_act else 'never'
        stall_lines.append(f"- {email}: {a['task_count']} tasks, last activity: {last_str}")

    description = (
        f"No agent task transitions or comments in the last **{WINDOW_MINUTES} min** "
        f"({actionable_count} actionable tasks are assigned).\n\n"
        f"**Agents holding work:**\n" + '\n'.join(stall_lines) + '\n\n'
        "Check agent health: heartbeats, container status, Pulse queue order. "
        "This alert auto-resolves when any agent makes a transition or comment."
    )

    _alert, _is_new = AlertDeduplicationService.submit_alert({
        'title': f'Org throughput flatline: no agent transitions in {WINDOW_MINUTES}min',
        'description': description,
        'source': ALERT_SOURCE,
        'item_type': 'alert.error',
        'severity': 'error',
        'priority': 'high',
        'assignment_group': 'devops',
        'correlation_id': 'throughput-watchdog-fleet',
        'labels': {
            'window_minutes': str(WINDOW_MINUTES),
        },
    })
    if _is_new:
        send_alert_notification.delay(_alert.id)

    logger.warning(
        f"sweep_throughput_watchdog: FLATLINE — 0 agent transitions in {WINDOW_MINUTES}min, "
        f"{actionable_count} actionable tasks assigned"
    )
    return {'status': 'alert_fired', 'window_minutes': WINDOW_MINUTES, 'actionable_count': actionable_count}


# ── PLS-49 / ORIG-15: Stale PR status sync ────────────────────────────────

PR_SYNC_SUCCESS_EVENT_KEY = 'pulse-pr-sync:last-success'
PR_SYNC_NO_TOKEN_EVENT_KEY = 'pulse-pr-sync:no-token'
PR_SYNC_GITHUB_AUTH_EVENT_KEY = 'pulse-pr-sync:github-auth'
PR_SYNC_STALE_ALERT_SOURCE = 'pulse-pr-sync-observability'
PR_SYNC_STALE_ALERT_SOURCE_ID = 'linked-pr-sync-no-success'

# PLS-675: stranded merge-event retry (per-item dedupe; not a success heartbeat).
MERGE_EVENT_RETRY_ALERT_SOURCE = 'pulse-deploy-verify-merge-event'
MERGE_EVENT_RETRY_ALERT_SOURCE_PREFIX = 'merge-event-retry-pending:'
MERGE_EVENT_RETRY_MIN_SECONDS = 300
MERGE_EVENT_RETRY_MAX_SECONDS = 21600


def _item_has_merged_linked_pr(item) -> bool:
    prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
    return any((entry.get('status') or '').lower() == 'merged' for entry in prs)


def _merge_event_retry_failure_fingerprint(item) -> str:
    """Stable identity for one unresolved merge-event condition."""
    from .services.workflow_service import WorkflowService

    prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
    merged = sorted(
        (
            str(entry.get('url') or ''),
            str(entry.get('merge_commit_sha') or entry.get('head_sha') or ''),
        )
        for entry in prs
        if (entry.get('status') or '').lower() == 'merged'
    )
    target = WorkflowService.merge_event_target_status(item) or '(none)'
    raw = repr((
        getattr(item, 'workflow_name', '') or '',
        getattr(item, 'status', '') or '',
        target,
        merged,
    )).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _merge_event_retry_due(item, *, now=None) -> bool:
    """Whether this logical failure is eligible for another bounded retry."""
    from django.utils.dateparse import parse_datetime

    now = now or timezone.now()
    meta = dict(getattr(item, 'metadata', None) or {})
    next_at = parse_datetime(str(meta.get('merge_event_retry_next_attempt_at') or ''))
    if next_at is None:
        return True
    if timezone.is_naive(next_at):
        next_at = timezone.make_aware(next_at, timezone.get_current_timezone())
    return next_at <= now


def _record_merge_event_retry_state(item, *, fingerprint, error) -> dict:
    """Persist exponential backoff for this exact unresolved condition."""
    from .models import Item

    now = timezone.now()
    meta = dict(getattr(item, 'metadata', None) or {})
    previous = str(meta.get('merge_event_retry_failure_fingerprint') or '')
    attempts = int(meta.get('merge_event_retry_attempts') or 0) + 1 if previous == fingerprint else 1
    delay = min(
        MERGE_EVENT_RETRY_MAX_SECONDS,
        MERGE_EVENT_RETRY_MIN_SECONDS * (2 ** max(0, attempts - 1)),
    )
    meta.update({
        'merge_event_retry_failure_fingerprint': fingerprint,
        'merge_event_retry_attempts': attempts,
        'merge_event_retry_last_attempt_at': now.isoformat(),
        'merge_event_retry_next_attempt_at': (now + timedelta(seconds=delay)).isoformat(),
        'merge_event_retry_last_error': str(error or '')[:500],
    })
    Item.objects.filter(pk=item.pk).update(metadata=meta)
    item.metadata = meta
    return meta


def _item_has_open_linked_pr(item) -> bool:
    prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
    return any((entry.get('status') or '').lower() == 'open' for entry in prs)



def _attempt_merge_event_retry(item) -> bool:
    """Apply merge-event transition for awaiting_merge + merged PR.

    Returns True when the item advanced off awaiting_merge. Fail-loud path
    records per-task pending evidence without a success heartbeat.

    An item that still has an open linked PR is not stranded — the merge
    event is incomplete. Retrying every historical merged PR on that item
    (PLAT-250: 18 merged GitHub PRs + one open Origin PR) writes a FAILED
    comment per PR per 5-minute sweep.

    PLAT-7066 (Retro 27Aug): an item marked
    ``merge_event_requirement_manifest_pending`` is a Security-side
    requirement-manifest issue, NOT a merge action — the merge lane must not
    retry it (it can never succeed until the manifest exists). Early-return
    (covers the opportunistic PR-sync caller) and route to Security instead
    of arming retry backoff.
    """
    from .models import Item
    from .services.workflow_service import WorkflowService

    status = (getattr(item, 'status', '') or '').strip().lower()
    if status != 'awaiting_merge':
        # PLS-909: the item advanced out of band (manual advance, another
        # reconciler path, etc.) — its stranded per-item retry failure is stale
        # and must be cleared so /statusz stops reporting it as pending. Only
        # THIS item's failure is cleared; unrelated items' pending failures are
        # preserved.
        try:
            from .liveness import clear_merge_event_retry_failure
            clear_merge_event_retry_failure(
                item_key=getattr(item, 'item_key', '') or '',
            )
        except Exception:
            logger.debug(
                'PLS-909 stale retry-failure clear failed for %s',
                getattr(item, 'item_key', item.pk),
                exc_info=True,
            )
        return False
    meta = dict(getattr(item, 'metadata', None) or {})
    if meta.get('merge_event_requirement_manifest_pending'):
        _route_merge_event_requirement_manifest_pending(
            item,
            pr_url=(
                ((meta.get('pull_requests') or [{}])[0] or {}).get('url') or ''
            ),
        )
        return False
    prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
    if any((entry.get('status') or '').lower() == 'open' for entry in prs):
        return False
    merged_entries = [
        entry for entry in prs
        if (entry.get('status') or '').lower() == 'merged'
    ]
    if not merged_entries:
        return False

    fingerprint = _merge_event_retry_failure_fingerprint(item)

    # One apply per item per sweep. Looping every merged PR multiplies the
    # identical fail-loud comment (open_linked_pr / invalid target / etc.).
    entry = merged_entries[0]
    target = WorkflowService.apply_merge_event_transition(
        item,
        user=None,
        pr_html_url=entry.get('url') or '',
        merged_by=entry.get('merged_by') or '',
    )
    item.refresh_from_db(fields=['status', 'metadata'])
    if target and (getattr(item, 'status', '') or '').strip().lower() != 'awaiting_merge':
        return True
    if (getattr(item, 'status', '') or '').strip().lower() != 'awaiting_merge':
        return True
    # PLAT-7066: if the apply recorded a requirement-manifest pending marker
    # (Security-side issue), route to Security instead of generic retry.
    if (getattr(item, 'metadata', None) or {}).get(
        'merge_event_requirement_manifest_pending'
    ):
        _route_merge_event_requirement_manifest_pending(
            item, pr_url=entry.get('url') or '',
        )
        return False
    error = (
        'apply_merge_event_transition returned None while status '
        'remains awaiting_merge'
    )
    try:
        _record_merge_event_retry_state(
            item, fingerprint=fingerprint, error=error,
        )
        _report_merge_event_retry_pending(
            item,
            pr_url=merged_entries[0].get('url') or '',
            error=error,
            failure_fingerprint=fingerprint,
        )
    except Exception:
        logger.exception(
            'PLS-675 merge-event fail-loud path failed for %s',
            getattr(item, 'item_key', item.pk),
        )
    logger.warning(
        'PLS-675 merge-event still pending for %s '
        '(awaiting_merge + merged PR; bounded retry armed)',
        getattr(item, 'item_key', item.pk),
    )
    return False


def _route_merge_event_requirement_manifest_pending(item, *, pr_url=''):
    """Route a merged finding stuck at awaiting_merge to Security (PLAT-7066).

    The merge→verification transition is blocked because no
    VerificationRequirementManifest is frozen (PLS-521 gate). This is a
    Security-side requirement-manifest issue, NOT a merge action — the merge
    lane must not retry it (it can never succeed until the manifest exists).

    Idempotent: a typed ``receipt.merge_event_requirement_manifest_pending``
    mechanical comment (day-bucketed event id) + an idempotent Security task
    to freeze the manifest + a deduped Security-queue alert. Never arms retry
    backoff.
    """
    from datetime import datetime, timezone as dt_timezone
    from .models import Comment, Item
    from .services.mechanical_comments import (
        MechanicalCommentFactory,
        MechanicalCommentError,
        deterministic_event_id,
    )
    from .services.deduplication import AlertDeduplicationService

    item_key = getattr(item, 'item_key', None) or str(item.pk)
    pr = (pr_url or '').strip()
    observed = datetime.now(dt_timezone.utc).isoformat()
    day = observed[:10]

    # 1) Typed pending receipt (idempotent per day, like merge_event_retry_pending).
    eid = deterministic_event_id(
        'merge-event-requirement-manifest', item, day,
    )
    receipt_payload = {
        'item_key': item_key,
        'result': 'routed_to_security',
        'pr_url': pr or '(unknown)',
        'observed_at': f'{day}T00:00:00+00:00',
    }
    try:
        MechanicalCommentFactory.create(
            item=item,
            origin_kind='WORKFLOW_LIFECYCLE',
            origin_event='receipt.merge_event_requirement_manifest_pending',
            template_version=1,
            structured_payload=receipt_payload,
            origin_event_id=eid,
            causal_actor=None,
        )
    except MechanicalCommentError:
        logger.info(
            'PLAT-7066 requirement-manifest pending receipt already recorded '
            'for %s', item_key,
        )
    except Exception:
        logger.exception(
            'PLAT-7066 requirement-manifest pending receipt mint failed for %s',
            item_key,
        )

    # 2) Idempotent Security task to freeze the manifest. One per item.
    try:
        existing = Item.objects.filter(
            mode='task',
            metadata__merge_event_requirement_manifest_freeze_for=item_key,
        ).first()
        if existing is None:
            key = getattr(item.project, 'get_next_item_key', None)
            new_key = key() if key else None
            sec_task = Item.objects.create(
                project=item.project,
                item_key=new_key,
                title=(
                    f'[SECURITY] Freeze VerificationRequirementManifest for '
                    f'{item_key} (PLS-521)'
                ),
                description=(
                    f'**{item_key}** is stuck at `awaiting_merge`: '
                    f'Merge→Verification is blocked because no '
                    f'VerificationRequirementManifest is frozen '
                    f'(requirement_manifest_invalid). A Security-team reviewer '
                    f'must freeze the requirement manifest before this finding '
                    f'can advance (PLS-521 gate).\n\n'
                    f'- item: `{item_key}`\n'
                    f'- pr: `{pr or "unknown"}`\n'
                    f'- parent: `{item_key}` (awaiting_merge)'
                ),
                mode='task',
                status='open',
                workflow_name='simple',
                assignment_group='security',
                created_by_id=0,
                created_by_email='system@shizuha.com',
                metadata={
                    'merge_event_requirement_manifest_freeze_for': item_key,
                    'spawned_from': item_key,
                    'source': 'plat7066-merge-event-requirement-manifest',
                },
            )
            try:
                from .models import ItemLink
                ItemLink.objects.create(
                    source_item=item,
                    target_item=sec_task,
                    link_type='blocks',
                    created_by_id=0,
                    created_by_email='system@shizuha.com',
                )
            except Exception:
                logger.debug(
                    'PLAT-7066 security freeze task link failed for %s',
                    item_key, exc_info=True,
                )
    except Exception:
        logger.exception(
            'PLAT-7066 security freeze task mint failed for %s', item_key,
        )

    # 3) Deduped Security-queue alert.
    source_id = f'merge-event-requirement-manifest:{item_key}'
    try:
        alert_data = {
            'item_type': 'alert.error',
            'title': f'PLS-521 requirement manifest missing: {item_key}',
            'description': (
                f'Task `{item_key}` has a provider-confirmed merged PR but '
                f'cannot advance `awaiting_merge → verification`: no '
                f'VerificationRequirementManifest is frozen '
                f'(requirement_manifest_invalid). ROUTED TO SECURITY — a '
                f'Security-team reviewer must freeze the requirement manifest. '
                f'The merge lane will NOT retry this.\n\n'
                f'- pr: `{pr or "unknown"}`\n'
                f'- item: `{item_key}`\n'
                f'- next action: freeze the VerificationRequirementManifest '
                f'(POST /api/verification-requirement-manifests/)'
            ),
            'source': 'pulse-pls521-requirement-manifest',
            'source_id': source_id,
            'severity': 'error',
            'priority': 'high',
            'project_id': getattr(item, 'project_id', None),
            'assignment_group': 'security',
            'labels': {
                'mechanism': 'deploy_verify_merge_event',
                'item_key': item_key,
                'routed_to': 'security',
            },
            'payload': {
                'item_key': item_key,
                'pr_url': pr,
                'observed_at': observed,
            },
        }
        alert, is_new = AlertDeduplicationService.submit_alert(alert_data)
        if is_new:
            try:
                send_alert_notification.delay(alert.id)
            except Exception:
                logger.exception(
                    'PLAT-7066 requirement-manifest alert notify failed for %s',
                    item_key,
                )
        return alert
    except Exception:
        logger.exception(
            'PLAT-7066 requirement-manifest alert failed for %s', item_key,
        )
        return None


def _drain_merged_awaiting_merge_retries(budget: int, *, touched_item_ids=None) -> int:
    """Dedicated lane: stored merged PR + awaiting_merge, independent of API budget.

    Rei P1 (PLS-675): open/closed provider-check budget must not starve rows
    whose only linked PR is already stored as merged. This lane never calls
    the provider API; it only retries merge-event application with oldest-
    attempt-first rotation.
    """
    from .models import Item

    if budget <= 0:
        return 0
    if touched_item_ids is None:
        touched_item_ids = set()
    attempted = 0
    # Prefer items never attempted, then oldest attempt marker, then id.
    candidates = []
    qs = (
        Item.objects
        .filter(status__iexact='awaiting_merge')
        .exclude(metadata__pull_requests=None)
        .only('id', 'item_key', 'status', 'metadata')
        .iterator(chunk_size=200)
    )
    for item in qs:
        if item.pk in touched_item_ids:
            continue
        if _item_has_open_linked_pr(item):
            continue
        if not _item_has_merged_linked_pr(item):
            continue
        if not _merge_event_retry_due(item):
            continue
        meta = getattr(item, 'metadata', None) or {}
        # PLAT-7066: Security-routed items (requirement manifest missing) must
        # not be retried by the merge lane — they cannot advance until Security
        # freezes the manifest. Skip them entirely.
        if meta.get('merge_event_requirement_manifest_pending'):
            continue
        last = str(meta.get('merge_event_retry_last_attempt_at') or '')
        candidates.append((last, item.pk, item))
    candidates.sort(key=lambda row: (row[0], row[1]))
    for _last, _pk, item in candidates:
        if attempted >= budget:
            break
        touched_item_ids.add(item.pk)
        try:
            _attempt_merge_event_retry(item)
        except Exception as exc:
            logger.warning(
                'PLS-675 dedicated merge-event retry failed for %s: %s',
                getattr(item, 'item_key', item.pk), exc,
            )
        attempted += 1
    return attempted


def _report_merge_event_retry_pending(
    item, *, pr_url='', error='', failure_fingerprint='',
):
    """Fail-loud path when a merged PR remains at awaiting_merge.

    PLS-675 / Rei P1 requirements:
    - typed mechanical receipt on the task (not ordinary comment text alone)
    - failure artifact on /statusz (NOT a success-style heartbeat)
    - deduped owner/lead notification via AlertDeduplicationService
    """
    from datetime import datetime, timezone as dt_timezone
    from .liveness import record_merge_event_retry_failure
    from .services.mechanical_comments import (
        MechanicalCommentFactory,
        MechanicalCommentError,
        deterministic_event_id,
    )
    from .services.deduplication import AlertDeduplicationService

    item_key = getattr(item, 'item_key', None) or str(item.pk)
    err = (error or 'merge-event apply did not advance status').strip()
    pr = (pr_url or '').strip()
    observed = datetime.now(dt_timezone.utc).isoformat()

    # 1) Failure artifact (never a success heartbeat).
    record_merge_event_retry_failure(item_key=item_key, error=err)

    # 2) One typed receipt per LOGICAL failure condition.  A changing
    #    observed_at used to defeat idempotency and mint one comment per retry.
    day = observed[:10]
    fingerprint = failure_fingerprint or _merge_event_retry_failure_fingerprint(item)
    eid = deterministic_event_id('merge-event-retry', item, fingerprint)
    receipt_payload = {
        'item_key': item_key,
        'result': 'pending',
        'pr_url': pr or '(unknown)',
        'error': err[:500],
        'observed_at': f'{day}T00:00:00+00:00',
    }
    try:
        MechanicalCommentFactory.create(
            item=item,
            origin_kind='WORKFLOW_LIFECYCLE',
            origin_event='receipt.merge_event_retry_pending',
            template_version=1,
            structured_payload=receipt_payload,
            origin_event_id=eid,
            causal_actor=None,
        )

    except MechanicalCommentError as exc:
        # A replay of the same logical failure intentionally keeps the first
        # immutable observation.  Payload drift is not a reason to append.
        logger.debug(
            'PLS-675 merge-event pending receipt already recorded for %s: %s',
            item_key, exc,
        )
    except Exception:
        logger.exception(
            'PLS-675 merge-event pending receipt mint failed for %s', item_key,
        )

    # 3) Deduped owner/lead alert + Connect DM.
    owner_email = (
        getattr(item, 'assignee_email', None)
        or getattr(item, 'created_by_email', None)
        or ''
    )
    source_id = f'{MERGE_EVENT_RETRY_ALERT_SOURCE_PREFIX}{item_key}'
    try:
        alert_data = {
            'item_type': 'alert.error',
            'title': f'PLS-675 merge-event retry pending: {item_key}',
            'description': (
                f'Task `{item_key}` has a provider-confirmed merged PR but is '
                f'still at `awaiting_merge` after the Origin reconciler merge-'
                f'event attempt. The success heartbeat was NOT refreshed.\n\n'
                f'- pr: `{pr or "unknown"}`\n'
                f'- error: `{err[:300]}`\n'
                f'- owner: `{owner_email or "unassigned"}`\n'
                f'- liveness: `/statusz` → `deploy_verify_merge_event_last_failure`\n'
                f'- next action: inspect the task; the reconciler retries with bounded exponential backoff'
            ),
            'source': MERGE_EVENT_RETRY_ALERT_SOURCE,
            'source_id': source_id,
            'severity': 'error',
            'priority': 'high',
            'project_id': getattr(item, 'project_id', None),
            'assignment_group': (
                getattr(item, 'assignment_group', None) or 'engineering'
            ),
            'labels': {
                'mechanism': 'deploy_verify_merge_event',
                'item_key': item_key,
            },
            'payload': {
                'item_key': item_key,
                'pr_url': pr,
                'error': err[:500],
                'owner_email': owner_email,
                'observed_at': observed,
            },
        }
        if owner_email:
            alert_data['assignee_email'] = owner_email
        alert, is_new = AlertDeduplicationService.submit_alert(alert_data)
        if is_new:
            try:
                send_alert_notification.delay(alert.id)
            except Exception:
                logger.exception(
                    'PLS-675 merge-event pending alert notify failed for %s',
                    item_key,
                )
        return alert
    except Exception:
        logger.exception(
            'PLS-675 merge-event pending alert failed for %s', item_key,
        )
        return None


def _linked_pr_provider_for_url(url: str):
    """Classify a linked-PR URL without making an API request."""
    import re
    from .origin_provenance import parse_origin_pr_reference

    url = (url or '').rstrip('/')
    if re.match(r'https://github\.com/[^/]+/[^/]+/pull/\d+$', url, re.IGNORECASE):
        return 'github'
    if parse_origin_pr_reference(url):
        return 'forgejo'
    return None


def _linked_pr_identity(url: str):
    """Return provider-neutral coordinates for bounce-map reconciliation."""
    from .origin_provenance import parse_linked_pr_identity

    return parse_linked_pr_identity(url)


def _pr_sync_commit_sha():
    """Stable pseudo-SHA for runtime-generated PR-sync observability events."""
    import os
    import hashlib

    raw = (
        os.environ.get('GITHUB_SHA')
        or os.environ.get('COMMIT_SHA')
        or os.environ.get('IMAGE_TAG')
        or 'pulse-linked-pr-sync-runtime'
    )
    cleaned = str(raw).strip()
    if cleaned and len(cleaned) <= 64 and re.match(r'^[A-Za-z0-9_.:-]+$', cleaned):
        return cleaned[:64]
    return hashlib.sha256(cleaned.encode('utf-8')).hexdigest()


def _record_pr_sync_event(*, idempotency_key, state, smoke_result, reason,
                          checked=0, updated=0, metadata=None):
    """Record linked-PR sync health in the PLS-183 deployment-event ledger.

    PLS-213 deliberately reuses the existing ledger/failure-task automation
    rather than adding a second sweep. Reusing fixed idempotency keys makes the
    row a "last success/failure" gauge: retries update one row/task instead of
    spamming Pulse.
    """
    from tasks.services.deployment_events import upsert_deployment_event

    payload = {
        'idempotency_key': idempotency_key,
        'project_key': 'PLS',
        'repo': 'shizuha-labs/shizuha-tasks',
        'service': 'pulse-linked-pr-sync',
        'component': 'sync_linked_pr_statuses',
        'environment': 'live',
        'cluster': 'shizuha-k3s',
        'commit_sha': _pr_sync_commit_sha(),
        'pipeline_system': 'pulse-celery-beat',
        'pipeline_run_id': idempotency_key,
        'state': state,
        'smoke_result': smoke_result,
        'metadata': {
            'reason': reason,
            'checked': checked,
            'updated': updated,
            **(metadata or {}),
        },
    }
    return upsert_deployment_event(payload)


def _alert_pr_sync_no_success_if_stale(*, reason, checked=0, updated=0, metadata=None):
    """Fire a deduped alert when failures mean there has been no recent success."""
    from django.utils.timezone import now as _now
    from .models import DeploymentEvent
    from .services.deduplication import AlertDeduplicationService

    success = DeploymentEvent.objects.filter(
        idempotency_key=PR_SYNC_SUCCESS_EVENT_KEY,
        state='smoke_passed',
        smoke_result='passed',
    ).first()
    age_seconds = None
    if success and success.updated_at:
        age_seconds = int((_now() - success.updated_at).total_seconds())
        if age_seconds <= 30 * 60:
            return None

    description_bits = [
        "Pulse linked-PR status sync has no successful run in the last 30 minutes.",
        "",
        f"- reason: `{reason}`",
        f"- checked in failing run: `{checked}`",
        f"- updated in failing run: `{updated}`",
    ]
    if success and success.updated_at:
        description_bits.append(f"- last success: `{success.updated_at.isoformat()}` ({age_seconds}s ago)")
    else:
        description_bits.append("- last success: `never recorded`")
    if metadata:
        description_bits.append(f"- metadata: `{metadata}`")

    alert, _is_new = AlertDeduplicationService.submit_alert({
        'item_type': 'alert.error',
        'title': 'Pulse linked-PR sync has no successful run in >30m',
        'description': '\n'.join(description_bits),
        'source': PR_SYNC_STALE_ALERT_SOURCE,
        'source_id': PR_SYNC_STALE_ALERT_SOURCE_ID,
        'severity': 'error',
        'priority': 'high',
        'assignment_group': 'devops',
        'labels': {
            'service': 'pulse-linked-pr-sync',
            'reason': reason,
        },
        'payload': metadata or {},
    })
    if _is_new:
        try:
            send_alert_notification.delay(alert.id)
        except Exception:
            logger.exception("sync_linked_pr_statuses: failed to notify stale-success alert")
    return alert


def _resolve_pr_sync_stale_alert():
    from .models import Item

    for alert in Item.objects.filter(
        mode='alert',
        source=PR_SYNC_STALE_ALERT_SOURCE,
        source_id=PR_SYNC_STALE_ALERT_SOURCE_ID,
        status__in=['firing', 'acknowledged'],
    ):
        alert.resolve(
            resolved_by='pr-sync@shizuha.local',
            note='sync_linked_pr_statuses recorded a successful run.',
        )


def _linked_pr_api_request(url: str, *, github_token: str, forgejo_token: str, origin_url: str):
    """Return (provider, owner, repo, number, response) for a linked PR URL."""
    import re
    from .origin_provenance import (
        parse_origin_pr_reference,
        parse_origin_pr_legacy_singular_url,
    )

    url = (url or '').rstrip('/')

    gh_match = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$', url, re.IGNORECASE)
    if gh_match:
        if not github_token:
            return None
        owner, repo, number = gh_match.groups()
        headers = {
            'Authorization': f'Bearer {github_token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        api_url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{number}'
        return 'github', owner, repo, number, requests.get(api_url, headers=headers, timeout=10)

    # Canonical plural Origin URL, or already-persisted legacy singular
    # ``/pull/N`` row (PLS-834). Coordinates still come only from the exact
    # public-host parsers; the token always targets operator-configured origin_url.
    origin_coords = parse_origin_pr_reference(url) or parse_origin_pr_legacy_singular_url(url)
    if origin_coords:
        if not forgejo_token:
            return None
        # PLAT-4675 P1 (kai review): the token target is the operator-configured
        # `origin_url` (a trusted in-cluster/public Origin base) — NEVER a host
        # derived from the (possibly attacker-supplied) linked PR URL. Fail closed
        # when origin_url is unset instead of falling back to the URL's own netloc
        # (the previous `origin_url or f'{scheme}://{netloc}'` could send the token
        # to an arbitrary host).
        base = (origin_url or '').rstrip('/')
        if not base:
            return None
        owner, repo, number = origin_coords
        headers = {
            'Authorization': f'token {forgejo_token}',
            'Accept': 'application/json',
        }
        api_url = f'{base}/api/v1/repos/{owner}/{repo}/pulls/{number}'
        return 'forgejo', owner, repo, number, requests.get(api_url, headers=headers, timeout=10)

    return None


def _github_auth_probe(github_token: str):
    """Independent GET /user auth probe for the GitHub token (PLS-979).

    Per-PR GitHub 401/403 responses can be transient provider errors (rate
    limit, per-request flake) and are NOT proof the credential is invalid. The
    only authoritative signal that the token itself is bad is a 401/403 from a
    dedicated ``GET /user`` probe using the same token. Returns
    ``(status_code, request_id, message)``; ``status_code == 0`` means the
    probe itself failed to reach GitHub (transport error) — not a credential
    verdict.
    """
    if not github_token:
        return 0, '', 'no token to probe'
    try:
        resp = requests.get(
            'https://api.github.com/user',
            headers={
                'Authorization': f'Bearer {github_token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        return 0, '', f'probe transport failure: {exc}'
    request_id = (getattr(resp, 'headers', {}) or {}).get('X-GitHub-Request-Id', '').strip()
    return resp.status_code, request_id, ''


def _linked_pr_status_from_payload(provider: str, data: dict):
    """Normalize GitHub/Forgejo pull-request payloads to Pulse PR metadata.

    PLS-441: also extracts ``head_sha`` for OPEN responses so the reconciler
    can backfill head provenance for the review re-entry gate.
    """
    state = (data.get('state') or '').lower()
    merged = bool(data.get('merged') or data.get('has_merged'))
    head_sha = ((data.get('head') or {}).get('sha') or '').strip()

    if merged:
        merger = data.get('merged_by') or data.get('merge_user') or {}
        merge_commit = (
            data.get('merge_commit_sha') or data.get('merge_commit') or ''
        ).strip()
        result = {
            'status': 'merged',
            'merged_at': data.get('merged_at') or data.get('merged'),
            'merged_by': merger.get('login') if isinstance(merger, dict) else None,
        }
        if head_sha:
            result['head_sha'] = head_sha
        if merge_commit:
            result['merge_commit_sha'] = merge_commit
        return result
    if state == 'closed':
        result = {'status': 'closed'}
        if head_sha:
            result['head_sha'] = head_sha
        return result
    # OPEN — return head_sha for provenance backfill (PLS-441)
    result = {'status': 'open'}
    if head_sha:
        result['head_sha'] = head_sha
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=60,
             soft_time_limit=60, time_limit=90)
def reingest_merge_correlated_deployment_events(
    self, *, repo: str, merge_commit_sha: str, item_key: str,
) -> int:
    """PLS-1303: merge-time re-ingest of deployment events for this commit.

    Scheduled via transaction.on_commit when a merge attestation is recorded.
    Idempotent: events already correlated to the task are skipped, and the
    underlying upsert is keyed by the producer's idempotency_key, so Celery
    redeliveries are no-ops (at-least-once, PLAT-8994 shape). Never invents an
    event — repos without a deploy producer stay untouched (emitter-gap
    family is a separate workstream).
    """
    from .services.deployment_events import reingest_events_for_merge

    try:
        count = reingest_events_for_merge(
            repo=repo,
            merge_commit_sha=merge_commit_sha,
            item_key=item_key,
        )
    except Exception:
        logger.exception(
            'PLS-1303 merge-correlated deployment-event re-ingest failed for %s %s@%s',
            item_key, repo, merge_commit_sha,
        )
        raise self.retry(exc=None)
    if count:
        logger.info(
            'PLS-1303 re-correlated %d deployment event(s) for %s %s@%s',
            count, item_key, repo, merge_commit_sha,
        )
    return count


@shared_task(bind=True, max_retries=0, soft_time_limit=240, time_limit=300)
def backfill_merge_correlated_deployment_events(self) -> dict:
    """PLS-1303 periodic healer for the merge→deploy-event correlation leg.

    Recovers (a) merge-time schedules lost to worker death and (b) the
    historical stranded cohort whose merges were attested before the
    merge-time schedule existed. Bounded (limit 50 re-ingests per run) and
    fail-closed: only attested merges whose commit already has a stored
    deployment event are touched.
    """
    from .services.deployment_events import backfill_merge_correlated_events

    try:
        healed = backfill_merge_correlated_events()
    except Exception:
        logger.exception('PLS-1303 merge-correlation backfill sweep failed')
        return {'scanned': 0, 'reingested': 0, 'events': 0, 'error': True}
    if healed['reingested']:
        logger.info(
            'PLS-1303 backfill re-correlated %d event(s) across %d task(s) (scanned %d)',
            healed['events'], healed['reingested'], healed['scanned'],
        )
    return healed


@shared_task(bind=True, max_retries=0, soft_time_limit=90, time_limit=120)
def sync_linked_pr_statuses(self, limit: int = 200):
    """
    PLS-49 / ORIG-15: reconcile linked PR statuses with GitHub and Origin/Forgejo.

    Reconciles both open and closed-looking linked PR rows.  The latter matters
    because a PR can be reopened (or a missed/out-of-order webhook can leave a
    false closed state); treating ``closed`` as immutable made an actually-open
    PR look finished forever (PLS-607).  ``limit`` is an API-check budget, not a
    candidate-row cap.  A provider-confirmed merge remains irreversible.
    """
    import os

    # The Pulse pods run one Celery slot each. A worst-case 200 × 10s external
    # PR scan used to occupy both slots across overlapping beat deliveries for
    # 15+ minutes, starving availability/routing sweeps for the entire fleet.
    # Stop gracefully before the task-level kill switch so partial progress is
    # recorded and the worker returns to latency-sensitive sweeps.
    try:
        run_budget_seconds = int(os.environ.get('PULSE_PR_SYNC_RUN_BUDGET_SECONDS', '75'))
    except (TypeError, ValueError):
        run_budget_seconds = 75
    # Keep at least one full 10s HTTP timeout plus 5s bookkeeping headroom
    # before the 90s Celery soft limit.
    run_budget_seconds = max(10, min(run_budget_seconds, 75))
    deadline = time.monotonic() + run_budget_seconds

    github_token = os.environ.get('GITHUB_TOKEN', '')
    forgejo_token = (
        os.environ.get('FORGEJO_TOKEN')
        or os.environ.get('ORIGIN_TOKEN')
        or os.environ.get('GITEA_TOKEN')
        or os.environ.get('TEA_TOKEN')
        or ''
    )
    origin_url = (
        os.environ.get('ORIGIN_URL')
        or os.environ.get('FORGEJO_URL')
        or 'https://origin.shizuha'
    ).rstrip('/')

    if not github_token and not forgejo_token:
        logger.warning("sync_linked_pr_statuses: no GitHub or Forgejo token set — skipping PR sync")
        event, _created, failure_task = _record_pr_sync_event(
            idempotency_key=PR_SYNC_NO_TOKEN_EVENT_KEY,
            state='smoke_failed',
            smoke_result='failed',
            reason='no_token',
        )
        _alert_pr_sync_no_success_if_stale(reason='no_token')
        return {
            'skipped': True,
            'reason': 'no_token',
            'event_id': str(event.id),
            'devops_task_key': failure_task.item_key if failure_task else None,
        }

    from django.utils.timezone import now as _now
    from .models import Item, ItemActivity, LinkedPullRequestMergeAttestation

    updated_count = 0
    checked_count = 0
    api_error_count = 0
    github_auth_error_count = 0
    github_auth_observed = False
    github_auth_request_ids = []
    missing_token_counts = {'github': 0, 'forgejo': 0}
    open_touched_item_ids = set()
    terminal_touched_item_ids = set()
    merge_retry_touched_item_ids = set()
    checked_entry_keys = set()
    budget_exhausted = False

    def _candidate_items(ordering):
        return (
            Item.objects
            .filter(metadata__pull_requests__isnull=False)
            .order_by(*ordering)
            .only('id', 'item_key', 'metadata')
            .iterator(chunk_size=200)
        )

    def _drain_candidates(
            candidates,
            budget,
            *,
            eligible_statuses,
            touched_item_ids,
            persist_sync_timestamp=False):
        nonlocal checked_count, updated_count, api_error_count, github_auth_error_count, \
            github_auth_observed, github_auth_request_ids, budget_exhausted
        if budget <= 0:
            return 0
        pass_checked = 0

        for item in candidates:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
            if pass_checked >= budget or checked_count >= limit:
                break
            if item.pk in touched_item_ids:
                continue

            prs = (item.metadata or {}).get('pull_requests') or []
            changed = False
            provider_confirmed_heads = {}

            indexed_entries = list(enumerate(prs))
            if persist_sync_timestamp:
                # Process the stalest closed-looking siblings first.  This,
                # plus persisting their successful check timestamp, gives the
                # bounded terminal audit pass deterministic rotation instead of
                # rechecking the same first N rows forever.
                indexed_entries.sort(
                    key=lambda pair: str(
                        pair[1].get('last_synced_at')
                        or pair[1].get('linked_at')
                        or ''
                    )
                )

            sync_timestamp_dirty = False
            for entry_index, entry in indexed_entries:
                if time.monotonic() >= deadline:
                    budget_exhausted = True
                    break
                if pass_checked >= budget or checked_count >= limit:
                    break
                entry_status = (entry.get('status') or 'open').lower()
                needs_merge_attestation = False
                if entry_status == 'merged':
                    repo = (entry.get('repo') or '').strip().lower()
                    number = entry.get('number')
                    has_attestation = bool(
                        repo and number and LinkedPullRequestMergeAttestation.objects.filter(
                            item=item, repo=repo, number=number,
                        ).exists()
                    )
                    # A caller may claim "merged" in mutable metadata. Query the
                    # provider until an immutable server attestation exists.
                    needs_merge_attestation = not has_attestation
                if entry_status not in eligible_statuses and not needs_merge_attestation:
                    continue
                entry_key = (
                    item.pk,
                    (entry.get('url') or f'entry:{entry_index}').rstrip('/'),
                )
                if entry_key in checked_entry_keys:
                    continue
                provider_hint = _linked_pr_provider_for_url(entry.get('url') or '')
                if provider_hint == 'github' and not github_token:
                    missing_token_counts['github'] += 1
                    continue
                if provider_hint == 'forgejo' and not forgejo_token:
                    missing_token_counts['forgejo'] += 1
                    continue

                try:
                    request_result = _linked_pr_api_request(
                        entry.get('url') or '',
                        github_token=github_token,
                        forgejo_token=forgejo_token,
                        origin_url=origin_url,
                    )
                except Exception as exc:
                    logger.debug("sync_linked_pr_statuses: parse %s failed: %s", entry.get('url'), exc)
                    continue

                if request_result is None:
                    continue

                provider, owner, repo, number, resp = request_result
                checked_entry_keys.add(entry_key)
                checked_count += 1
                pass_checked += 1

                if resp.status_code == 404:
                    continue
                if not resp.ok:
                    api_error_count += 1
                    if provider == 'github' and resp.status_code in (401, 403):
                        github_auth_error_count += 1
                        github_auth_observed = True
                        request_id = (getattr(resp, 'headers', {}) or {}).get(
                            'X-GitHub-Request-Id', '').strip()
                        if request_id and len(github_auth_request_ids) < 5:
                            github_auth_request_ids.append(request_id)
                    logger.warning(
                        "sync_linked_pr_statuses: %s %s/%s#%s → HTTP %s",
                        provider, owner, repo, number, resp.status_code,
                    )
                    continue

                try:
                    data = resp.json()
                except Exception:
                    continue

                normalized = _linked_pr_status_from_payload(provider, data)
                if not normalized:
                    continue

                old_status = entry.get('status') or 'open'
                new_status = normalized['status']
                status_changed = old_status != new_status
                entry['status'] = new_status
                entry['last_synced_at'] = _now().isoformat()
                if persist_sync_timestamp:
                    sync_timestamp_dirty = True
                if new_status == 'merged':
                    entry['merged_at'] = normalized.get('merged_at')
                    entry['merged_by'] = normalized.get('merged_by')
                    normalized_merge_commit = (
                        normalized.get('merge_commit_sha') or ''
                    ).strip().lower()
                    if normalized_merge_commit and (
                        entry.get('merge_commit_sha') != normalized_merge_commit
                    ):
                        entry['merge_commit_sha'] = normalized_merge_commit
                        changed = True

                # PLS-441: persist head_sha from API response for provenance
                normalized_head = (normalized.get('head_sha') or '').strip()
                if normalized_head and new_status == 'open':
                    provider_confirmed_heads[entry_key[1]] = normalized_head
                if normalized_head and entry.get('head_sha') != normalized_head:
                    entry['head_sha'] = normalized_head
                    changed = True

                # HIVE-694 (2026-07-12): only log activity on a REAL transition.
                # This used to create a pr_status_changed row UNCONDITIONALLY —
                # "open → open" for every still-open linked PR on every sweep
                # (~1.3k no-op rows/day, 43% of all recent ItemActivity), which
                # flooded the Home dashboard live feed with "PR sync" spam
                # (operator report). The webhook path (views.GitHubWebhookView)
                # and the signals differ already guard on an actual change; this
                # sweep is now consistent with them. last_synced_at only
                # persists when something real changed — nothing reads it, and
                # skipping the no-op metadata UPDATE also drops a pointless DB
                # write per open PR per sweep.
                if status_changed:
                    label = f"#{number} {owner}/{repo}"
                    ItemActivity.objects.create(
                        item_id=item.pk,
                        action='pr_status_changed',
                        field_name='Pull Request',
                        old_value=f"{label} — {old_status}",
                        new_value=f"{label} — {new_status}",
                        user_id=0,
                        user_email='pr-sync@shizuha.local',
                    )
                    changed = True

                if new_status == 'merged':
                    try:
                        from .models import VerificationRequirementManifest
                        from .services.deployment_evidence import (
                            invalidate_evidence,
                            record_merge_attestation,
                        )
                        # The provider attestation and newer-merge invalidation
                        # are one transaction.  If invalidation fails the new
                        # attestation rolls back, so a later sweep retries it
                        # instead of treating stale evidence as current forever.
                        with transaction.atomic():
                            _attestation, attestation_created = record_merge_attestation(
                                item=item,
                                provider=provider,
                                repo=f'{owner}/{repo}',
                                number=int(number),
                                url=entry.get('url') or '',
                                head_sha=normalized.get('head_sha') or '',
                                merge_commit=normalized.get('merge_commit_sha') or '',
                                merged_at=normalized.get('merged_at'),
                                attested_by='pr-sync',
                            )
                            # PLS-1303: schedule the correlated deployment-event
                            # re-ingest at merge time. CI producers may have
                            # emitted the deploy event for this commit before the
                            # merge was attested (stored with empty/partial
                            # source_task_keys); re-ingesting now that the linked-PR
                            # tuple carries the provider merge identity lets the
                            # correlation fill in truthfully. on_commit keeps the
                            # schedule inside the attestation's atomicity; the task
                            # itself is idempotent (key already present = no-op) so
                            # replays are safe (PLAT-8994 at-least-once shape).
                            if attestation_created and normalized.get('merge_commit_sha'):
                                _merge_sha = normalized['merge_commit_sha']
                                _merge_repo = f'{owner}/{repo}'
                                _merge_key = item.item_key or ''
                                transaction.on_commit(
                                    lambda repo=_merge_repo, sha=_merge_sha, key=_merge_key:
                                    reingest_merge_correlated_deployment_events.apply_async(
                                        kwargs={
                                            'repo': repo,
                                            'merge_commit_sha': sha,
                                            'item_key': key,
                                        },
                                    )
                                )
                            if attestation_created and VerificationRequirementManifest.objects.filter(
                                item=item, is_current=True,
                            ).exists():
                                invalidate_evidence(
                                    item=item,
                                    reason='newer_merge',
                                    actor_email='pr-sync@shizuha.local',
                                )
                    except Exception:
                        logger.exception(
                            'PLS-521 failed loud: could not attest/invalidate provider merge for %s %s/%s#%s',
                            item.item_key or item.pk, owner, repo, number,
                        )
                        # Do not emit a successful reconciler receipt after a
                        # trusted merge was observed but its stale-evidence
                        # fence could not be committed.  The atomic block above
                        # rolled the attestation back, so the next scheduled run
                        # will retry the exact provider observation.
                        raise

            touched_item_ids.add(item.pk)

            # PLS-441 / PLS-684: backfill bounce provenance for existing
            # bounced tasks with an absent, partial, or explicit-null map.
            # Only heads confirmed by THIS provider pass are eligible; a stored
            # sibling head that missed the bounded API budget is not trusted.
            # Populated historical snapshots remain immutable, and a row first
            # linked after a no-PR bounce keeps the stricter first-artifact path.
            meta = dict(item.metadata or {})
            raw_bounced_heads = meta.get('review_bounce_pr_heads')
            if (
                meta.get('review_bounce_at')
                and (
                    raw_bounced_heads is None
                    or isinstance(raw_bounced_heads, dict)
                )
            ):
                from django.utils.dateparse import parse_datetime

                bounce_at = parse_datetime(str(meta.get('review_bounce_at') or ''))
                if bounce_at and timezone.is_naive(bounce_at):
                    bounce_at = timezone.make_aware(
                        bounce_at, timezone.get_current_timezone(),
                    )
                bounced_heads = dict(raw_bounced_heads or {})
                backfilled = False

                for pe in prs:
                    pr_url = str(pe.get('url') or '').strip().rstrip('/')
                    pr_key = pr_url or str(
                        pe.get('number') or pe.get('repo') or ''
                    )
                    confirmed_head = provider_confirmed_heads.get(pr_key)
                    if not pr_key or not confirmed_head:
                        continue

                    pr_identity = _linked_pr_identity(pr_url)
                    matching_key = next(
                        (
                            key for key in bounced_heads
                            if str(key or '').strip().rstrip('/') == pr_key
                        ),
                        None,
                    )
                    identity_only_match = False
                    if matching_key is None and pr_identity:
                        matching_key = next(
                            (
                                key for key in bounced_heads
                                if _linked_pr_identity(key) == pr_identity
                            ),
                            None,
                        )
                        identity_only_match = matching_key is not None
                    if matching_key is not None:
                        if str(bounced_heads.get(matching_key) or '').strip():
                            continue
                        if identity_only_match:
                            # Replace a valueless legacy representation rather
                            # than adding a second canonical key beside it.
                            bounced_heads.pop(matching_key, None)
                            target_key = pr_key
                        else:
                            target_key = matching_key
                    else:
                        linked_at = parse_datetime(
                            str(pe.get('linked_at') or '')
                        )
                        if linked_at and timezone.is_naive(linked_at):
                            linked_at = timezone.make_aware(
                                linked_at, timezone.get_current_timezone(),
                            )
                        if not bounce_at or not linked_at or linked_at > bounce_at:
                            continue
                        target_key = pr_key

                    bounced_heads[target_key] = confirmed_head
                    backfilled = True

                if backfilled:
                    meta['review_bounce_pr_heads'] = bounced_heads
                    meta['review_bounce_backfilled_at'] = _now().isoformat()
                    meta['review_bounce_backfill_source'] = (
                        'sync_linked_pr_statuses'
                    )
                    item.metadata = meta
                    changed = True

            if changed or sync_timestamp_dirty:
                Item.objects.filter(pk=item.pk).update(metadata=item.metadata)
            if changed:
                updated_count += 1
            # Opportunistic merge-event retry when this item was already in the
            # open/closed API lane (e.g. open→merged transition this sweep).
            # Skip if the dedicated lane already attempted this pk this run.
            if (
                item.pk not in merge_retry_touched_item_ids
                and any((entry.get('status') or '').lower() == 'merged' for entry in prs)
            ):
                try:
                    full_item = Item.objects.get(pk=item.pk)
                    if _merge_event_retry_due(full_item):
                        merge_retry_touched_item_ids.add(item.pk)
                        _attempt_merge_event_retry(full_item)
                except Exception as exc:
                    logger.warning(
                        "PLS-675 merge-event transition failed during PR sync for %s: %s",
                        getattr(item, 'item_key', item.pk), exc,
                    )
                if changed:
                    try:
                        from .services.recurring_cycle_close import close_recurring_cycle_if_complete
                        for entry in prs:
                            if (entry.get('status') or '').lower() != 'merged':
                                continue
                            close_recurring_cycle_if_complete(
                                item,
                                trigger_kind='PR',
                                trigger_label=entry.get('url') or f"{owner}/{repo}#{number}",
                                actor_email='pr-sync@shizuha.local',
                                # PLS-855: bind the merged PR to the generation whose
                                # window contains its merge date so a late/stale merge
                                # cannot close the CURRENT cycle.
                                trigger_at=entry.get('merged_at'),
                            )
                    except Exception as exc:
                        try:
                            from .services.recurring_cycle_close import alert_recurring_cycle_auto_close_failure
                            alert_recurring_cycle_auto_close_failure(
                                item,
                                context='linked_pr_status_sync',
                                exc=exc,
                            )
                        except Exception:
                            logger.exception("PLAT-1353 recurring-cycle fail-loud path failed during PR sync")
                        logger.warning(
                            "PLAT-1353 recurring-cycle auto-close failed during PR sync for %s",
                            getattr(item, 'item_key', item.pk),
                            exc_info=True,
                        )

        return pass_checked

    def _terminal_candidate_items():
        candidates = []
        for item in _candidate_items(('id',)):
            terminal_entries = [
                entry for entry in ((item.metadata or {}).get('pull_requests') or [])
                if (entry.get('status') or 'open') == 'closed'
            ]
            if not terminal_entries:
                continue
            oldest_sync = min(
                str(entry.get('last_synced_at') or entry.get('linked_at') or '')
                for entry in terminal_entries
            )
            candidates.append((oldest_sync, item.pk, item))
        candidates.sort(key=lambda row: (row[0], row[1]))
        return (row[2] for row in candidates)

    def _stale_open_candidate_items():
        """PLS-784: items whose OPEN-stored linked PRs are the stalest.

        The recent (`-updated_at`) pass only covers recently-touched items and
        the low-id pass only covers the first N rows by primary key — a stale
        'open' entry on a mid/high-id item that was actually merged upstream
        (or otherwise changed) can starve forever, leaving Pulse disagreeing
        with the provider in the open->merged direction (PLS-600/#118). This
        rotation mirrors the terminal ('closed') pass: oldest-sync-first with
        successful no-op checks persisting `last_synced_at`, so every open
        entry eventually rotates through the provider check.
        """
        candidates = []
        for item in _candidate_items(('id',)):
            open_entries = [
                entry for entry in ((item.metadata or {}).get('pull_requests') or [])
                if (entry.get('status') or 'open') == 'open'
            ]
            if not open_entries:
                continue
            oldest_sync = min(
                str(entry.get('last_synced_at') or entry.get('linked_at') or '')
                for entry in open_entries
            )
            candidates.append((oldest_sync, item.pk, item))
        candidates.sort(key=lambda row: (row[0], row[1]))
        return (row[2] for row in candidates)

    # Preserve the open-row fast path, while reserving a bounded quarter of each
    # multi-check run for closed-looking rows.  The terminal lane is oldest-sync
    # first and records successful no-op checks, so every row eventually rotates
    # through without expanding the task's wall-clock/API budget.
    #
    # PLS-675 / Rei P1: ALSO reserve a dedicated merged-awaiting retry budget that
    # does NOT share the open/closed provider API counter. Stored-merged rows
    # otherwise starve forever behind open-lane API checks.
    #
    # Rei P2: the dedicated lane makes no provider API call, so a positive run
    # budget (including the supported bounded invocation limit=1) must always
    # reserve ≥1 merge-retry attempt. `0 if limit <= 1` disabled the lane for a
    # valid positive budget and is forbidden.
    terminal_budget = 0 if limit <= 1 else max(1, limit // 4)
    merge_retry_budget = max(1, limit // 4) if limit > 0 else 0
    # PLS-784: reserve a bounded quarter for the stale-open rotation so old
    # mid/high-id items with open-stored entries (possibly merged upstream)
    # cannot starve behind the recent/low-id fast paths.
    stale_open_budget = 0 if limit <= 1 else max(1, limit // 4)
    open_budget = max(0, limit - terminal_budget - stale_open_budget)
    recent_open_budget = open_budget if open_budget <= 1 else max(1, open_budget // 2)

    # Dedicated lane FIRST so open-lane budget exhaustion cannot skip it.
    merge_retry_attempted = _drain_merged_awaiting_merge_retries(
        merge_retry_budget,
        touched_item_ids=merge_retry_touched_item_ids,
    )

    open_checked = _drain_candidates(
        _candidate_items(('-updated_at', 'id')),
        recent_open_budget,
        eligible_statuses={'open'},
        touched_item_ids=open_touched_item_ids,
    )
    remaining_open_budget = open_budget - open_checked
    if remaining_open_budget > 0 and not budget_exhausted:
        _drain_candidates(
            _candidate_items(('id',)),
            remaining_open_budget,
            eligible_statuses={'open'},
            touched_item_ids=open_touched_item_ids,
        )

    if terminal_budget > 0 and not budget_exhausted:
        _drain_candidates(
            _terminal_candidate_items(),
            terminal_budget,
            eligible_statuses={'closed'},
            touched_item_ids=terminal_touched_item_ids,
            persist_sync_timestamp=True,
        )

    # PLS-784: stale-open rotation — oldest-sync-first, persisting no-op check
    # timestamps so every open-stored entry eventually re-checks the provider
    # (catching open->merged transitions on old mid/high-id items).
    if stale_open_budget > 0 and not budget_exhausted:
        _drain_candidates(
            _stale_open_candidate_items(),
            stale_open_budget,
            eligible_statuses={'open'},
            touched_item_ids=open_touched_item_ids,
            persist_sync_timestamp=True,
        )

    # If one lane had fewer candidates than its reservation, let the open lane
    # consume the spare checks; open/merge convergence remains the urgent path.
    remaining = limit - checked_count
    if remaining > 0 and not budget_exhausted:
        _drain_candidates(
            _candidate_items(('id',)),
            remaining,
            eligible_statuses={'open'},
            touched_item_ids=open_touched_item_ids,
        )
    # Note: do NOT re-run the dedicated merge-retry lane in the same sweep —
    # each candidate gets one attempt per beat tick; rotation across ticks is
    # via merge_event_retry_last_attempt_at. A same-run second pass would
    # double-apply and hide transient first-failure fail-loud evidence.
    _ = merge_retry_attempted  # retained for future metrics/logging
    logger.info(
        "sync_linked_pr_statuses: checked=%d prs, updated=%d items, api_errors=%d, "
        "missing_tokens=%s budget_exhausted=%s",
        checked_count,
        updated_count,
        api_error_count,
        missing_token_counts,
        budget_exhausted,
    )
    failure_task = None
    failure_reason = None
    event = None
    github_auth_probe = None
    if missing_token_counts['github'] > 0:
        failure_reason = 'missing_github_token'
    elif github_auth_observed:
        # PLS-979: a per-PR GitHub 401/403 is NOT proof the credential is
        # invalid (transient rate-limit / per-request flake). Perform at most
        # one dedicated GET /user auth probe per sweep; only a 401/403 from the
        # probe itself is a credential verdict. If the probe is 200, keep the
        # PR failures as provider/API errors without a credential-failure event.
        probe_status, probe_request_id, probe_message = _github_auth_probe(github_token)
        github_auth_probe = {
            'status_code': probe_status,
            'request_id': probe_request_id,
            'message': probe_message,
        }
        if probe_status in (401, 403):
            failure_reason = 'github_token_invalid'
    if failure_reason:
        event, _failure_created, failure_task = _record_pr_sync_event(
            idempotency_key=PR_SYNC_GITHUB_AUTH_EVENT_KEY,
            state='smoke_failed',
            smoke_result='failed',
            reason=failure_reason,
            checked=checked_count,
            updated=updated_count,
            metadata={
                'api_errors': api_error_count,
                'github_auth_errors': github_auth_error_count,
                'github_auth_probe': github_auth_probe,
                'github_auth_request_ids': github_auth_request_ids,
                'missing_token_counts': missing_token_counts,
                'budget_exhausted': budget_exhausted,
            },
        )
        _alert_pr_sync_no_success_if_stale(
            reason=failure_reason,
            checked=checked_count,
            updated=updated_count,
            metadata={'failure_event_id': str(event.id)},
        )
    else:
        event, _created, _failure_task = _record_pr_sync_event(
            idempotency_key=PR_SYNC_SUCCESS_EVENT_KEY,
            state='smoke_passed',
            smoke_result='passed',
            reason='partial_budget' if budget_exhausted else 'success',
            checked=checked_count,
            updated=updated_count,
            metadata={
                'api_errors': api_error_count,
                'github_auth_errors': github_auth_error_count,
                'github_auth_probe': github_auth_probe,
                'github_auth_request_ids': github_auth_request_ids,
                'missing_token_counts': missing_token_counts,
                'budget_exhausted': budget_exhausted,
            },
        )
        _resolve_pr_sync_stale_alert()

    return {
        'checked': checked_count,
        'updated': updated_count,
        'api_errors': api_error_count,
        'github_auth_errors': github_auth_error_count,
        'github_auth_probe': github_auth_probe,
        'github_auth_request_ids': github_auth_request_ids,
        'missing_token_counts': missing_token_counts,
        'budget_exhausted': budget_exhausted,
        'event_id': str(event.id),
        'devops_task_key': failure_task.item_key if failure_task else None,
    }


# PLS-99: @mention notifications. A comment containing "@username" instantly DMs
# that agent/person via Connect. The enqueue is hooked at the Comment model layer
# (post_save signal) so it fires for comments created via the UI AND the MCP/API.
# Delivery delegates to tasks.services.mentions, the single implementation shared
# with the pre-existing PLS-100 mention path, to avoid duplicate DMs and preserve
# response-status handling.
@shared_task(bind=True, max_retries=2)
def notify_comment_mentions(self, comment_id):
    """PLS-99: async wrapper for the single comment-mention implementation."""
    from .models import Comment
    from .services.mentions import notify_comment_mentions as notify_mentions

    try:
        comment = Comment.objects.select_related('item').get(id=comment_id)
    except Comment.DoesNotExist:
        return f"Comment {comment_id} not found"

    notify_mentions(
        comment.item,
        comment,
        author_id=comment.author_id,
        author_name=comment.author_name or comment.author_email or 'someone',
    )
    return f"Processed mention notifications for comment {comment_id}"


# PLAT-948: Weekly retrospective prompt ─────────────────────────────────────
#
# Fires every Friday at 11:30 UTC (= 17:00 IST) via Celery beat.
# Posts a structured retro prompt to the Leadership Retro Connect group
# conversation so the team can reflect async; Shizuha compiles a summary
# by Monday.  The group conversation ID is hard-wired here — it is a
# stable leadership group, not a per-sprint channel.
#
# Connect internal endpoint: POST /internal/group-message/
#   conversation_id  — target group conversation UUID
#   content          — message body
#   sender_name      — display name shown in the group
#   sender_service   — allowlisted service identifier (shizuha-pulse)
#
# The X-Internal-Service header satisfies Connect's allowlist check.
# No origin-token is required for group messages (no recipient_user_id to
# sign over), so system_message_headers is not used here.

_WEEKLY_RETRO_CONVERSATION_ID = '1257fe3e-cb14-485e-9ec1-a60ef9e7d738'


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def post_weekly_retro_prompt(self):
    """PLAT-948: post a weekly retrospective prompt to the Leadership Retro
    Connect group every Friday at 11:30 UTC (17:00 IST).

    The message is posted via the Connect internal group-message API so it
    arrives in the Leadership Retro conversation rather than as a DM.  The
    sender identity is the shizuha system agent (AI_ID / AI_EMAIL from
    settings, defaulting to id=2 / ai@shizuha.com).

    Retries up to 2× with a 30-second back-off on transient failures (e.g.
    Connect pod restart); does not raise into beat on permanent errors.
    """
    import requests as req

    now = timezone.now()
    # Format as "28 Jun 2026" (day month year, no leading zero on day)
    date_str = now.strftime('%-d %b %Y')

    content = (
        f'\U0001f4cb Weekly Retro — {date_str}\n\n'
        'Three questions for the week:\n'
        '1. What went well?\n'
        '2. What slowed us down or went wrong?\n'
        '3. What should we change next week?\n\n'
        'Learning-loop mapping (PLAT-1180/PLAT-1192) — every finding maps to a row:\n'
        '`finding | mechanism class | linked artifact | owner`\n'
        '- mechanism class ∈ {skill edit, workflow validator/post-function, '
        'event-hook, ADR, alert/probe, N/A — <reason>}\n'
        '- "sweep" is NOT a first-class mechanism class (event-driven-over-sweeps): '
        'allowed only with written justification that no event hook is possible.\n'
        '- Each finding row is filed as a Pulse task labeled `retro-finding`. '
        'Every non-N/A row must materialize as a Pulse task labeled '
        '`prevention-mechanism`, linked (relates_to/blocks) from the finding task. '
        'An N/A row records a comment `mechanism: N/A — <reason>` on the finding '
        'task instead.\n\n'
        'Reply in this thread. Shizuha will compile the summary, the mapping rows, '
        'and file the finding/mechanism tasks by Monday.'
    )

    connect_base_url = getattr(
        settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api'
    ).rstrip('/')
    url = f'{connect_base_url}/internal/group-message/'

    payload = {
        'conversation_id': _WEEKLY_RETRO_CONVERSATION_ID,
        'content': content,
        'sender_name': 'Shizuha',
        'sender_service': 'shizuha-pulse',
    }
    headers = {'X-Internal-Service': 'shizuha-pulse'}

    try:
        resp = req.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info(
            'PLAT-948: weekly retro prompt posted to conversation %s (HTTP %s)',
            _WEEKLY_RETRO_CONVERSATION_ID,
            resp.status_code,
        )
        return {
            'posted': True,
            'conversation_id': _WEEKLY_RETRO_CONVERSATION_ID,
            'date': date_str,
        }
    except req.exceptions.Timeout:
        logger.warning('PLAT-948: post_weekly_retro_prompt timed out — will retry')
        raise self.retry(exc=Exception('Connect timeout'))
    except req.exceptions.RequestException as exc:
        logger.error('PLAT-948: post_weekly_retro_prompt failed: %s', exc)
        raise self.retry(exc=exc)


# PLAT-1192: Monthly org-health digest — learning-loop conversion metric ─────
#
# Fires on the 1st of each month at 05:00 UTC and posts the previous calendar
# month's findings→mechanism conversion block (PLAT-1180 deliverable c) to the
# Leadership Retro Connect group. Metric computation lives in
# services/learning_loop.py so the Company-State Snapshot API (HLD PLAT-1190
# §5.8 `learning_loop`) can reuse the same numbers.


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def post_monthly_org_health_digest(self):
    """PLAT-1192: post the monthly org-health digest (learning-loop conversion
    metric for the just-ended month) to the Leadership Retro Connect group.

    Computed purely from Pulse links + labels (the PLAT-1180 contract): retro
    finding tasks (`retro-finding`) + incident closes vs distinct linked
    `prevention-mechanism` tasks, explicit `mechanism: N/A — <reason>` waivers,
    and a staleness line for mechanism tasks open >30d.
    """
    import requests as req

    from .services.learning_loop import (
        compute_learning_loop_metrics,
        format_learning_loop_block,
        previous_month_bounds,
    )

    start, end = previous_month_bounds()
    try:
        metrics = compute_learning_loop_metrics(start, end)
    except Exception as exc:
        logger.error('PLAT-1192: learning-loop metric computation failed: %s', exc)
        raise self.retry(exc=exc)

    month_label = start.strftime('%b %Y')
    content = (
        f'\U0001f3e5 Org-health digest — {month_label}\n\n'
        + format_learning_loop_block(metrics, month_label)
        + '\n\nContract: PLAT-1180 (label `prevention-mechanism` + '
          'relates_to/blocks links; N/A via `mechanism: N/A — <reason>`).'
    )

    connect_base_url = getattr(
        settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api'
    ).rstrip('/')
    url = f'{connect_base_url}/internal/group-message/'
    payload = {
        'conversation_id': _WEEKLY_RETRO_CONVERSATION_ID,
        'content': content,
        'sender_name': 'Shizuha',
        'sender_service': 'shizuha-pulse',
    }
    headers = {'X-Internal-Service': 'shizuha-pulse'}

    try:
        resp = req.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info(
            'PLAT-1192: monthly org-health digest posted for %s (HTTP %s): %s',
            month_label, resp.status_code,
            {k: metrics[k] for k in ('findings_total', 'mechanisms_linked',
                                     'na_count', 'conversion_rate')},
        )
        return {'posted': True, 'month': month_label, 'metrics': metrics}
    except req.exceptions.Timeout:
        logger.warning('PLAT-1192: org-health digest post timed out — will retry')
        raise self.retry(exc=Exception('Connect timeout'))
    except req.exceptions.RequestException as exc:
        logger.error('PLAT-1192: org-health digest post failed: %s', exc)
        raise self.retry(exc=exc)


# PLAT-967: Design-Review Thread Ritual (HLD PLAT-963) ──────────────────────
#
# Two posts to the Design Review Connect group (settings.DESIGN_REVIEW_CONVERSATION_ID):
#   - post_design_review_thread(item_id, conversation_id): per-HLD-ship, fired by
#     the `connect_group_post` transition post-function when a design/HLD/ADR task
#     enters Architecture Review (§2.3 template).
#   - post_weekly_design_review_digest(): Monday roll-up of design tasks still in
#     review so none languish unseen.
# Both reuse the Connect internal group-message endpoint (same as PLAT-948) and are
# non-blocking & advisory — they never gate the formal Architecture Review.

_DESIGN_REVIEW_WIKI_RE = re.compile(r'https?://[^\s)\]]*wiki[^\s)\]]*', re.IGNORECASE)


def _post_to_design_review_group(conversation_id, content):
    """POST to the Connect internal group-message endpoint. Returns the response;
    raises on HTTP error / timeout so the caller's Celery retry can engage."""
    connect_base_url = getattr(
        settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api'
    ).rstrip('/')
    url = f'{connect_base_url}/internal/group-message/'
    payload = {
        'conversation_id': conversation_id,
        'content': content,
        'sender_name': 'Shizuha',
        'sender_service': 'shizuha-pulse',
    }
    headers = {'X-Internal-Service': 'shizuha-pulse'}
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp


def _design_review_wiki_link(item):
    """Best-effort wiki link for an HLD task: metadata wiki keys first, else the
    first wiki URL found in the description."""
    md = item.metadata or {}
    for k in ('wiki_url', 'wiki_link', 'wiki_page', 'hld_url'):
        v = md.get(k)
        if v:
            return str(v)
    m = _DESIGN_REVIEW_WIKI_RE.search(item.description or '')
    return m.group(0) if m else '(link in task)'


def _design_review_tier(item):
    """Human label for the task's design class, derived from its labels/tags."""
    labels = item.labels
    if isinstance(labels, dict):
        labels = list(labels.keys())
    elif isinstance(labels, str):
        labels = [l.strip() for l in labels.split(',') if l.strip()]
    labels = labels or []
    return ', '.join(str(l) for l in labels if l) or 'design'


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def post_design_review_thread(self, item_id, conversation_id):
    """PLAT-967: post the §2.3 Design-Review template to the Design Review group
    when a design/HLD/ADR task enters Architecture Review. Enqueued on_commit by
    the `connect_group_post` transition post-function (so the row is committed).

    PLS-852: the review window is computed ONCE per review version, not per emit.
    The post-function fires on every transition into Architecture Review, and a
    transition can be re-fired while a review cannot be assigned — each re-fire
    used to recompute ``now + 48h``, so the advertised deadline drifted forward
    on every notice and no notice was authoritative. The close time is now stored
    on the item's metadata at first emit and reused on re-emits (which are
    explicitly marked as reminders). The stored close is cleared when the item
    leaves in_review, so a genuinely new review entry gets a fresh window.
    """
    from datetime import timedelta
    from .models import Item

    if not conversation_id:
        logger.info('PLAT-967: no Design Review conversation configured — skip post')
        return {'posted': False, 'reason': 'no_conversation'}
    try:
        item = Item.objects.get(id=item_id)
    except Item.DoesNotExist:
        logger.warning('PLAT-967: item %s vanished before design-review post', item_id)
        return {'posted': False, 'reason': 'item_missing'}

    window_h = int(getattr(settings, 'DESIGN_REVIEW_WINDOW_HOURS', 48))
    meta = dict(item.metadata or {})
    stored_close = meta.get('design_review_window_close')
    is_reemit = bool(stored_close)
    if not is_reemit:
        closes = (timezone.now() + timedelta(hours=window_h)).strftime('%-d %b %H:%M UTC')
        meta['design_review_window_close'] = closes
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
    else:
        closes = stored_close
    author = item.assignee_email or item.created_by_email or 'unassigned'
    summary_lines = [l.strip() for l in (item.description or '').splitlines() if l.strip()]
    summary = summary_lines[0] if summary_lines else '(see task)'
    if len(summary) > 200:
        summary = summary[:197] + '…'
    content = (
        f'🧭 Design Review — {item.title}\n'
        f'Author: {author}   ·   Class/tier: {_design_review_tier(item)}\n'
        f'Read: {_design_review_wiki_link(item)}   ·   Task: {item.item_key}\n'
        f'Summary: {summary}\n'
        f'Review window: {window_h}h (closes {closes})\n'
        + ('Reminder — window unchanged from the first notice.\n' if is_reemit else '')
        + 'Blocking? No — advisory. Post questions / objections in-thread.'
    )
    try:
        resp = _post_to_design_review_group(conversation_id, content)
        logger.info('PLAT-967: design-review post for %s → conv %s (HTTP %s)%s',
                    item.item_key, conversation_id, resp.status_code,
                    ' [re-emit]' if is_reemit else '')
        return {'posted': True, 'item': item.item_key, 'conversation_id': conversation_id,
                'reemit': is_reemit}
    except requests.exceptions.Timeout:
        logger.warning('PLAT-967: design-review post timed out — will retry')
        raise self.retry(exc=Exception('Connect timeout'))
    except requests.exceptions.RequestException as exc:
        logger.error('PLAT-967: design-review post failed: %s', exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def post_weekly_design_review_digest(self):
    """PLAT-967: Monday roll-up of design/HLD/ADR tasks currently in review,
    posted to the Design Review group so open reviews don't languish unseen."""
    from .models import Item
    from .services.workflow_service import WorkflowService

    conversation_id = getattr(settings, 'DESIGN_REVIEW_CONVERSATION_ID', None)
    if not conversation_id:
        logger.info('PLAT-967: no Design Review conversation configured — skip digest')
        return {'posted': False, 'reason': 'no_conversation'}

    now = timezone.now()
    rows = []
    for it in Item.objects.filter(status='in_review').order_by('updated_at'):
        if not WorkflowService._is_design_review_item(it):
            continue
        age_days = max(0, (now - (it.updated_at or now)).days)
        author = it.assignee_email or it.created_by_email or 'unassigned'
        rows.append(f'• {it.item_key} — {it.title}  (author: {author}, in review {age_days}d)')

    date_str = now.strftime('%-d %b %Y')
    if rows:
        content = (
            f'🧭 Weekly Design-Review Digest — {date_str}\n\n'
            f'{len(rows)} design/HLD/ADR task(s) currently in Architecture Review:\n'
            + '\n'.join(rows) +
            '\n\nAdvisory — add async eyes / objections in-thread. The formal review gate is unchanged.'
        )
    else:
        content = (
            f'🧭 Weekly Design-Review Digest — {date_str}\n\n'
            'No design/HLD/ADR tasks are currently in Architecture Review. 🎉'
        )
    try:
        resp = _post_to_design_review_group(conversation_id, content)
        logger.info('PLAT-967: weekly design-review digest posted (%d items, HTTP %s)',
                    len(rows), resp.status_code)
        return {'posted': True, 'count': len(rows), 'conversation_id': conversation_id}
    except requests.exceptions.Timeout:
        logger.warning('PLAT-967: weekly design-review digest timed out — will retry')
        raise self.retry(exc=Exception('Connect timeout'))
    except requests.exceptions.RequestException as exc:
        logger.error('PLAT-967: weekly design-review digest failed: %s', exc)
        raise self.retry(exc=exc)


# ── Orphan review-lane sweep (operator directive 2026-09-15) ─────────────────
#
# Pipeline-created items (CI regression watchlists, qa-automation discovery,
# focused-QA demands) can be minted directly INTO the review lane with no
# assignee. The assignment machinery deliberately never auto-assigns
# review-lane statuses (review work is owned by whoever produced it), so such
# items are lifecycle orphans: they count in a team's header as active work,
# appear in nobody's heartbeat, and have no path to an owner — forever. The
# creation side now stamps hands-on statuses (qa_cadence / regression_watchlist
# / focused_qa all mint 'open'/'todo'); this sweep re-homes the legacy and any
# future orphans: unassigned in_review items older than the grace window are
# bounced to a hands-on status so the queue-drain can assign them normally.

_ORPHAN_REVIEW_LANE_LOCK_KEY = 'pulse:lock:sweep_orphan_review_lane'
_ORPHAN_REVIEW_LANE_LOCK_TTL = 280
_ORPHAN_REVIEW_LANE_GRACE_DAYS = 3
_ORPHAN_REVIEW_LANE_BATCH = 25
_ORPHAN_REVIEW_LANE_TARGETS = ('todo', 'open', 'in_progress')


@shared_task(name='tasks.tasks.sweep_orphan_review_lane', bind=True)
def sweep_orphan_review_lane(self):
    """Bounce unassigned review-lane orphans back to a hands-on status.

    An item in in_review/in_qa/awaiting_merge with no assignee and no recent
    movement cannot ever be picked up: the queue-drain skips review-pipeline
    statuses and the critical-unassigned override only covers urgent/high.
    After a grace window (default 3 days) this sweep transitions the item to
    the first hands-on status its workflow allows (todo/open/in_progress) and
    records why, so the normal drain owns it again. Bounded per run; single-
    flight via the shared Redis lock; fail-open on Redis errors.
    """
    import uuid
    from datetime import timedelta as _timedelta
    from django.utils import timezone as _tz
    from django.db import transaction as _db_transaction
    from .models import Item
    from .services.workflow_service import WorkflowService

    token = uuid.uuid4().hex
    lock_key = _ORPHAN_REVIEW_LANE_LOCK_KEY
    if not getattr(settings, 'TESTING', False):
        try:
            have_lock = bool(WorkflowService._redis_client().set(
                lock_key, token, nx=True, ex=_ORPHAN_REVIEW_LANE_LOCK_TTL))
        except Exception:
            logger.warning('sweep_orphan_review_lane: redis lock unavailable — fail-open')
            have_lock = True
        if not have_lock:
            return {'skipped': 'locked'}

    try:
        now = _tz.now()
        grace = now - _timedelta(days=_ORPHAN_REVIEW_LANE_GRACE_DAYS)
        terminal = set(WorkflowService.TERMINAL_STATUS_SLUGS)
        candidates = list(
            Item.objects.filter(
                mode='task',
                status__in=WorkflowService._REVIEW_PIPELINE_STATUSES,
                assignee_email__isnull=True,
                updated_at__lt=grace,
            )
            .exclude(assignee_id__isnull=False)
            .exclude(resolution__isnull=False)
            .exclude(is_archived=True)
            .exclude(status__in=terminal)
            .select_related('project')
            .order_by('updated_at')[:_ORPHAN_REVIEW_LANE_BATCH]
        )
        bounced, skipped = [], []
        for item in candidates:
            target = None
            try:
                available = WorkflowService.get_available_transitions(
                    from_status=item.status, project=item.project)
                tos = {t.get('to_status') for t in available if isinstance(t, dict)}
                target = next((s for s in _ORPHAN_REVIEW_LANE_TARGETS if s in tos), None)
            except Exception:
                target = None
            try:
                with _db_transaction.atomic():
                    fresh = Item.objects.select_for_update().filter(pk=item.pk).first()
                    if fresh is None or (fresh.assignee_email or '').strip():
                        skipped.append((str(fresh and fresh.item_key or item.pk), 'claimed'))
                        continue
                    if fresh.status not in WorkflowService._REVIEW_PIPELINE_STATUSES:
                        skipped.append((str(fresh.item_key or item.pk), 'status-moved'))
                        continue
                    from django.contrib.auth import get_user_model
                    _User = get_user_model()
                    operator_email = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip()
                    actor = ((_User.objects.filter(email__iexact=operator_email).first() if operator_email else None)
                             or _User.objects.filter(is_superuser=True).first()
                             or _User.objects.filter(is_staff=True).first()
                             or _User.objects.filter(username='orphan-sweep').first())
                    if actor is None:
                        actor = _User.objects.create_user(
                            username='orphan-sweep', email='orphan-sweep@pulse',
                            password=None)
                    note = (
                        'Orphan review-lane sweep (operator directive 2026-09-15): this item sat in '
                        f'\'{fresh.status}\' with no assignee past the {_ORPHAN_REVIEW_LANE_GRACE_DAYS}-day grace '
                        'window — review-lane items are never auto-assigned, so a pipeline-created item '
                        'minted directly into the lane has no path to an owner. Bouncing to a hands-on '
                        'status so the queue-drain can assign it to a team member.'
                    )
                    moved = False
                    if target:
                        try:
                            WorkflowService.execute_transition(fresh, target, user=actor)
                            moved = True
                        except Exception:
                            moved = False
                    if not moved:
                        # Workflow has no hands-on transition from this lane —
                        # direct status set; Item.save() heals status/workflow_status sync.
                        fresh.status = 'todo'
                        fresh.workflow_status = 'To Do'
                        fresh.save(update_fields=['status', 'workflow_status', 'updated_at'])
                    from .models import Comment as _Comment
                    _Comment.objects.create(
                        item=fresh, content=note,
                        author_id=actor.id if actor else None,
                        author_email=actor.email if actor else 'orphan-sweep@pulse',
                        author_name='Orphan Review-Lane Sweep',
                    )
                    fresh.refresh_from_db()
                    bounced.append({'key': fresh.item_key or str(fresh.pk), 'status': fresh.status})
            except Exception as exc:
                logger.warning('sweep_orphan_review_lane: %s failed: %s',
                               getattr(item, 'item_key', item.pk), exc)
        if bounced or skipped:
            logger.info('sweep_orphan_review_lane: bounced=%d skipped=%d', len(bounced), len(skipped))
        return {'bounced': bounced, 'skipped': skipped}
    finally:
        if not getattr(settings, 'TESTING', False):
            try:
                WorkflowService._redis_client().delete(lock_key)
            except Exception:
                pass


# ── Recurring status-truth sweep (operator directive 2026-09-16) ─────────────
#
# One truth, no discrepancies: a recurring row waiting for its next occurrence
# must be `scheduled` everywhere — DB status, workload projection, and card.
# The recurring cycle state machine already parks rows in `scheduled` between
# occurrences and re-materializes them at due time (fire_due_cycle → workflow
# initial status), and the workload projection rebuckets future-due open rows
# to 'scheduled' for the card. But rows that predate the state machine (or were
# reset by older paths) sit in `open`/`todo` with a future due date: the card
# says "scheduled", the raw status says "open", and the mismatch reads as a
# seat ignoring actionable work (nagi: 4 open recurring rows, card 0 open,
# hibernated as nonproductive — all three views were right except the raw
# status). This sweep aligns the raw status with the projection.

_RECURRING_TRUTH_LOCK_KEY = 'pulse:lock:sweep_recurring_status_truth'
_RECURRING_TRUTH_LOCK_TTL = 280
_RECURRING_TRUTH_BATCH = 100


@shared_task(name='tasks.tasks.sweep_recurring_status_truth', bind=True)
def sweep_recurring_status_truth(self):
    """Park future-due recurring rows in `scheduled` so DB == card == queue.

    Only rows the cycle state machine would park anyway are touched:
    is_recurring, status open/todo/pending, due_date strictly in the future.
    in_progress/in_review/blocked work is never touched (active cycle). When
    the due timer fires, fire_due_cycle re-materializes the row into its
    workflow's initial hands-on status — the parked state is the machine's
    own convention, so this sweep cannot strand a cycle.
    """
    import uuid
    from django.db import transaction as _db_transaction
    from .models import Item
    from .services.workflow_service import WorkflowService

    token = uuid.uuid4().hex
    lock_key = _RECURRING_TRUTH_LOCK_KEY
    if not getattr(settings, 'TESTING', False):
        try:
            have_lock = bool(WorkflowService._redis_client().set(
                lock_key, token, nx=True, ex=_RECURRING_TRUTH_LOCK_TTL))
        except Exception:
            logger.warning('sweep_recurring_status_truth: redis lock unavailable — fail-open')
            have_lock = True
        if not have_lock:
            return {'skipped': 'locked'}

    try:
        now = timezone.now()
        parked = []
        rows = list(
            Item.objects.filter(
                mode='task', is_recurring=True,
                status__in=('open', 'todo', 'pending'),
                due_date__gt=now,
            ).order_by('updated_at')[:_RECURRING_TRUTH_BATCH]
        )
        for item in rows:
            try:
                with _db_transaction.atomic():
                    fresh = Item.objects.select_for_update().filter(pk=item.pk).first()
                    if fresh is None:
                        continue
                    if fresh.status not in ('open', 'todo', 'pending'):
                        continue
                    if not (fresh.due_date and fresh.due_date > now):
                        continue
                    WorkflowService._ensure_scheduled_gap_state_for_workflow(fresh)
                    fresh.status = 'scheduled'
                    fresh._activity_actor = (0, 'scheduler@pulse.shizuha.com')
                    fresh._activity_status_action = 'recurring_truth_park'
                    fresh._activity_suppress_fields = ('due_date', 'metadata')
                    fresh.save(update_fields=['status', 'workflow_status', 'updated_at'])
                    parked.append(fresh.item_key or str(fresh.pk))
            except Exception as exc:
                logger.warning('sweep_recurring_status_truth: %s failed: %s',
                               getattr(item, 'item_key', item.pk), exc)
        if parked:
            logger.info('sweep_recurring_status_truth: parked=%d', len(parked))
        return {'parked': parked}
    finally:
        if not getattr(settings, 'TESTING', False):
            try:
                WorkflowService._redis_client().delete(lock_key)
            except Exception:
                pass
