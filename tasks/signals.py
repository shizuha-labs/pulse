"""
Django signals for automatic activity tracking on Item changes.

Tracks field changes and creates ItemActivity records for the audit log.
"""

import logging
import threading
from django.db.models.signals import pre_save, post_save, post_delete, pre_delete
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)

# Fields whose activity-log old_value/new_value must be secret-redacted (PLS-39).
# These are the free-text fields that can contain arbitrary user-supplied content.
_ACTIVITY_REDACT_FIELDS = frozenset({'title', 'description'})

# Fields to track for activity log
TRACKED_FIELDS = {
    'title': 'Title',
    'description': 'Description',
    'status': 'Status',
    'priority': 'Priority',
    'severity': 'Severity',
    'assignee_id': 'Assignee',
    'assignee_email': 'Assignee',
    'due_date': 'Due Date',
    'reminder_time': 'Reminder',
    'start_date': 'Start Date',
    'estimated_hours': 'Estimated Hours',
    'labels': 'Labels',
    'tags': 'Tags',
    'project_id': 'Project',
}


def _operator_wait_proof_snapshot(item):
    """Fields whose mutation is a PLS-529 re-arm event.

    Assignee fields are intentionally absent: provider availability and normal
    failover ownership churn must never clear an operator wait.
    """
    metadata = item.metadata or {}
    return {
        'status': item.status,
        'resolution_id': item.resolution_id,
        'is_archived': item.is_archived,
        'assignment_group': item.assignment_group,
        'title': item.title,
        'description': item.description,
        'tags': item.tags,
        'priority': item.priority,
        'due_date': item.due_date,
        'gate': metadata.get('escalation_routing_gate'),
        'auto_blocker_for': metadata.get('auto_blocker_for'),
        'auto_blocker_parent_id': metadata.get('auto_blocker_parent_id'),
        'auto_blocker_team': metadata.get('auto_blocker_team'),
        'operator_dependency_kind': metadata.get('operator_dependency_kind'),
    }

# Store original values before save
_original_values = {}

# Thread-local storage to track items being deleted (to avoid FK violations in signals)
_deleting_items = threading.local()


def get_request_user():
    """
    Get the current request user from thread-local storage.
    Returns (user_id, user_email) or (None, None) if not available.
    """
    try:
        from threading import current_thread
        from .middleware import _request_local
        request = getattr(_request_local, 'request', None)
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            return request.user.id, getattr(request.user, 'email', '')
    except Exception:
        pass
    return None, None


def get_item_actor(instance):
    """
    Resolve the actor to credit for a change to `instance`.

    Precedence:
      1. Explicit override on the instance: `instance._activity_actor`
         (tuple of (user_id, user_email)) — callers running from a
         non-request context (scheduler, Celery) set this before save().
      2. Request threadlocal — the normal HTTP request path.
      3. item.created_by_* — best-effort fallback.

    Returns (user_id, user_email) or (None, None).
    """
    override = getattr(instance, '_activity_actor', None)
    if override:
        return override[0], override[1] or ''
    return get_request_user()


def is_item_being_deleted(item_id):
    """Check if an item is currently being deleted (cascade)."""
    deleting = getattr(_deleting_items, 'ids', set())
    return item_id in deleting


def mark_item_deleting(item_id):
    """Mark an item as being deleted."""
    if not hasattr(_deleting_items, 'ids'):
        _deleting_items.ids = set()
    _deleting_items.ids.add(item_id)


def unmark_item_deleting(item_id):
    """Remove the deleting mark from an item."""
    if hasattr(_deleting_items, 'ids'):
        _deleting_items.ids.discard(item_id)



@receiver(post_save, sender='tasks.Project')
def ensure_project_default_board(sender, instance, created, **kwargs):
    """Create the default Kanban board for newly-created software projects."""
    if not created:
        return
    from .default_boards import ensure_default_board_for_new_project
    ensure_default_board_for_new_project(instance)

@receiver(pre_delete, sender='tasks.Item')
def mark_item_as_deleting(sender, instance, **kwargs):
    """Mark item as being deleted to prevent FK violations in cascade delete signals."""
    mark_item_deleting(instance.pk)
    logger.debug(f"Marked Item {instance.pk} as being deleted")


@receiver(post_delete, sender='tasks.Item')
def unmark_item_as_deleting(sender, instance, **kwargs):
    """Unmark item after deletion is complete."""
    unmark_item_deleting(instance.pk)
    logger.debug(f"Unmarked Item {instance.pk} from deleting state")


@receiver(post_save, sender='tasks.Item')
def schedule_item_timer(sender, instance, created, **kwargs):
    """Schedule or cancel Redis timers based on item state.

    When due_date or reminder_time changes, clears the scheduler dedup
    keys so the notification fires again for the new time.
    """
    from django.conf import settings as _settings
    # PLAT-1245: the hermetic test suite has NO redis broker — settings.TESTING
    # points CELERY_BROKER_URL at the :9 discard sentinel. This post_save signal
    # fires on EVERY Item.save() (thousands of times across the 1000+ test suite);
    # each open→connect→fail on the dead broker, plus the scheduler's retry-backoff,
    # is what SIGKILLed the CI test Job at 23m and blocked every pulse deploy. Timer
    # scheduling is a production concern — skip it entirely under test. Scheduler
    # unit tests inject a fake redis via TimerManager(redis_client=...), so they are
    # unaffected by this early return.
    if getattr(_settings, 'TESTING', False):
        return
    try:
        from .scheduler import get_timer_manager, KEY_PROCESSED_PREFIX
        import redis as _redis
        from django.conf import settings as _settings

        original = _original_values.get(instance.pk)
        if original and not created:
            r = _redis.Redis.from_url(_settings.CELERY_BROKER_URL, decode_responses=True)

            # If due_date changed, clear the overdue dedup key so the
            # scheduler will dispatch a fresh notification for the new time.
            if original.get('due_date') != instance.due_date:
                r.delete(f"{KEY_PROCESSED_PREFIX}:due:{instance.pk}")
                logger.debug("Cleared overdue dedup key for Item %s (due_date changed)", instance.pk)

            # If reminder_time changed, clear the reminder dedup key.
            if original.get('reminder_time') != instance.reminder_time:
                r.delete(f"{KEY_PROCESSED_PREFIX}:reminder:{instance.pk}")
                logger.debug("Cleared reminder dedup key for Item %s (reminder_time changed)", instance.pk)

        get_timer_manager().schedule_item(instance)
    except Exception:
        logger.exception("Failed to schedule timer for Item %s (non-fatal)", instance.pk)


@receiver(post_save, sender='tasks.Item')
def reconcile_evidence_blocker_timer(sender, instance, **kwargs):
    """PLS-463: derive an evidence deadline from every committed mutation."""
    try:
        from django.db import transaction
        from .services.evidence_blocker_escalation import (
            has_evidence_marker,
            reconcile_evidence_blocker,
        )

        if not has_evidence_marker(instance):
            return

        item_id = instance.pk
        transaction.on_commit(lambda: reconcile_evidence_blocker(item_id))
    except Exception:
        logger.exception(
            "Failed to register evidence-blocker reconciliation for Item %s",
            instance.pk,
        )


