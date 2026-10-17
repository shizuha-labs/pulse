"""Canonical active-queue predicate shared by API queues and sweeps (PLS-445)."""

from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from tasks.models import EscalationRoutingDisposition, ItemLink, Status, Workflow, WorkflowStatus
from tasks.services.queue_priority import (
    active_blocked_descendant_exists,
    active_upstream_blocker_exists,
    effective_blocker_priority_rank,
    queue_stage_rank_expression,
)

UNRESOLVED_OPERATOR_DESTINATION = 'org_local_operator_destination_unresolved'


TERMINAL_STATUS_SLUGS = frozenset({
    'completed', 'cancelled', 'canceled', 'closed', 'done',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'expired', 'deferred',
    # PLS-1362: done-category workflow statuses that were omitted here are
    # falsely flagged non-terminal by slug-keyed consumers (dead-end sweeps,
    # archive drains, re-route guards) even though their Status.category is
    # 'done'. `applied` is the change-request final success state (PLAT-940 —
    # must not trigger failover re-routing); `risk_accepted` is the security
    # workflow's terminal acceptance disposition. Key terminality on the done
    # CATEGORY wherever possible; these entries keep slug-only membership
    # checks honest for the same states.
    'applied', 'risk_accepted',
})

WAITING_QUEUE_STATUS_SLUGS = frozenset({'blocked', 'scheduled'})

# These workflow states are intentionally absent from the default active queue
# only while their named implementation artifacts are still active. Explicit
# status-filtered queries remain an escape hatch and do not call this predicate.
NON_ACTIONABLE_STATUS_SLUGS = frozenset({'rfc_implementation_linked'})

IMPLEMENTATION_LINK_TYPES = frozenset({
    'implements',
    'implemented_by',
    'implementation',
    'implementation_of',
    'has_implementation',
})


def waiting_owner_queue_status_slugs():
    """Return every catalog status that is awareness-only for owner queues.

    Status slugs are workflow-extensible, so literal checks for ``scheduled``
    or ``backlog`` are incomplete (for example ``snoozed`` is a scheduled
    status). Keep the explicit non-catalog compatibility slugs while deriving
    the rest from the authoritative status category.
    """
    slugs = set(WAITING_QUEUE_STATUS_SLUGS)
    slugs.add('waiting_external')
    slugs.update(
        Status.objects.filter(category__in=('backlog', 'scheduled'))
        .values_list('slug', flat=True)
    )
    return frozenset(slugs)


def unresolved_operator_destination_hold_exists():
    """True when the latest typed escalation hold could not seat a human owner.

    Sato/BKSK-6: Raise to Admin Ops produced
    ``org_local_operator_destination_unresolved`` (PLS-278: no exact-org
    admin-ops roster) and left the carrier ``in_progress``. That is not
    ready work. Only the latest generation counts so a later successful
    route can make the item runnable again.
    """
    newer = EscalationRoutingDisposition.objects.filter(
        item_id=OuterRef('item_id'),
        generation__gt=OuterRef('generation'),
    )
    return Exists(
        EscalationRoutingDisposition.objects.filter(
            item_id=OuterRef('pk'),
            outcome='held',
            reason_code=UNRESOLVED_OPERATOR_DESTINATION,
        ).exclude(Exists(newer))
    )


def future_recurring_owner_waiting_q():
    """Future recurring initial states wait for their exact due boundary.

    Workflow metadata defines the unstarted state; the legacy ``open`` slug
    remains compatible when no workflow catalog entry exists. An initial
    status categorized as in-progress is still active work, not a timer gap.
    """
    initial_todo = Workflow.objects.filter(
        Q(slug__iexact=OuterRef('workflow_name'))
        | Q(name__iexact=OuterRef('workflow_name')),
        initial_status__slug=OuterRef('status'),
        initial_status__category='todo',
    )
    return (
        Q(is_recurring=True, due_date__gt=timezone.now())
        & (Q(status='open') | Q(Exists(initial_todo)))
    )


def owner_queue_waiting_q():
    """Canonical waiting predicate shared by queue order and focus guards."""
    return (
        Q(status__in=waiting_owner_queue_status_slugs())
        | Q(_queue_owner_awareness=True)
        | Q(_queue_has_active_upstream=True)
        | unresolved_operator_destination_hold_exists()
        | future_recurring_owner_waiting_q()
        # A nominally-open task with a linked implementation PR is waiting on
        # review/merge/reconciliation, not fresh author work. Stale task status
        # must not let it focus-pin an unrelated runnable item.
        | Q(
            status='open',
            metadata__pull_requests__contains=[{'status': 'open'}],
        )
    )


