"""Canonical ordering primitives shared by Pulse work queues.

Stored priority remains the business value. These helpers only derive an
execution rank so a runnable root blocker can inherit the impact of blocked
work that only it can release.
"""

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.expressions import RawSQL


_TERMINAL_DEPENDENCY_STATUSES = frozenset({
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'silenced', 'applied', 'change_rejected', 'rfc_approved',
    'rfc_implementation_linked', 'rfc_validation',
})


def active_blocked_descendant_exists():
    """Whether this row directly releases active nonterminal dependency work.

    The link event normally moves the child to ``blocked`` immediately. Checking
    the wider active/nonterminal condition closes the transaction window and
    also protects legacy links whose auto-block event was interrupted.
    """
    from tasks.models import ItemLink

    return Exists(
        ItemLink.objects.filter(
            Q(
                source_item_id=OuterRef('pk'),
                link_type='blocks',
                target_item__resolution__isnull=True,
                target_item__is_archived=False,
            )
            & ~Q(target_item__status__in=_TERMINAL_DEPENDENCY_STATUSES)
            | Q(
                target_item_id=OuterRef('pk'),
                link_type__in=('is_blocked_by', 'blocked_by'),
                source_item__resolution__isnull=True,
                source_item__is_archived=False,
            )
            & ~Q(source_item__status__in=_TERMINAL_DEPENDENCY_STATUSES)
        )
    )


def active_upstream_blocker_exists():
    """Whether this row still has an active nonterminal dependency blocker.

    Status is not a sufficient runnable signal: interrupted auto-block writes or
    legacy drift can leave a row in ``open``/``in_progress`` while its upstream
    dependency remains active. Such a row must never become an executable focus
    target and freeze unrelated owner progress.
    """
    from tasks.models import ItemLink

    return Exists(
        ItemLink.objects.filter(
            Q(
                source_item_id=OuterRef('pk'),
                link_type__in=('is_blocked_by', 'blocked_by'),
                target_item__resolution__isnull=True,
                target_item__is_archived=False,
            )
            & ~Q(target_item__status__in=_TERMINAL_DEPENDENCY_STATUSES)
            | Q(
                target_item_id=OuterRef('pk'),
                link_type='blocks',
                source_item__resolution__isnull=True,
                source_item__is_archived=False,
            )
            & ~Q(source_item__status__in=_TERMINAL_DEPENDENCY_STATUSES)
        )
    )


def effective_blocker_priority_rank():
    """Return queue rank inherited from active blocked descendants.

    ``UNION`` (rather than ``UNION ALL``) makes the recursive walk cycle-safe.
    Both canonical link orientations are supported.
    """
    return RawSQL(
        """
        WITH RECURSIVE dependency_tree(id, priority) AS (
            SELECT "tasks_task"."id", "tasks_task"."priority"
            UNION
            SELECT dependent."id", dependent."priority"
            FROM dependency_tree current
            JOIN "tasks_item_link" link
              ON (
                (link."link_type" = 'blocks'
                 AND link."source_item_id" = current.id)
                OR
                (link."link_type" IN ('is_blocked_by', 'blocked_by')
                 AND link."target_item_id" = current.id)
              )
            JOIN "tasks_task" dependent
              ON dependent."id" = CASE
                WHEN link."link_type" = 'blocks' THEN link."target_item_id"
                ELSE link."source_item_id"
              END
            WHERE dependent."status" = 'blocked'
              AND dependent."resolution_id" IS NULL
              AND NOT dependent."is_archived"
        )
        SELECT MIN(
            CASE priority
              WHEN 'urgent' THEN 0
              WHEN 'high' THEN 1
              WHEN 'normal' THEN 2
              WHEN 'medium' THEN 2
              WHEN 'low' THEN 3
              ELSE 4
            END
        )
        FROM dependency_tree
        """,
        [],
        output_field=IntegerField(),
    )


_QUEUE_STAGE_RANK = {
    'awaiting_verification': 0,
    'awaiting_deploy': 1,
    'awaiting_merge': 2,
    'applied': 3,
    'in_review': 3,
    'under_review': 3,
    'in_qa': 3,
    'in_progress': 4,
    'accepted': 4,
}


def queue_focus_order_key(
    *, effective_priority_rank, is_blocker_root, status, created_at, pk,
):
    """Return the canonical runnable-owner key used by the focus guard.

    Waiting and active-upstream rows are excluded before this key is compared.
    A candidate may preempt only when its complete key is strictly smaller;
    equal-impact roots therefore cannot synthesize a reciprocal focus cycle.
    """
    return (
        int(effective_priority_rank) if effective_priority_rank is not None else 4,
        0 if is_blocker_root else 1,
        queue_stage_rank_value(status),
        created_at,
        int(pk),
    )


def queue_stage_rank_value(status):
    """Return the production queue's closest-to-done rank for one status."""
    return _QUEUE_STAGE_RANK.get((status or '').strip().lower(), 5)


def queue_stage_rank_expression():
    """Return the ORM expression equivalent of :func:`queue_stage_rank_value`."""
    return Case(
        When(status='awaiting_verification', then=Value(0)),
        When(status='awaiting_deploy', then=Value(1)),
        When(status='awaiting_merge', then=Value(2)),
        When(status__in=['applied', 'in_review', 'under_review', 'in_qa'], then=Value(3)),
        When(status__in=['in_progress', 'accepted'], then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