@receiver(post_save, sender='tasks.ItemLink')
def reconcile_evidence_blocker_link(sender, instance, **kwargs):
    """PLS-463: a new/changed block edge may make a blocker eligible."""
    try:
        from .services.evidence_blocker_escalation import reconcile_link
        reconcile_link(instance)
    except Exception:
        logger.exception(
            "Failed to register evidence-blocker link reconciliation for %s",
            instance.pk,
        )


@receiver(post_delete, sender='tasks.ItemLink')
def clear_evidence_blocker_link(sender, instance, **kwargs):
    """PLS-463: removing the last active parent clears its deadline."""
    try:
        from .services.evidence_blocker_escalation import reconcile_link
        reconcile_link(instance)
    except Exception:
        logger.exception(
            "Failed to register deleted evidence-blocker link reconciliation for %s",
            instance.pk,
        )



@receiver(post_save, sender='tasks.Item')
def reset_epic_closeout_guard_on_child_create(sender, instance, created, **kwargs):
    """PLS-200: new child scope makes a previous close-out request stale."""
    if not created or not getattr(instance, 'parent_item_id', None):
        return
    try:
        from .services.workflow_service import WorkflowService
        WorkflowService.reset_epic_closeout_guard_for_child(instance)
    except Exception:
        logger.exception(
            "Failed to reset PLS-200 close-out guard for Item %s",
            getattr(instance, 'pk', None),
        )


@receiver(post_delete, sender='tasks.Item')
def cancel_item_timer(sender, instance, **kwargs):
    """Cancel all Redis timers when an item is deleted."""
    try:
        from .scheduler import get_timer_manager
        get_timer_manager().cancel_all(str(instance.pk))
    except Exception:
        logger.exception("Failed to cancel timers for Item %s (non-fatal)", instance.pk)


@receiver(pre_save, sender='tasks.Item')
def capture_item_original_values(sender, instance, **kwargs):
    """Capture original field values before saving.

    Also resets notification flags when the relevant date changes so a
    fresh notification fires for the new time.
    """
    if instance.pk:
        try:
            from .models import Item
            import copy as _copy
            original = Item.objects.get(pk=instance.pk)
            instance._operator_wait_proof_before = _operator_wait_proof_snapshot(original)
            snapshot = {
                field: getattr(original, field)
                for field in TRACKED_FIELDS.keys()
            }
            # Also snapshot metadata.pull_requests so the post_save signal
            # can diff PR attach/remove/status events. Deep-copy to avoid
            # the caller mutating the same list in-place between pre/post.
            orig_md = original.metadata or {}
            snapshot['__pull_requests'] = _copy.deepcopy(
                orig_md.get('pull_requests') or []
            )
            _original_values[instance.pk] = snapshot
            # track_item_changes consumes the shared snapshot with pop() before
            # later post_save receivers run. Keep the provider-status edge on
            # the instance so ADR-0064's release outbox is emitted only for a
            # real status transition, never for a full save/priority change on
            # an already-terminal provider.
            instance._external_wait_previous_status = original.status

            # Reset overdue_notified when due_date changes
            if original.due_date != instance.due_date:
                instance.overdue_notified = False

            # Reset reminder_sent when reminder_time changes
            if original.reminder_time != instance.reminder_time and instance.reminder_time:
                instance.reminder_sent = False
        except Item.DoesNotExist:
            pass


@receiver(pre_save, sender='tasks.Item')
def enforce_blocker_priority_invariant(sender, instance, **kwargs):
    """Blocker invariant backstop (operator directive 2026-08-08).

    A task that blocks active work MUST carry ``urgent`` priority so the
    rebalancer assigns it before any lower-priority work. If any write path
    (service, import, internal mutation) persists a priority below urgent while
    the item still has active blocked descendants, coerce back to urgent here.
    The API layer already rejects the downgrade with a clear message; this
    raises the floor for every other save path.

    Skipped when the item is already urgent or the priority is unchanged, so a
    routine save does not re-run the dependency exists query.
    """
    if str(getattr(instance, 'priority', '') or '').strip().lower() == 'urgent':
        return
    if not getattr(instance, 'pk', None):
        return
    # Only enforce when priority is actually changing (avoid a query per save).
    orig = _original_values.get(instance.pk)
    if orig is not None and orig.get('priority') == getattr(instance, 'priority', ''):
        return
    try:
        from .services.workflow_service import WorkflowService
        if WorkflowService._has_active_blocked_descendants(instance):
            instance.priority = 'urgent'
    except Exception:
        logger.exception(
            "Blocker-priority invariant check failed for Item %s (non-fatal)",
            getattr(instance, 'pk', None),
        )


@receiver(post_save, sender='tasks.Item')
def invalidate_operator_wait_on_item_mutation(sender, instance, created, **kwargs):
    if created:
        return
    before = getattr(instance, '_operator_wait_proof_before', None)
    if before is None or before == _operator_wait_proof_snapshot(instance):
        return
    try:
        from .services.operator_wait import invalidate_markers_for_item
        invalidate_markers_for_item(instance.pk, 'blocker_or_evidence_mutation')
    except Exception as exc:
        logger.exception(
            'PLS-529 operator-wait item invalidation failed for %s',
            instance.pk,
        )
        from .services.operator_wait import report_operator_wait_failure
        report_operator_wait_failure('item_mutation', exc, target=instance.pk)


