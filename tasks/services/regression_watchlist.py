"""PLAT-1210: post-deploy regression watchlist.

When a service rolls out, the bugs fixed on that service in the recent past
are exactly the surfaces most likely to regress — PLAT-287 ("Books MCP missing
X-Organization-ID", fixed and done) was re-broken by the books-42a2fe1 rollout
and re-found by an internal user (BKS-44), not by QA, because nothing connects
deployment events to a service's recently-fixed bugs.

This module closes that loop on the PLS-183 deployment-event ledger: a
successful rollout event produces ONE QA re-verification task listing the
service's bugs closed in the last ``WATCHLIST_DAYS`` days, so QA (or an
automated probe) re-runs each bug's recorded repro against the new rollout.

Deliberate properties:
- Event-driven (fires from ``upsert_deployment_event``), not a sweep.
- Idempotent per deployment event: CI retries and state progressions
  (rollout_started → smoke_passed) update the same task.
- No recently-fixed bugs → no task (no noise).
- Watchlist creation must never break deployment ingestion.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tasks.label_utils import item_labels_match_q
from tasks.models import (
    Comment,
    DeploymentEvent,
    Item,
    Project,
    QaVerificationAudit,
    QaVerificationDemand,
    QaVerificationGeneration,
)

logger = logging.getLogger(__name__)

WATCHLIST_DAYS = 30
WATCHLIST_MAX_BUGS = 25
# Earliest point the new code can be live; later states for the same event
# update the same task via the idempotent lookup.
ROLLOUT_STATES = {'rollout_started', 'smoke_pending', 'smoke_passed'}
FIXED_STATUSES = {'done', 'resolved', 'completed', 'closed'}
BUG_WORKFLOWS = {'autonomous-bug', 'bug-workflow'}
_TERMINAL_STATUSES = {
    'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def recently_fixed_bugs(event: DeploymentEvent, days: int = WATCHLIST_DAYS):
    """Bugs for this event's service fixed within the window, newest first.

    A bug belongs to the service when its project matches the event's project,
    or its labels/metadata carry the event's service name. ``item_type``
    bug-prefix and bug workflows are both honored so autonomous-bug tasks and
    typed bugs are covered.
    """
    since = timezone.now() - timedelta(days=days)
    qs = (
        Item.objects
        .filter(mode='task', status__in=FIXED_STATUSES)
        .filter(Q(completed_at__gte=since) | Q(completed_at__isnull=True, updated_at__gte=since))
        .filter(Q(workflow_name__in=BUG_WORKFLOWS) | Q(item_type__startswith='task.bug'))
    )
    service_q = (
        item_labels_match_q({'service': event.service})
        | Q(metadata__service=event.service)
    )
    if event.project_id:
        service_q |= Q(project_id=event.project_id)
    qs = qs.filter(service_q)
    # The watchlist task itself (and other deployment automation) is not a bug
    # surface to re-verify.
    qs = qs.exclude(source='deployment-event')
    return qs.order_by('-completed_at', '-updated_at')[:WATCHLIST_MAX_BUGS]


def _watchlist_title(event: DeploymentEvent, count: int) -> str:
    service = event.service or event.repo
    return (
        f"[Regression watchlist] {service} → {event.environment} "
        f"({event.commit_sha[:12]}): re-verify {count} recently-fixed surface(s)"
    )[:400]


def _watchlist_description(event: DeploymentEvent, bugs) -> str:
    lines = [
        f"`{event.service}` rolled out (state `{event.state}`, commit `{event.commit_sha}`, "
        f"image `{event.image_digest or event.image_tag or event.image or 'unknown'}`).",
        '',
        f"Re-verify each surface fixed in the last {WATCHLIST_DAYS} days against the NEW rollout "
        "(use the repro/verification evidence recorded on each bug; PLAT-287→BKS-44 showed a "
        "rollout can silently re-break a fixed surface):",
        '',
    ]
    for bug in bugs:
        closed = bug.completed_at or bug.updated_at
        closed_str = closed.date().isoformat() if closed else 'unknown'
        lines.append(f"- [ ] **{bug.item_key or bug.pk}** — {bug.title} (closed {closed_str})")
    lines.extend([
        '',
        'Acceptance: each box checked with a one-line re-verification result, or a new '
        'regression bug filed and linked for any surface that re-broke.',
        '',
        f"DeploymentEvent: `{event.id}` (idempotency `{event.idempotency_key}`)",
    ])
    return '\n'.join(lines)


def _find_existing_watchlist_task(event: DeploymentEvent) -> Item | None:
    if (
        event.qa_task_id
        and event.qa_task
        and _is_regression_watchlist_task(event.qa_task)
        and event.qa_task.status not in _TERMINAL_STATUSES
    ):
        return event.qa_task
    return (
        Item.objects
        .filter(
            source='deployment-event',
            metadata__deployment_event_id=str(event.id),
            metadata__deployment_qa_kind='regression-watchlist',
        )
        .exclude(status__in=_TERMINAL_STATUSES)
        .order_by('-created_at')
        .first()
    )


def _is_regression_watchlist_task(task: Item | None) -> bool:
    if task is None:
        return False
    return (
        task.item_type == 'task.regression_watchlist'
        or (task.metadata or {}).get('deployment_qa_kind') == 'regression-watchlist'
    )


def _watchlist_may_own_event_pointer(event: DeploymentEvent) -> bool:
    """The single legacy pointer is primary-QA, not shared mutable ownership.

    Focused operator-surface QA and the broader regression watchlist can both
    exist for one deployment.  The watchlist may fill an empty/terminal pointer
    or keep its own pointer, but must never replace/rewrite a focused-QA task.
    """
    if not event.qa_task_id or not event.qa_task:
        return True
    if event.qa_task.status in _TERMINAL_STATUSES:
        return True
    return _is_regression_watchlist_task(event.qa_task)


def _emit_demand_created_comment(item, demand, generation, event) -> None:
    from tasks.services.mechanical_comments import (
        MechanicalCommentFactory,
        deterministic_event_id,
    )

    MechanicalCommentFactory.create(
        item=item,
        origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='qa_demand.created',
        template_version=1,
        structured_payload={
            'demand_id': str(demand.id),
            'generation': int(generation.generation),
            'event_id': str(event.id),
            'commit_sha': str(event.commit_sha),
        },
        origin_event_id=deterministic_event_id(
            'qa_demand.created', item, demand.id, generation.generation,
        ),
    )


def _emit_demand_superseded_comment(item, audit, event) -> None:
    from tasks.services.mechanical_comments import (
        MechanicalCommentFactory,
        deterministic_event_id,
    )

    payload = audit.payload or {}
    MechanicalCommentFactory.create(
        item=item,
        origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='qa_demand.superseded',
        template_version=1,
        structured_payload={
            'from_generation': int(audit.from_generation),
            'to_generation': int(audit.to_generation),
            'superseded_event_id': str(payload.get('superseded_event_id') or ''),
            'superseded_commit_sha': str(payload.get('superseded_commit_sha') or ''),
            'current_event_id': str(event.id),
            'current_commit_sha': str(event.commit_sha),
        },
        origin_event_id=deterministic_event_id(
            'qa_demand.superseded', item, audit.demand_id,
            audit.from_generation, audit.to_generation,
        ),
    )


@transaction.atomic
def ensure_regression_watchlist_task(event: DeploymentEvent) -> Item | None:
    """Synchronously materialize one event (direct/test compatibility helper).

    Production ingestion calls :func:`maybe_create_watchlist`, which only
    advances the durable demand/outbox and lets the settled worker materialize.
    """
    if event.is_failure or event.state not in ROLLOUT_STATES:
        return None
    bugs = list(recently_fixed_bugs(event))
    if not bugs:
        return None

    if event.project_id:
        from tasks.services.qa_verification_demand import (
            accept_regression_watchlist_event,
            materialize_qa_verification_outbox,
        )

        demand = accept_regression_watchlist_event(event)
        if demand is None:
            return None
        outbox = demand.outbox.filter(version=demand.version).first()
        if outbox is None:
            return demand.active_item
        materialize_qa_verification_outbox(outbox.id, force=True)
        demand.refresh_from_db()
        return demand.active_item

    # Legacy direct helper path for an event with no exact project/org.  The
    # ingestion path refuses this ambiguity; retained only for callers that
    # explicitly construct a project-less event and materialize immediately.

    title = _watchlist_title(event, len(bugs))
    description = _watchlist_description(event, bugs)
    metadata = {
        'deployment_event_id': str(event.id),
        'deployment_event_key': event.idempotency_key,
        'deployment_qa_kind': 'regression-watchlist',
        'repo': event.repo,
        'service': event.service,
        'environment': event.environment,
        'commit_sha': event.commit_sha,
        'watchlist_task_keys': [b.item_key or str(b.pk) for b in bugs],
    }

    existing = _find_existing_watchlist_task(event)
    if existing:
        existing.title = title
        existing.description = description
        existing.metadata = {**(existing.metadata or {}), **metadata}
        existing.save(update_fields=['title', 'description', 'metadata', 'updated_at'])
        if event.qa_task_id != existing.pk and _watchlist_may_own_event_pointer(event):
            event.qa_task = existing
            event.save(update_fields=['qa_task', 'updated_at'])
        return existing

    project = event.project or Project.objects.filter(key__iexact='PLS', is_active=True).first() \
        or Project.objects.filter(is_active=True).order_by('id').first()
    task = Item.objects.create(
        project=project,
        item_key=project.get_next_item_key() if project else None,
        mode='task',
        item_type='task.regression_watchlist',
        title=title,
        description=description,
        source='deployment-event',
        source_id=str(event.id),
        source_url=event.pipeline_url or event.logs_url or None,
        status='open',
        priority='high',
        assignment_group='qa',
        workflow_name='autonomous-dev',
        created_by_id=0,
        created_by_email='ci@shizuha.com',
        labels={'source': 'deployment-event', 'service': event.service,
                'environment': event.environment, 'kind': 'regression-watchlist'},
        tags='deployment-events,qa,regression-watchlist,requires-live-qa',
        metadata=metadata,
    )
    if _watchlist_may_own_event_pointer(event):
        event.qa_task = task
        event.save(update_fields=['qa_task', 'updated_at'])
    Comment.objects.create(
        item=task,
        author_email='ci@shizuha.com',
        author_id=0,
        content=(
            f"Auto-created from DeploymentEvent `{event.id}` (PLAT-1210): this service rolled "
            "out, so its recently-fixed bug surfaces need a cheap re-verification. Retries/state "
            "updates for the same event update this task instead of duplicating it."
        ),
    )
    return task


@transaction.atomic
def materialize_regression_watchlist_demand(
    demand: QaVerificationDemand,
    generation: QaVerificationGeneration,
) -> Item | None:
    """Retarget/create the single active watchlist for the current generation."""
    demand = (
        QaVerificationDemand.objects.select_for_update(of=('self',))
        .select_related('active_item', 'project')
        .get(pk=demand.pk)
    )
    if generation.generation != demand.current_generation:
        return demand.active_item
    event = generation.deployment_event
    if event.is_failure or event.state not in ROLLOUT_STATES:
        return None
    bugs = list(recently_fixed_bugs(event))
    if not bugs:
        return None

    title = _watchlist_title(event, len(bugs))
    description = _watchlist_description(event, bugs)
    metadata = {
        'deployment_event_id': str(event.id),
        'deployment_event_key': event.idempotency_key,
        'deployment_qa_kind': 'regression-watchlist',
        'qa_demand_id': str(demand.id),
        'qa_demand_generation': generation.generation,
        'qa_demand_version': demand.version,
        'qa_demand_surface_key': demand.surface_key,
        'repo': event.repo,
        'service': event.service,
        'environment': event.environment,
        'commit_sha': event.commit_sha,
        'artifact_identity': generation.artifact_identity,
        'watchlist_task_keys': [b.item_key or str(b.pk) for b in bugs],
    }

    existing = demand.active_item
    if existing and existing.status in _TERMINAL_STATUSES:
        existing = None
    if existing is None:
        existing = (
            Item.objects.filter(
                source='deployment-event',
                metadata__qa_demand_id=str(demand.id),
            )
            .exclude(status__in=_TERMINAL_STATUSES)
            .order_by('-created_at')
            .first()
        )

    if existing:
        previous_generation = int(
            (existing.metadata or {}).get('qa_demand_generation') or 0
        )
        previous_event_id = (existing.metadata or {}).get('deployment_event_id')
        previous_commit = (existing.metadata or {}).get('commit_sha')
        existing.title = title
        existing.description = description
        existing.source_id = str(demand.id)
        existing.source_url = event.pipeline_url or event.logs_url or existing.source_url
        existing.metadata = {**(existing.metadata or {}), **metadata}
        existing.save(update_fields=[
            'title', 'description', 'source_id', 'source_url', 'metadata', 'updated_at',
        ])
        if previous_generation and previous_generation != generation.generation:
            audit, created = QaVerificationAudit.objects.get_or_create(
                demand=demand,
                from_generation=previous_generation,
                to_generation=generation.generation,
                effect_kind='generation_superseded',
                defaults={
                    'deployment_event': event,
                    'item': existing,
                    'payload': {
                        'superseded_event_id': previous_event_id,
                        'superseded_commit_sha': previous_commit,
                        'current_event_id': str(event.id),
                        'current_commit_sha': event.commit_sha,
                    },
                },
            )
            comment_needed = audit.item_id != existing.id
            if comment_needed:
                audit.item = existing
                audit.payload = {
                    **(audit.payload or {}),
                    'superseded_event_id': previous_event_id,
                    'superseded_commit_sha': previous_commit,
                    'current_event_id': str(event.id),
                    'current_commit_sha': event.commit_sha,
                }
                audit.save(update_fields=['item', 'payload'])
                _emit_demand_superseded_comment(existing, audit, event)
        task = existing
    else:
        project = demand.project
        task = Item.objects.create(
            project=project,
            item_key=project.get_next_item_key(),
            mode='task',
            item_type='task.regression_watchlist',
            title=title,
            description=description,
            source='deployment-event',
            source_id=str(demand.id),
            source_url=event.pipeline_url or event.logs_url or None,
            status='open',
            priority='high',
            assignment_group='qa',
            workflow_name='autonomous-dev',
            created_by_id=0,
            created_by_email='ci@shizuha.com',
            labels={
                'source': 'deployment-event', 'service': event.service,
                'environment': event.environment, 'kind': 'regression-watchlist',
            },
            tags='deployment-events,qa,regression-watchlist,requires-live-qa',
            metadata=metadata,
        )
        _emit_demand_created_comment(task, demand, generation, event)
        supersession = QaVerificationAudit.objects.filter(
            demand=demand,
            to_generation=generation.generation,
            effect_kind='generation_superseded',
        ).first()
        if supersession and supersession.item_id is None:
            supersession.item = task
            supersession.save(update_fields=['item'])
            _emit_demand_superseded_comment(task, supersession, event)

    demand.active_item = task
    demand.save(update_fields=['active_item', 'updated_at'])
    if _watchlist_may_own_event_pointer(event):
        event.qa_task = task
        event.save(update_fields=['qa_task', 'updated_at'])
    return task


@transaction.atomic
def retire_watchlist_task(event: DeploymentEvent) -> Item | None:
    """Cancel an open watchlist when its deployment event turns into a failure.

    Review P1: `rollout_started` legitimately creates the watchlist early, but
    if the SAME event later reports `smoke_failed`/`rollout_stalled` etc., the
    checklist would tell QA to re-verify surfaces against a rollout that did
    not pass — the exact wrong queue signal next to the DevOps failure task.
    """
    existing = _find_existing_watchlist_task(event)
    if existing is None:
        return None
    existing.status = 'cancelled'
    existing.save(update_fields=['status', 'updated_at'])
    Comment.objects.create(
        item=existing,
        author_email='ci@shizuha.com',
        author_id=0,
        content=(
            f"Cancelled: DeploymentEvent `{event.id}` progressed to failure state "
            f"`{event.state}` (smoke `{event.smoke_result}`) — this rollout did not "
            "pass, so there is no new rollout to re-verify against. The DevOps "
            "failure task tracks the deployment itself; a fresh watchlist is "
            "created when a later rollout of this service succeeds (PLAT-1210)."
        ),
    )
    logger.info("retired regression watchlist %s for failed event %s", existing.pk, event.pk)
    return existing


def maybe_create_watchlist(event: DeploymentEvent) -> Item | None:
    """Advance the durable latest-wins demand; materialization is delayed."""
    try:
        if event.is_failure or event.state not in ROLLOUT_STATES:
            return None
        if not recently_fixed_bugs(event).exists():
            return None
        from tasks.services.qa_verification_demand import accept_regression_watchlist_event

        demand = accept_regression_watchlist_event(event)
        return demand.active_item if demand else None
    except Exception:
        logger.warning("regression watchlist creation failed for event %s", event.pk, exc_info=True)
        return None


def maybe_retire_watchlist(event: DeploymentEvent) -> Item | None:
    """Retire only when this failed event still owns the current generation."""
    try:
        from tasks.services.qa_verification_demand import mark_regression_watchlist_event_failed

        retired = mark_regression_watchlist_event_failed(event)
        if retired is not None:
            return retired
        # Pre-migration/legacy event-specific rows remain safely supported.
        if not event.qa_demand_generations.exists():
            return retire_watchlist_task(event)
        return None
    except Exception:
        logger.warning("regression watchlist retirement failed for event %s", event.pk, exc_info=True)
        return None