def filter_active_queue_items(queryset, *, include_item_id=None):
    """Return rows that belong in the default active assignee queue.

    This is the single definition used by both ``pulse_get_my_tasks`` and
    stale/offline assignment monitoring. It intentionally excludes terminal,
    scheduled, backlog, and Implementation-Linked RFCs that still have active
    named implementation artifacts (PLS-189/PLS-445).
    """
    parked_slugs = set(
        Status.objects.filter(category__in=('backlog', 'scheduled'))
        .values_list('slug', flat=True)
    )
    inactive_slugs = set(
        Status.objects.filter(category__in=['done', 'scheduled', 'backlog'])
        .values_list('slug', flat=True)
    )
    inactive_slugs.update(TERMINAL_STATUS_SLUGS)
    # Assignment exclusion and terminality are different invariants.
    # waiting_external is deliberately non-terminal and todo-category, so it
    # needs this explicit queue exclusion rather than inheriting a broad status
    # set whose other semantics do not belong to the heartbeat predicate.
    inactive_slugs.add('waiting_external')
    # Backlog/scheduled work is normally invisible to an agent heartbeat. A
    # legacy/corrupt blocker root is the one fail-safe exception: keep it visible
    # until the link-event activation path repairs its status, otherwise the
    # graph can deadlock merely because its release task was parked earlier.
    inactive_filter = Q(status__in=inactive_slugs - parked_slugs)
    if include_item_id is not None:
        inactive_filter &= ~Q(pk=include_item_id)
    queryset = (
        queryset
        .exclude(inactive_filter)
        .annotate(
            _queue_has_active_blocked_descendant=active_blocked_descendant_exists(),
        )
        .exclude(
            Q(status__in=parked_slugs)
            & Q(_queue_has_active_blocked_descendant=False)
        )
    )

    # PLS-529: a surfaced, typed operator-only blocker root remains a durable
    # dependency but is not agent-runnable demand.  Reuse the exact same marker
    # predicate as capacity/failover/root-focus callers instead of inventing a
    # queue-only special case.
    from tasks.services.operator_wait import exclude_operator_wait_items
    queryset = exclude_operator_wait_items(queryset)

    if not NON_ACTIONABLE_STATUS_SLUGS:
        return queryset

    # Link-type only. A previous `description__icontains=F(item_key)` OR branch
    # (legacy free-text "implements PLAT-123") forced Postgres into nested-loop
    # ILIKE scans + multi-second JIT compiles on every is_active list/count —
    # EXPLAIN showed ~14s JIT alone for /api/items/?is_active=true. Typed
    # implementation links are the durable model (PLS-189/PLS-445).
    active_outward_links = (
        ItemLink.objects
        .filter(source_item=OuterRef('pk'))
        .exclude(target_item__status__in=inactive_slugs)
        .filter(link_type__in=IMPLEMENTATION_LINK_TYPES)
    )
    active_inward_links = (
        ItemLink.objects
        .filter(target_item=OuterRef('pk'))
        .exclude(source_item__status__in=inactive_slugs)
        .filter(link_type__in=IMPLEMENTATION_LINK_TYPES)
    )
    return (
        queryset
        .annotate(
            _has_active_outward_link=Exists(active_outward_links),
            _has_active_inward_link=Exists(active_inward_links),
        )
        .exclude(
            Q(status__in=NON_ACTIONABLE_STATUS_SLUGS)
            & (
                Q(_has_active_outward_link=True)
                | Q(_has_active_inward_link=True)
            )
        )
    )


def owner_awareness_exists():
    return Exists(
        WorkflowStatus.objects.filter(
            Q(workflow__slug__iexact=OuterRef('workflow_name'))
            | Q(workflow__name__iexact=OuterRef('workflow_name')),
            status__slug=OuterRef('status'),
            owner_action_policy='awareness',
        )
    )


def annotate_owner_queue_projection(queryset):
    """Attach the canonical runnable-owner facts used by queue and guards.

    The caller supplies the authorization-scoped base queryset.  In
    particular, owner identity is only a ranking key; it must never widen this
    queryset across organizations or projects (PLS-826).
    """
    queryset = queryset.annotate(
        _queue_owner_awareness=owner_awareness_exists(),
        _queue_is_blocker_root=active_blocked_descendant_exists(),
        _queue_has_active_upstream=active_upstream_blocker_exists(),
        _queue_effective_priority_rank=effective_blocker_priority_rank(),
        _queue_stage_rank=queue_stage_rank_expression(),
    )
    return queryset.annotate(
        _queue_is_waiting=Case(
            When(owner_queue_waiting_q(), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
    )


def filter_runnable_owner_queue_items(
    queryset,
    *,
    include_item_id=None,
    excluded_statuses=(),
):
    """Return the runnable band from an already-authorized owner projection.

    Waiting rows remain available to the list endpoint as awareness, but they
    cannot participate in focus selection.  Active upstream links are checked
    independently of stored status so an interrupted auto-block write cannot
    make a stale ``open``/``in_progress`` row runnable.
    """
    ranked = annotate_owner_queue_projection(
        filter_active_queue_items(
            queryset,
            include_item_id=include_item_id,
        )
    )
    if include_item_id is None:
        ranked = ranked.filter(_queue_is_waiting=False)
    else:
        # External-wait release evaluates the exact carrier at its recorded
        # target status. Keep only that row as the explicit waiting exception;
        # every competing candidate still comes from the runnable band.
        ranked = ranked.filter(
            Q(_queue_is_waiting=False)
            | Q(pk=include_item_id, _queue_has_active_upstream=False)
        )
    if excluded_statuses:
        ranked = ranked.exclude(status__in=set(excluded_statuses))
    return ranked