@receiver(post_save, sender='tasks.Item')
def track_item_changes(sender, instance, created, **kwargs):
    """Create activity records for item changes.

    Attribution precedence: `instance._activity_actor` (explicit, for
    scheduler / Celery callers) > request threadlocal > created_by_*.

    Semantic action override: callers can set
    `instance._activity_status_action` to a specific action name (e.g.
    'recurring_cycle_fired_manual', 'recurring_parked_scheduled') so the
    history shows meaningful labels instead of generic 'status_changed'.
    """
    from .models import ItemActivity

    user_id, user_email = get_item_actor(instance)

    # Fallback to created_by ONLY when we have neither id nor email.
    # Use `is None` (not truthiness) — SCHEDULER_USER_ID is 0, which the
    # old `if not user_id` check wrongly treated as "missing" and fell
    # back to created_by, re-crediting changes to the task's creator
    # (e.g. admin@example.com) for every scheduler fire.
    if user_id is None and not user_email and instance.created_by_id:
        user_id = instance.created_by_id
        user_email = instance.created_by_email or ''

    if user_id is None and not user_email:
        logger.debug(f"Skipping activity tracking for Item {instance.pk}: no actor context")
        return

    status_action_override = getattr(instance, '_activity_status_action', None)
    suppress_fields = set(getattr(instance, '_activity_suppress_fields', ()))

    if created:
        ItemActivity.objects.create(
            item=instance,
            action='created',
            user_id=user_id or 0,
            user_email=user_email or '',
        )
        logger.debug(f"Created 'created' activity for Item {instance.pk}")
    else:
        original = _original_values.pop(instance.pk, None)
        if not original:
            return

        for field, label in TRACKED_FIELDS.items():
            if field in suppress_fields:
                continue
            old_val = original.get(field)
            new_val = getattr(instance, field)

            if old_val == new_val:
                continue

            # Determine action type
            if field == 'status':
                if status_action_override:
                    action = status_action_override
                elif new_val == 'completed':
                    action = 'completed'
                elif old_val == 'completed':
                    action = 'reopened'
                else:
                    action = 'status_changed'
            elif field in ('assignee_id', 'assignee_email'):
                if field == 'assignee_email':
                    # Skip duplicate tracking (we track assignee_id)
                    continue
                action = 'assigned' if new_val else 'unassigned'
            elif field == 'priority':
                action = 'priority_changed'
            elif field == 'project_id':
                action = 'moved'
            else:
                action = 'updated'

            old_display = _format_value(field, old_val)
            new_display = _format_value(field, new_val)

            # PLS-39: redact secrets from free-text fields before persisting
            # to activity history.  The serializer already redacts incoming
            # writes, but the pre-save snapshot captured from the DB may still
            # carry a plaintext secret from before this fix landed.  Redact
            # here so the activity row never stores the raw secret even when
            # the old_value came from a pre-redaction DB row.
            if field in _ACTIVITY_REDACT_FIELDS:
                from .secret_scanner import redact_secrets as _redact
                old_display, _ = _redact(old_display)
                new_display, _ = _redact(new_display)

            # Belt-and-suspenders: when old/new raw values differ by type
            # (int 24 vs str "24"), the earlier `old_val == new_val` guard
            # lets them through but the rendered display is identical.
            # Don't log a visibly-no-op row in that case.
            if old_display == new_display:
                continue

            ItemActivity.objects.create(
                item=instance,
                action=action,
                field_name=label,
                old_value=old_display,
                new_value=new_display,
                user_id=user_id or 0,
                user_email=user_email or '',
            )
            logger.debug(f"Created '{action}' activity for Item {instance.pk}: {label}")

            # PLAT-4609: typed escalation owns delivery through its durable
            # disposition outbox.  Do not also enqueue the generic status/
            # assignment notification from the same write (that would expose
            # intermediate routing and duplicate owner attention).
            if not getattr(instance, '_suppress_standard_notifications', False):
                _dispatch_field_notification(
                    instance, field, old_val, new_val, action,
                    user_id, user_email,
                )

            # HIVE-608: emit a Home `task.assignment` event on an assignee change.
            # This runs in post_save (after the row is committed), so an
            # assign(save=False)+later-save caller can't ghost-publish before the
            # source row exists. Best-effort, transaction-after-commit.
            if field == 'assignee_id':
                try:
                    from .services import home_activity
                    _asg_actor = (
                        {'kind': 'agent' if '@agents.' in (user_email or '').lower() else 'user',
                         'username': (user_email or '').split('@', 1)[0]}
                        if user_email else None
                    )
                    home_activity.emit_assignment(
                        instance, user=_asg_actor,
                        assignee_email=getattr(instance, 'assignee_email', '') or '',
                    )
                except Exception:
                    logger.exception("HIVE-608 home-activity assignment emit failed")

        # Diff linked pull requests — produces pr_attached / pr_removed /
        # pr_status_changed activities so history shows who linked a PR.
        _track_pull_request_changes(
            instance, original, user_id, user_email,
        )

        # PLAT-1353: if a linked deliverable task just reached terminal and
        # its recurring parent already has explicit cycle-completion evidence,
        # close/park the parent's current cycle so rebalancers do not churn it.
        if original.get('status') != instance.status:
            from django.db import transaction
            try:
                # PLS-373: isolate the recurring-cycle auto-close in its own savepoint
                # so a DB error in that path rolls back only the savepoint and can never
                # abort the outer transition transaction (-> TransactionManagementError
                # 500, the KOT-40/41 symptom). The alert path below is likewise
                # savepoint-isolated inside alert_recurring_cycle_auto_close_failure.
                with transaction.atomic():
                    from .services.recurring_cycle_close import close_recurring_parents_for_terminal_child
                    close_recurring_parents_for_terminal_child(
                        instance,
                        actor_email=user_email or 'system@pulse',
                    )
            except Exception as exc:
                try:
                    from .services.recurring_cycle_close import alert_recurring_cycle_auto_close_failure
                    alert_recurring_cycle_auto_close_failure(
                        instance,
                        context='terminal_child_status_signal',
                        exc=exc,
                    )
                except Exception:
                    logger.exception("PLAT-1353 recurring-cycle fail-loud path failed for terminal child signal")
                logger.warning(
                    "PLAT-1353 recurring-cycle parent auto-close failed for child %s",
                    getattr(instance, 'item_key', instance.pk),
                    exc_info=True,
                )


@receiver(post_save, sender='tasks.Comment')
def close_recurring_cycle_on_completion_marker(sender, instance, created, **kwargs):
    """PLS-774: event-driven recurring cycle close on completion marker comment.

    When a comment containing explicit cycle-completion evidence is posted on a
    recurring item, dispatch a transaction-on-commit ``complete_cycle()`` attempt
    (HLD entry point #2) to park the item in ``scheduled`` so rebalance sweeps do
    not reassign it before the next due boundary.

    The completion is deferred with ``transaction.on_commit`` so it runs outside
    the comment's write transaction and never synchronously pre-empts an explicit
    callers' own completion (e.g. a merged-PR webhook calling
    ``close_recurring_cycle_if_complete``) — the CAS on cycle revision/generation
    makes both paths idempotent and replay-safe.  A mechanism failure is surfaced
    loud on the item via ``alert_recurring_cycle_auto_close_failure`` (HLD
    invariant #4), never silently swallowed by a later sweep.
    """
    if not created:
        return
    content = (instance.content or '').strip()
    if not content:
        return

    from .services.recurring_cycle_close import (
        _AUDIT_MARKER,
        _affirmative_completion_matches,
    )
    # Skip auto-close audit markers to avoid infinite loops
    if _AUDIT_MARKER in content:
        return
    # PLS-1384: only explicit AFFIRMATIVE completion evidence may drive the
    # automatic state write — negated ("PARTIAL, not cycle complete"), quoted,
    # and example-context mentions are rejected here so the comment never
    # dispatches a complete_cycle() attempt.
    if not _affirmative_completion_matches(content):
        return

    item = instance.item
    if not getattr(item, 'is_recurring', False) or not getattr(item, 'frequency', None):
        return
    if (item.status or '').strip().lower() in {'cancelled', 'canceled', 'deferred', 'expired', 'rejected', 'duplicate', 'wont_fix', 'failed', 'scheduled'}:
        return

    from django.db import transaction
    from .services.recurring_cycle_close import (
        _item_label,
        alert_recurring_cycle_auto_close_failure,
    )

    item_pk = item.pk
    comment_pk = instance.pk
    actor_id = instance.author_id or 0
    actor_email = instance.author_email or 'system@pulse'
    trigger_key = f'comment:{comment_pk}'

    def _attempt_completion():
        from .models import Item as ItemModel
        from .services.recurring_cycle_state import complete_cycle, cycle_snapshot
        try:
            # Re-fetch fresh state: the comment's transaction has committed and a
            # concurrent terminal/PR path may already have advanced the cycle.
            current = ItemModel.objects.select_related('project').filter(pk=item_pk).first()
            if current is None:
                logger.warning(
                    "PLS-774: recurring item %s vanished before on-commit close (comment %s)",
                    item_pk, comment_pk,
                )
                return
            expected = cycle_snapshot(current)
            result = complete_cycle(
                current.pk,
                actor_id=actor_id,
                actor_email=actor_email,
                trigger_key=trigger_key,
                expected_cycle_revision=expected['cycle_revision'],
                expected_generation=expected['generation'],
                expected_due_at=expected['due_boundary'],
                expected_completion_watermark=expected['completion_watermark'] or '',
            )
            if result.applied:
                logger.info(
                    "PLS-774: Closed recurring cycle for %s via comment %s (next due: %s)",
                    _item_label(current), comment_pk, result.next_due,
                )
        except Exception as exc:
            try:
                current = ItemModel.objects.filter(pk=item_pk).first()
                alert_recurring_cycle_auto_close_failure(
                    current,
                    context='completion_marker_comment',
                    exc=exc,
                )
            except Exception:
                logger.exception(
                    "PLS-774: fail-loud path failed for completion marker %s (comment %s)",
                    item_pk, comment_pk,
                )
            logger.warning(
                "PLS-774: Failed to close recurring cycle for item %s via comment %s: %s",
                item_pk, comment_pk, exc,
                exc_info=True,
            )

    transaction.on_commit(_attempt_completion)


@receiver(post_save, sender='tasks.Item')
def record_external_dependency_release(sender, instance, created, **kwargs):
    """Provider status changes append the ADR-0064 release outbox in-transaction."""
    if created:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and 'status' not in set(update_fields):
        return
    previous_status = getattr(
        instance, '_external_wait_previous_status', instance.status
    )
    if previous_status == instance.status:
        return
    from .services.workflow_service import WorkflowService
    if not WorkflowService._status_releases_blocks(instance.status):
        return
    from .models import ExternalDependencyBinding, ExternalDependencyReleaseEvent
    if not ExternalDependencyBinding.objects.filter(
        provider_item=instance,
        state=ExternalDependencyBinding.STATE_ACTIVE,
    ).exists():
        return
    from .services.external_wait import record_provider_release
    record_provider_release(
        instance,
        kind=ExternalDependencyReleaseEvent.KIND_TERMINAL,
    )


@receiver(post_save, sender='tasks.Item')
def sync_external_wait_local_owner(sender, instance, created, **kwargs):
    """A failover re-home while parked becomes the owner restored on release."""
    if created or instance.status != 'waiting_external':
        return
    update_fields = kwargs.get('update_fields')
    owner_fields = {'assignee_id', 'assignee_email', 'assignment_group'}
    if update_fields is not None and not (set(update_fields) & owner_fields):
        return
    from .models import ExternalWaitAuthority
    authority = ExternalWaitAuthority.objects.filter(
        item=instance,
        wait_generation=instance.external_wait_generation,
        state=ExternalWaitAuthority.STATE_WAITING,
    ).first()
    if authority is None:
        return
    authority.pre_assignee_id = instance.assignee_id
    authority.pre_assignee_email = instance.assignee_email or ''
    authority.pre_assignment_group = instance.assignment_group or ''
    authority.save(update_fields=[
        'pre_assignee_id', 'pre_assignee_email', 'pre_assignment_group', 'updated_at',
    ])


@receiver(post_save, sender='tasks.TeamRoster')
def reconcile_group_owned_waits_on_roster_change(sender, instance, created, **kwargs):
    """A staffed exact-org roster immediately claims its parked escalations."""
    # Legacy rows can still be saved while their organization identity is
    # being canonicalized. They are not exact-org routing authorities yet and
    # must not enqueue a cross-org reconciliation with a guessed identifier.
    if not instance.is_active or not instance.organization_id:
        return
    members = sender._flat_member_emails(
        instance.tiers or [],
        instance.junior_emails or [],
    )
    if not members:
        return
    update_fields = kwargs.get('update_fields')
    if (
        not created
        and update_fields is not None
        and not ({'tiers', 'junior_emails', 'is_active'} & set(update_fields))
    ):
        return

    from django.db import transaction
    from .tasks import reconcile_group_owned_external_waits

    organization_id = int(instance.organization_id)
    team_name = instance.team_name
    transaction.on_commit(lambda: reconcile_group_owned_external_waits.apply_async(
        kwargs={
            'organization_id': organization_id,
            'team_name': team_name,
        },
    ))


@receiver(post_save, sender='tasks.Item')
def reconcile_urgent_risk_containment(sender, instance, created, **kwargs):
    """PLS-620: event-driven owner + containment reconciliation.

    Run synchronously on the Item write transaction so a rollback leaves no
    decision, audit event, owner change, or loud-hold comment behind.
    """

    if getattr(instance, '_suppress_urgent_risk_reconcile', False):
        return
    from .services.urgent_risk_containment import requires_containment

    if created and not requires_containment(instance):
        return
    update_fields = kwargs.get('update_fields')
    relevant = {
        'priority', 'labels', 'tags', 'assignee_id', 'assignee_email',
        'assignment_group',
    }
    if not created and update_fields is not None and not (set(update_fields) & relevant):
        return

    # PLS-467 owns the Item mutation until the typed outbox occurrence has
    # been appended.  PLS-620 may need a second, derived owner mutation, but
    # doing that from this nested post_save would violate the one-generation
    # writer boundary.  Defer only until Item.save finalizes the current
    # occurrence; the reconciliation still runs inside the same outer atomic
    # transaction, so rollback remains all-or-nothing.
    from .services.item_state_mutation import item_state_mutation_active
    if item_state_mutation_active(instance.pk):
        instance._deferred_urgent_risk_reconcile_trigger = 'item_write'
        return

    from .services.urgent_risk_containment import reconcile_item

    result = reconcile_item(
        instance.pk,
        trigger='create' if created else 'item_write',
    )
    # perform_create continues with the same in-memory object after post_save.
    # Reflect any owner chosen by the nested reconciliation so it cannot append
    # a stale "capacity queued" comment or emit a stale final assignment.
    if result.get('owner_changed'):
        instance.assignee_id = result.get('assignee_id')
        instance.assignee_email = result.get('assignee_email')
        instance.assignment_group = result.get('assignment_group')


def _track_pull_request_changes(instance, original, user_id, user_email):
    """
    Diff the PR list between pre-save and post-save snapshots of metadata
    and emit activity rows. Keyed on the PR URL (normalised by trailing
    slash). Compares `status` too — a webhook-driven open→merged flip
    produces a `pr_status_changed` row (though webhooks use .update() and
    bypass signals, so those also log directly in GitHubWebhookView).
    """
    from .models import ItemActivity

    old_list = original.get('__pull_requests') or []
    md = instance.metadata or {}
    new_list = (md.get('pull_requests') or []) if isinstance(md, dict) else []

    def _norm_url(u):
        return (u or '').rstrip('/')

    old_by_url = {_norm_url(p.get('url')): p for p in old_list if isinstance(p, dict) and p.get('url')}
    new_by_url = {_norm_url(p.get('url')): p for p in new_list if isinstance(p, dict) and p.get('url')}

    # Attachments: in new, not in old
    for url, pr in new_by_url.items():
        if url in old_by_url:
            continue
        ItemActivity.objects.create(
            item=instance,
            action='pr_attached',
            field_name='Pull Request',
            new_value=_format_pr(pr),
            user_id=user_id or 0,
            user_email=user_email or '',
        )
        logger.debug("Created 'pr_attached' activity for Item %s: %s", instance.pk, url)
        # HIVE-608: emit a Home live-activity `task.pr_link` event for the
        # canonical PR-attach path (pulse_link_pr / UI update metadata.pull_requests[]),
        # not just ItemLink issue-links. Best-effort, transaction-after-commit.
        try:
            from .services import home_activity
            _pr_actor = (
                {'kind': 'agent' if '@agents.' in (user_email or '').lower() else 'user',
                 'username': (user_email or '').split('@', 1)[0]}
                if user_email else None
            )
            home_activity.emit_pr_link(
                instance, user=_pr_actor, link_type='pr',
                target_key=str(pr.get('number') or ''), url=url,
            )
        except Exception:
            logger.exception("HIVE-608 home-activity pr_attached emit failed")

    # Removals: in old, not in new
    for url, pr in old_by_url.items():
        if url in new_by_url:
            continue
        ItemActivity.objects.create(
            item=instance,
            action='pr_removed',
            field_name='Pull Request',
            old_value=_format_pr(pr),
            user_id=user_id or 0,
            user_email=user_email or '',
        )
        logger.debug("Created 'pr_removed' activity for Item %s: %s", instance.pk, url)

    # Status changes: same url, different status
    for url, new_pr in new_by_url.items():
        old_pr = old_by_url.get(url)
        if not old_pr:
            continue
        old_status = (old_pr.get('status') or '').lower()
        new_status = (new_pr.get('status') or '').lower()
        if old_status == new_status:
            continue
        ItemActivity.objects.create(
            item=instance,
            action='pr_status_changed',
            field_name='Pull Request',
            old_value=f"{_format_pr(new_pr)} — {old_status or 'unknown'}",
            new_value=f"{_format_pr(new_pr)} — {new_status or 'unknown'}",
            user_id=user_id or 0,
            user_email=user_email or '',
        )
        logger.debug(
            "Created 'pr_status_changed' activity for Item %s: %s %s→%s",
            instance.pk, url, old_status, new_status,
        )


def _format_pr(pr):
    """Render a PR dict for the activity log: '#12 org/repo' or the URL."""
    if not isinstance(pr, dict):
        return str(pr)
    number = pr.get('number')
    repo = pr.get('repo')
    url = pr.get('url') or ''
    if number and repo:
        return f"#{number} {repo}"
    if number:
        return f"#{number}"
    return url or '(unknown PR)'


def _dispatch_field_notification(instance, field, old_val, new_val, action, user_id, user_email):
    """Dispatch a Celery notification task for notable field changes.

    ``post_save`` still runs inside the caller's transaction. Enqueuing here
    immediately lets a fast worker read the pre-commit row and reject a current
    status/assignee event as stale. It can also publish an event for a write
    that later rolls back. Register every dispatch on the commit boundary;
    outside an atomic block Django executes the callback immediately.
    """
    ctx = {
        'changed_by': user_email or str(user_id),
        'exclude_user_id': user_id,
        # Fence delayed delivery to the complete tuple that produced this
        # event.  Status + assignee alone are insufficient for repeated review
        # rounds: a task can return to the same status/owner with a newer return
        # generation before the old Celery message runs (PLS-474).
        'dispatched_assignee_id': str(getattr(instance, 'assignee_id', '') or ''),
        'dispatched_assignee_email': str(
            getattr(instance, 'assignee_email', '') or ''
        ).strip().lower(),
        'dispatched_assignment_group': str(
            getattr(instance, 'assignment_group', '') or ''
        ).strip().lower(),
    }
    metadata = getattr(instance, 'metadata', None)
    if isinstance(metadata, dict):
        # The armed context is only one durable generation shape. Fail-closed
        # returns deliberately remove invalid context while retaining the
        # standalone watermark and a typed hold, so reading context alone lets
        # their delayed notifications escape the generation fence (PLS-568).
        from .services.workflow_service import WorkflowService

        generation = WorkflowService._implementation_return_generation(instance)
        if generation:
            ctx['dispatched_return_generation'] = generation

    try:
        # PLAT-432: import INSIDE the try. This runs in a post_save signal, so any
        # failure resolving send_item_notification (e.g. a tasks-package refactor
        # that doesn't re-export it) would otherwise propagate out of Model.save()
        # and 500 EVERY item update — labels, status, assignee, priority. Notifications
        # are best-effort; a dispatch/import failure must never break the save.
        from .tasks import send_item_notification

        def enqueue(event_type, context):
            from django.db import transaction

            transaction.on_commit(
                lambda task=send_item_notification,
                       item_id=str(instance.pk),
                       event=event_type,
                       payload=dict(context): task.delay(item_id, event, payload),
                # A broker failure must never turn a committed item write into
                # an HTTP 500 after the transaction has succeeded.
                robust=True,
            )

        if field == 'status':
            if action == 'completed':
                enqueue('completed', ctx)
            else:
                ctx['old_status'] = str(old_val or '')
                ctx['new_status'] = str(new_val or '')
                enqueue('status_changed', ctx)
        elif field == 'assignee_id' and new_val:
            # PLAT-1357: record the assignee this event was dispatched for so the
            # delivery task can suppress the notification if the item was reassigned
            # to someone else before the Celery task ran (stale reassignment replay).
            ctx['dispatched_status'] = str(getattr(instance, 'status', '') or '')
            enqueue('assignment_changed', ctx)
        elif field == 'priority':
            ctx['old_priority'] = str(old_val or '')
            ctx['new_priority'] = str(new_val or '')
            enqueue('priority_changed', ctx)
    except Exception:
        logger.exception("Failed to dispatch notification for Item %s field %s (non-fatal)", instance.pk, field)


def _format_value(field, value):
    """Format a field value for display in activity log."""
    if value is None:
        return ''
    if field in ('due_date', 'start_date'):
        if hasattr(value, 'strftime'):
            return value.strftime('%Y-%m-%d %H:%M')
    if field == 'labels' and isinstance(value, dict):
        return ', '.join(f"{k}={v}" for k, v in value.items())
    if field == 'project_id':
        # Look up project name for better display
        try:
            from .models import Project
            project = Project.objects.get(pk=value)
            return f"{project.key} - {project.name}"
        except Project.DoesNotExist:
            return str(value)
    if field in ('assignee_id', 'reporter_id', 'created_by_id'):
        # Resolve user_id → email/name so the history reads
        # "Shizuha changed Assignee from 'rin@shizuha.com' to 'kai@shizuha.com'"
        # instead of "'24' -> '13'". Short in-process cache keyed on the ID
        # avoids N HTTP hits when a bulk update touches many items.
        return _resolve_user_display(value)
    return str(value)


# Per-process cache for user_id → display label. TTL not strictly needed —
# this process restarts on deploy, and emails rarely change under active
# user IDs. Keyed on the integer ID; stores the resolved label or the
# stringified ID as fallback.
_user_display_cache: dict = {}


def _resolve_user_display(value):
    """Return a human-readable label for a user_id. Falls back to str(value)."""
    try:
        key = int(value)
    except (TypeError, ValueError):
        return str(value)
    cached = _user_display_cache.get(key)
    if cached is not None:
        return cached
    label = str(key)
    try:
        from .user_service import user_service
        info = user_service.resolve_user_info(user_id=key)
        if info:
            name = (info.get('name') or info.get('username') or '').strip()
            email = (info.get('email') or '').strip()
            if name and email:
                label = f"{name} <{email}>"
            elif email:
                label = email
            elif name:
                label = name
    except Exception as exc:
        logger.debug("Failed to resolve user_id=%s for activity log: %s", key, exc)
    _user_display_cache[key] = label
    return label


@receiver(post_save, sender='tasks.Comment')
def track_comment_activity(sender, instance, created, **kwargs):
    """Create activity when a comment is added."""
    if not created:
        return  # Only track new comments

    from .models import ItemActivity

    # PLS-39: the comment was already redacted by CommentCreateSerializer before
    # save, but redact the snippet here too as defence-in-depth.
    from .secret_scanner import redact_secrets as _redact
    snippet = instance.content[:100] + ('...' if len(instance.content) > 100 else '')
    snippet, _ = _redact(snippet)
    # PLS-722: a focus-refused body is preserved as a Comment row, but it is
    # NOT work — the agent was stopped, not productive. Recording it as
    # `commented` would manufacture the appearance of progress the focus guard
    # exists to prevent: the PLS-509 WIP-recovery scan and the agent-flatline
    # detector both whitelist `commented` as real activity, so a refusal would
    # protect the very item it was refused on and suppress a stall alert.
    # A distinct action excludes it from both by construction.
    from .models import Comment as _Comment
    action = (
        'comment_refused'
        if instance.origin_kind == _Comment.ORIGIN_FOCUS_REFUSED
        else 'commented'
    )
    ItemActivity.objects.create(
        item=instance.item,
        action=action,
        new_value=snippet,
        user_id=instance.author_id,
        user_email=instance.author_email,
    )
    logger.debug(f"Created '{action}' activity for Item {instance.item_id}")
    # PLS-49: comment notifications are dispatched synchronously by CommentViewSet.perform_create;
    # firing send_item_notification here as well caused duplicate entries (same comment notified
    # twice with slightly different author display strings). Signal handles only the activity row.

    # PLS-99: @mention notifications. Hooked here (model layer) — NOT in the
    # viewset — so a comment created via the UI OR the MCP/API both notify any
    # @mentioned agent/person via Connect. This is a distinct DM from the PLS-49
    # watcher/assignee notification (mention vs subscription), so it does not
    # duplicate it. Async + fail-soft: never blocks comment creation. The cheap
    # '@' guard avoids enqueuing a task for the (common) no-mention comment.
    if created and '@' in (instance.content or ''):
        try:
            from .tasks import notify_comment_mentions
            notify_comment_mentions.delay(instance.id)
        except Exception as e:
            logger.warning(f"PLS-99: failed to enqueue mention notifications for comment {instance.id}: {e}")

    # PLAT-1277: event-driven ops-plane anomaly detection. This is best-effort
    # and must never block the comment write path.
    try:
        from .services.ops_anomaly_monitor import observe_comment_created
        observe_comment_created(instance)
    except Exception:
        logger.exception("Pulse ops anomaly comment detector failed for Item %s (non-fatal)", instance.item_id)
        try:
            from .services.ops_anomaly_monitor import record_detector_exception
            record_detector_exception('comment', target=str(instance.item_id))
        except Exception:
            logger.exception("Pulse ops anomaly comment detector fail-loud path failed")

    try:
        from .services.operator_wait import handle_digest_comment_saved
        handle_digest_comment_saved(instance)
    except Exception as exc:
        logger.exception(
            'PLS-529 operator-wait comment hook failed for %s',
            instance.pk,
        )
        from .services.operator_wait import report_operator_wait_failure
        report_operator_wait_failure('comment_create', exc, target=instance.pk)


@receiver(post_save, sender='tasks.Comment')
def reconcile_operator_wait_on_comment_edit(sender, instance, created, **kwargs):
    if created:
        return
    try:
        from .services.operator_wait import handle_digest_comment_saved
        handle_digest_comment_saved(instance)
    except Exception as exc:
        logger.exception(
            'PLS-529 operator-wait comment-edit hook failed for %s',
            instance.pk,
        )
        from .services.operator_wait import report_operator_wait_failure
        report_operator_wait_failure('comment_edit', exc, target=instance.pk)


@receiver(post_delete, sender='tasks.Comment')
def invalidate_operator_wait_on_comment_delete(sender, instance, **kwargs):
    try:
        from .services.operator_wait import handle_comment_deleted
        handle_comment_deleted(instance)
    except Exception as exc:
        logger.exception(
            'PLS-529 operator-wait comment-delete hook failed for %s',
            instance.pk,
        )
        from .services.operator_wait import report_operator_wait_failure
        report_operator_wait_failure('comment_delete', exc, target=instance.pk)


@receiver(post_save, sender='tasks.Attachment')
def track_attachment_added(sender, instance, created, **kwargs):
    """Create activity when an attachment is added."""
    if not created:
        return

    from .models import ItemActivity

    ItemActivity.objects.create(
        item=instance.item,
        action='attachment_added',
        new_value=instance.filename,
        user_id=instance.uploaded_by_id,
        user_email=instance.uploaded_by_email,
    )
    logger.debug(f"Created 'attachment_added' activity for Item {instance.item_id}")


@receiver(post_delete, sender='tasks.Attachment')
def track_attachment_removed(sender, instance, **kwargs):
    """Create activity when an attachment is removed."""
    from .models import ItemActivity

    # Skip if item is being deleted (cascade delete scenario)
    if is_item_being_deleted(instance.item_id):
        logger.debug(f"Skipping attachment_removed activity: Item {instance.item_id} is being deleted")
        return

    user_id, user_email = get_request_user()
    if not user_id:
        user_id = instance.uploaded_by_id
        user_email = instance.uploaded_by_email

    ItemActivity.objects.create(
        item_id=instance.item_id,
        action='attachment_removed',
        old_value=instance.filename,
        user_id=user_id,
        user_email=user_email,
    )
    logger.debug(f"Created 'attachment_removed' activity for Item {instance.item_id}")


@receiver(post_save, sender='tasks.ItemLink')
def track_link_created(sender, instance, created, **kwargs):
    """Create activity when a link is created."""
    if not created:
        return

    from .models import ItemActivity

    target_key = instance.target_item.item_key or f"#{instance.target_item_id}"

    ItemActivity.objects.create(
        item=instance.source_item,
        action='linked',
        field_name=instance.get_link_type_display(),
        new_value=target_key,
        user_id=instance.created_by_id,
        user_email=instance.created_by_email,
    )
    logger.debug(f"Created 'linked' activity for Item {instance.source_item_id}")

    try:
        from .services.workflow_service import WorkflowService
        WorkflowService.reset_epic_closeout_guard_for_link(instance)
    except Exception:
        logger.exception(
            "Failed to reset PLS-200 close-out guard for ItemLink %s",
            getattr(instance, 'id', None),
        )

    if instance.link_type in ('blocks', 'is_blocked_by'):
        try:
            from .services.workflow_service import WorkflowService
            # A dependency link makes its runnable root active WIP immediately.
            # Do this before blocking the dependent so a previously parked
            # backlog root can never disappear from every agent queue.
            WorkflowService.ensure_blocker_runnable_from_link(instance)
            # Operator directive 2026-08-08: a task that blocks active work MUST
            # carry the highest priority (urgent) so the rebalancer assigns it
            # before any lower-priority work — otherwise the dependent stays
            # blocked even when seats are free. Escalate at the same boundary.
            try:
                WorkflowService._escalate_blocker_priority_from_link(instance)
            except Exception:
                logger.exception(
                    "Failed to escalate blocker priority for ItemLink %s",
                    getattr(instance, 'id', None),
                )
            if not getattr(instance, '_skip_auto_block', False):
                WorkflowService.auto_block_from_link(instance)
        except Exception:
            logger.exception(
                "Failed to enforce blocker WIP invariant for ItemLink %s",
                getattr(instance, 'id', None),
            )
        try:
            from .services.operator_wait import invalidate_markers_for_item
            invalidate_markers_for_item(instance.source_item_id, 'blocker_link_mutation')
            invalidate_markers_for_item(instance.target_item_id, 'blocker_link_mutation')
        except Exception as exc:
            logger.exception(
                'PLS-529 operator-wait link invalidation failed for %s',
                getattr(instance, 'id', None),
            )
            from .services.operator_wait import report_operator_wait_failure
            report_operator_wait_failure(
                'link_mutation', exc, target=getattr(instance, 'id', None),
            )


@receiver(post_delete, sender='tasks.ItemLink')
def track_link_removed(sender, instance, **kwargs):
    """Create activity when a link is removed."""
    from .models import ItemActivity, Item

    # Skip if source item is being deleted (cascade delete scenario)
    if is_item_being_deleted(instance.source_item_id):
        logger.debug(f"Skipping unlinked activity: Item {instance.source_item_id} is being deleted")
        return

    user_id, user_email = get_request_user()
    if not user_id:
        user_id = instance.created_by_id
        user_email = instance.created_by_email

    # Target item might also be deleted, so use stored ID
    try:
        target_key = instance.target_item.item_key or f"#{instance.target_item_id}"
    except Item.DoesNotExist:
        target_key = f"#{instance.target_item_id}"

    ItemActivity.objects.create(
        item_id=instance.source_item_id,
        action='unlinked',
        field_name=instance.get_link_type_display(),
        old_value=target_key,
        user_id=user_id,
        user_email=user_email,
    )
    if instance.link_type in ('blocks', 'is_blocked_by'):
        try:
            from .services.operator_wait import invalidate_markers_for_item
            invalidate_markers_for_item(instance.source_item_id, 'blocker_link_mutation')
            invalidate_markers_for_item(instance.target_item_id, 'blocker_link_mutation')
        except Exception as exc:
            logger.exception(
                'PLS-529 operator-wait unlink invalidation failed for %s',
                getattr(instance, 'id', None),
            )
            from .services.operator_wait import report_operator_wait_failure
            report_operator_wait_failure(
                'unlink_mutation', exc, target=getattr(instance, 'id', None),
            )
    logger.debug(f"Created 'unlinked' activity for Item {instance.source_item_id}")


@receiver(post_save, sender='tasks.ItemWatcher')
def track_watch_started(sender, instance, created, **kwargs):
    """Create activity when someone starts watching."""
    if not created:
        return

    from .models import ItemActivity

    ItemActivity.objects.create(
        item=instance.item,
        action='watched',
        user_id=instance.user_id,
        user_email=instance.user_email,
    )
    logger.debug(f"Created 'watched' activity for Item {instance.item_id}")


@receiver(post_delete, sender='tasks.ItemWatcher')
def track_watch_stopped(sender, instance, **kwargs):
    """Create activity when someone stops watching."""
    from .models import ItemActivity

    # Skip if item is being deleted (cascade delete scenario)
    if is_item_being_deleted(instance.item_id):
        logger.debug(f"Skipping unwatched activity: Item {instance.item_id} is being deleted")
        return

    ItemActivity.objects.create(
        item_id=instance.item_id,
        action='unwatched',
        user_id=instance.user_id,
        user_email=instance.user_email,
    )
    logger.debug(f"Created 'unwatched' activity for Item {instance.item_id}")


# =============================================================================
# PLS-171 — TaskEvent delta-log emission (live frontend updates, Slice 1)
# These dedicated receivers append to the append-only TaskEvent log so the
# frontend can patch its cache from deltas (GET /api/events?since=). They are
# independent of the activity/notification receivers (no shared early-return) and
# best-effort (record_* swallow errors) — a delta-log append never breaks a save.
# =============================================================================
def _item_event_payload(instance):
    """Compact display-field snapshot the frontend patches its cached row with."""
    return {
        'status': instance.status,
        'priority': instance.priority,
        'workflow_status': instance.workflow_status,
        'assignee_email': instance.assignee_email,
        'title': instance.title,
        'is_archived': getattr(instance, 'is_archived', False),
    }


@receiver(post_save, sender='tasks.Item')
def emit_item_task_event(sender, instance, created, **kwargs):
    """PLS-171: append item_created / item_updated so live lists/detail patch."""
    from .models import TaskEvent
    from .services.task_events import record_item_event
    actor_id, _ = get_item_actor(instance)
    record_item_event(
        instance,
        TaskEvent.KIND_ITEM_CREATED if created else TaskEvent.KIND_ITEM_UPDATED,
        actor_user_id=actor_id,
        payload=_item_event_payload(instance),
    )

    # PLS-254: fail-loud queue-depth anomaly monitor. Best-effort and
    # non-blocking; catches floods in any workflow/status/identity.
    try:
        from .services.ops_anomaly_monitor import observe_item_saved
        observe_item_saved(instance, created=created)
    except Exception:
        logger.exception(
            "Pulse ops anomaly item-depth detector failed for Item %s (non-fatal)",
            instance.id,
        )
        try:
            from .services.ops_anomaly_monitor import record_detector_exception
            record_detector_exception('item_depth', target=str(instance.id))
        except Exception:
            logger.exception("Pulse ops anomaly item-depth fail-loud path failed")


@receiver(post_delete, sender='tasks.Item')
def emit_item_deleted_task_event(sender, instance, **kwargs):
    """PLS-171: append item_deleted so live lists drop the row without a refresh."""
    from .models import TaskEvent
    from .services.task_events import record_item_event
    actor_id, _ = get_item_actor(instance)
    record_item_event(instance, TaskEvent.KIND_ITEM_DELETED, actor_user_id=actor_id, payload={})


@receiver(post_save, sender='tasks.Comment')
def emit_comment_task_event(sender, instance, created, **kwargs):
    """PLS-171: append comment_added so an open task page shows new comments."""
    if not created:
        return
    from .services.task_events import record_comment_event
    record_comment_event(instance)


@receiver(post_save, sender='tasks.ItemLink')
def emit_link_created_task_event(sender, instance, created, **kwargs):
    """PLS-171: append link_changed when a link is created."""
    if not created:
        return
    from .services.task_events import record_link_event
    record_link_event(instance)


@receiver(post_delete, sender='tasks.ItemLink')
def emit_link_removed_task_event(sender, instance, **kwargs):
    """PLS-171: append link_changed when a link is removed.
    PLS-498: attribute the removal to the request actor when the caller stamped
    ``instance._actor_user_id`` (e.g. delete_link); otherwise anonymous, as for
    cascade/bulk deletes with no request context."""
    from .services.task_events import record_link_event
    record_link_event(
        instance, actor_user_id=getattr(instance, '_actor_user_id', None))


@receiver(post_save, sender='tasks.ItemActivity')
def detect_ops_activity_anomaly(sender, instance, created, **kwargs):
    """PLAT-1277: event-driven activity-row anomaly detection (no sweeps)."""
    if not created:
        return
    try:
        from .services.ops_anomaly_monitor import observe_item_activity_created
        observe_item_activity_created(instance)
    except Exception:
        logger.exception(
            "Pulse ops anomaly activity detector failed for Item %s (non-fatal)",
            instance.item_id,
        )
        try:
            from .services.ops_anomaly_monitor import record_detector_exception
            record_detector_exception('activity', target=str(instance.item_id))
        except Exception:
            logger.exception("Pulse ops anomaly activity detector fail-loud path failed")


@receiver(post_save, sender='tasks.Notification')
def detect_ops_notification_anomaly(sender, instance, created, **kwargs):
    """PLAT-1277: event-driven repeated-notification anomaly detection."""
    if not created:
        return
    try:
        from .services.ops_anomaly_monitor import observe_notification_created
        observe_notification_created(instance)
    except Exception:
        logger.exception(
            "Pulse ops anomaly notification detector failed for user %s (non-fatal)",
            instance.user_id,
        )
        try:
            from .services.ops_anomaly_monitor import record_detector_exception
            record_detector_exception('notification', target=str(instance.user_id))
        except Exception:
            logger.exception("Pulse ops anomaly notification detector fail-loud path failed")


# ── HIVE-608: Home live-activity producer hooks (HLD 1f271d2c) ────────────────
# Event-driven from the existing write paths (no polling sweeps). Each handler is
# best-effort: a producer error must never break the originating write. Org
# provenance + display-safe capping are enforced inside tasks.services.home_activity.

@receiver(post_save, sender='tasks.Comment')
def home_activity_on_comment(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from .services import home_activity
        home_activity.emit_comment(instance)
    except Exception:
        logger.exception("HIVE-608 home-activity comment emit failed")


@receiver(post_save, sender='tasks.ItemLink')
def home_activity_on_item_link(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from .services import home_activity
        source = getattr(instance, 'source_item', None)
        target = getattr(instance, 'target_item', None)
        home_activity.emit_pr_link(
            source,
            user={'kind': 'user', 'username': (getattr(instance, 'created_by_email', '') or '').split('@', 1)[0]}
            if getattr(instance, 'created_by_email', '') else None,
            link_type=getattr(instance, 'link_type', '') or '',
            target_key=getattr(target, 'item_key', '') or '',
        )
    except Exception:
        logger.exception("HIVE-608 home-activity item-link emit failed")


# =============================================================================
# PLS-1310 — tombstone the item-state outbox stream on item hard-delete
# =============================================================================
# The outbox is the only audit surface a hard delete currently leaves (the
# hard-delete path emits no item_deleted activity row — the PLS-1301 NULL-actor
# gap). With the FK on CASCADE the stream used to vanish with the item; via the
# raw/SQL delete path it instead survived as orphaned *pending* mutations for a
# nonexistent item (datum: item 36111). Both shapes are wrong:
#
#   * CASCADE removal destroys the forensic trail.
#   * Surviving *pending* rows are live mutations for an item that cannot
#     exist — undefined behavior for any future consumer that drains pending.
#
# This receiver runs on EVERY Item deletion (direct instance delete AND
# collector cascade — Django emits pre_delete per collected instance), flipping
# the item's outbox rows to disposition='orphaned' BEFORE the item row goes.
# The FK (SET_NULL since the PLS-1310 migration) then detaches the rows, so
# they survive as inert forensic record: never relayed (consumers drain
# 'pending' only), never drained, never counted as live stream state.
# Orphans created by the raw/SQL path (which bypasses the ORM and this signal)
# are caught by the tombstone_orphaned_outbox management command and the
# deploy-time backfill in the PLS-1310 migration.
def _tombstone_item_outbox(item_pk):
    """Flip one item's outbox rows to disposition='orphaned' (PLS-1310).

    Returns the number of rows tombstoned. Idempotent: already-orphaned rows
    are left untouched so a re-entrant delete path cannot restale them.
    """
    from .models import ItemStateEventOutbox

    return ItemStateEventOutbox.objects.filter(item_id=item_pk).exclude(
        disposition='orphaned'
    ).update(disposition='orphaned', stale_reason='item-hard-deleted')


@receiver(pre_delete, sender='tasks.Item', dispatch_uid='pls1310_outbox_tombstone_on_item_delete')
def tombstone_outbox_on_item_delete(sender, instance, **kwargs):
    try:
        _tombstone_item_outbox(instance.pk)
    except Exception:
        # Never block the delete on tombstone failure — but fail LOUD: a
        # silently-skipped tombstone re-opens the orphaned-pending hole.
        logger.exception(
            "PLS-1310 outbox tombstone failed for item %s — outbox rows may "
            "survive as non-orphaned orphans", instance.pk)
