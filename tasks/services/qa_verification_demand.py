"""DB-backed latest-wins QA demand authority (PLS-593).

Deployment events are immutable attempt evidence.  This service converts the
events that actually require work into one surface-scoped demand, persists a
version-fenced delayed outbox intent, and materializes only the newest version.
"""
from __future__ import annotations

import logging
import hashlib
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from tasks.models import (
    DeploymentEvent,
    QaVerificationAudit,
    QaVerificationDemand,
    QaVerificationGeneration,
    QaVerificationOutbox,
)


logger = logging.getLogger(__name__)

REGRESSION_WATCHLIST = 'regression_watchlist'


def canonical_deployment_surface(event: DeploymentEvent) -> str:
    """Return the typed surface identity; never use a title or event UUID."""
    return '|'.join([
        str(event.repo or '').strip().lower(),
        str(event.service or '').strip().lower(),
        str(event.cluster or '').strip().lower(),
        str(event.namespace or '').strip().lower(),
        str(event.deployment_target or '').strip().lower(),
    ])


def _artifact_identity(event: DeploymentEvent) -> str:
    return str(
        event.image_digest or event.manifest_ref or event.image_tag or event.image or ''
    ).strip()


def _event_fingerprint(event: DeploymentEvent) -> str:
    value = '|'.join([
        str(event.state or ''),
        str(event.smoke_result or ''),
        str(event.commit_sha or ''),
        _artifact_identity(event),
    ])
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _surface_sequence(event: DeploymentEvent):
    """Stable ordering evidence carried by the event, with server fallback."""
    return (
        event.rolled_out_at
        or event.rollout_started_at
        or event.started_at
        or event.created_at
        or timezone.now()
    )


def _settle_seconds() -> int:
    try:
        return max(0, int(getattr(settings, 'QA_VERIFICATION_SETTLE_SECONDS', 90)))
    except (TypeError, ValueError):
        return 90


def _enqueue_outbox(outbox_id, available_at) -> None:
    """Best-effort enqueue; the periodic DB sweep is the liveness authority."""
    try:
        from tasks.tasks import deliver_qa_verification_outbox

        countdown = max(0, int((available_at - timezone.now()).total_seconds()))
        deliver_qa_verification_outbox.apply_async(
            args=[str(outbox_id)], countdown=countdown,
        )
    except Exception:
        logger.exception('Could not enqueue QA verification outbox %s', outbox_id)


@transaction.atomic
def accept_regression_watchlist_event(
    event: DeploymentEvent,
) -> Optional[QaVerificationDemand]:
    """Advance the one regression demand for ``event``'s exact-org surface.

    Replaying an already accepted event is a no-op.  A newly seen event whose
    stable surface timestamp predates the current generation is retained as
    superseded evidence but cannot move the demand backwards.
    """
    if event.project_id is None:
        # Exact-org/project identity is mandatory.  Legacy direct helper calls
        # may still materialize without this authority, but deployment ingest
        # must never guess an organization.
        return None

    identity = {
        'organization_id': event.project.org_id,
        'project_id': event.project_id,
        'demand_kind': REGRESSION_WATCHLIST,
        'surface_key': canonical_deployment_surface(event),
        'environment': str(event.environment or 'live').strip().lower(),
        'primary_risk': '',
    }
    demand, _ = QaVerificationDemand.objects.get_or_create(**identity)
    demand = QaVerificationDemand.objects.select_for_update().get(pk=demand.pk)

    existing = QaVerificationGeneration.objects.filter(
        demand=demand, deployment_event=event,
    ).first()
    if existing is not None:
        fingerprint = _event_fingerprint(event)
        if existing.event_fingerprint == fingerprint:
            return demand
        existing.commit_sha = event.commit_sha
        existing.artifact_identity = _artifact_identity(event)
        existing.event_fingerprint = fingerprint
        existing.surface_sequence = _surface_sequence(event)
        existing.save(update_fields=[
            'commit_sha', 'artifact_identity', 'event_fingerprint', 'surface_sequence',
        ])
        if demand.current_generation == existing.generation:
            now = timezone.now()
            available_at = now + timedelta(seconds=_settle_seconds())
            demand.version = F('version') + 1
            demand.last_event_at = now
            demand.settle_after = available_at
            demand.save(update_fields=[
                'version', 'last_event_at', 'settle_after', 'updated_at',
            ])
            demand.refresh_from_db(fields=['version'])
            outbox = QaVerificationOutbox.objects.create(
                demand=demand,
                generation=existing.generation,
                version=demand.version,
                available_at=available_at,
            )
            transaction.on_commit(
                lambda oid=outbox.id, due=available_at: _enqueue_outbox(oid, due)
            )
        return demand

    last = (
        QaVerificationGeneration.objects.filter(demand=demand)
        .order_by('-generation')
        .first()
    )
    next_generation = (last.generation if last else 0) + 1
    sequence = _surface_sequence(event)
    current = None
    if demand.current_generation:
        current = QaVerificationGeneration.objects.filter(
            demand=demand, generation=demand.current_generation,
        ).first()

    stale_on_arrival = bool(current and sequence < current.surface_sequence)
    generation = QaVerificationGeneration.objects.create(
        demand=demand,
        generation=next_generation,
        deployment_event=event,
        commit_sha=event.commit_sha,
        artifact_identity=_artifact_identity(event),
        event_fingerprint=_event_fingerprint(event),
        surface_sequence=sequence,
        state='superseded' if stale_on_arrival else 'current',
    )
    if stale_on_arrival:
        QaVerificationAudit.objects.get_or_create(
            demand=demand,
            from_generation=next_generation,
            to_generation=demand.current_generation,
            effect_kind='stale_arrival_ignored',
            defaults={
                'deployment_event': event,
                'payload': {
                    'commit_sha': event.commit_sha,
                    'current_generation': demand.current_generation,
                },
            },
        )
        return demand

    previous_generation = demand.current_generation
    if previous_generation:
        QaVerificationGeneration.objects.filter(
            demand=demand,
            generation=previous_generation,
            state='current',
        ).update(state='superseded')
        previous = QaVerificationGeneration.objects.select_related(
            'deployment_event'
        ).get(demand=demand, generation=previous_generation)
        QaVerificationAudit.objects.get_or_create(
            demand=demand,
            from_generation=previous_generation,
            to_generation=generation.generation,
            effect_kind='generation_superseded',
            defaults={
                'deployment_event': event,
                'payload': {
                    'superseded_event_id': str(previous.deployment_event_id),
                    'superseded_commit_sha': previous.commit_sha,
                    'current_event_id': str(event.id),
                    'current_commit_sha': event.commit_sha,
                },
            },
        )

    now = timezone.now()
    available_at = now + timedelta(seconds=_settle_seconds())
    demand.current_generation = generation.generation
    demand.version = F('version') + 1
    demand.last_event_at = now
    demand.settle_after = available_at
    demand.save(update_fields=[
        'current_generation', 'version', 'last_event_at', 'settle_after', 'updated_at',
    ])
    demand.refresh_from_db(fields=['version'])
    outbox = QaVerificationOutbox.objects.create(
        demand=demand,
        generation=generation.generation,
        version=demand.version,
        available_at=available_at,
    )
    transaction.on_commit(
        lambda oid=outbox.id, due=available_at: _enqueue_outbox(oid, due)
    )
    return demand


@transaction.atomic
def materialize_qa_verification_outbox(
    outbox_id,
    *,
    now=None,
    force: bool = False,
) -> dict:
    """Materialize one matured outbox row iff its demand version is current."""
    now = now or timezone.now()
    outbox = (
        QaVerificationOutbox.objects.select_for_update()
        .select_related('demand')
        .filter(pk=outbox_id)
        .first()
    )
    if outbox is None:
        return {'status': 'missing', 'outbox_id': str(outbox_id)}
    if outbox.delivered_at:
        return {'status': 'delivered', 'outbox_id': str(outbox.id)}
    if not force and outbox.available_at > now:
        return {'status': 'pending', 'outbox_id': str(outbox.id)}

    QaVerificationOutbox.objects.filter(pk=outbox.pk).update(
        attempts=F('attempts') + 1,
    )
    demand = QaVerificationDemand.objects.select_for_update().get(pk=outbox.demand_id)
    if outbox.version != demand.version or outbox.generation != demand.current_generation:
        outbox.delivered_at = now
        outbox.last_error = ''
        outbox.save(update_fields=['delivered_at', 'last_error'])
        return {'status': 'superseded', 'outbox_id': str(outbox.id)}

    generation = QaVerificationGeneration.objects.select_related(
        'deployment_event', 'demand__active_item', 'demand__project',
    ).get(demand=demand, generation=outbox.generation)
    event = generation.deployment_event
    if event.is_failure:
        generation.state = 'failed'
        generation.save(update_fields=['state'])
        _retire_current_item(demand, generation, event, now=now)
        outbox.delivered_at = now
        outbox.save(update_fields=['delivered_at'])
        return {'status': 'failed_generation', 'outbox_id': str(outbox.id)}

    if demand.demand_kind == REGRESSION_WATCHLIST:
        from tasks.services.regression_watchlist import materialize_regression_watchlist_demand

        item = materialize_regression_watchlist_demand(demand, generation)
    else:
        raise ValueError(f'Unsupported QA demand kind: {demand.demand_kind}')

    outbox.delivered_at = now
    outbox.last_error = ''
    outbox.save(update_fields=['delivered_at', 'last_error'])
    return {
        'status': 'materialized' if item else 'no_work',
        'outbox_id': str(outbox.id),
        'item_id': str(item.pk) if item else None,
    }


def _retire_current_item(demand, generation, event, *, now=None):
    item = demand.active_item
    if item is None:
        return None
    metadata = item.metadata or {}
    if int(metadata.get('qa_demand_generation') or 0) != generation.generation:
        return None
    if item.status in {
        'completed', 'cancelled', 'canceled', 'closed', 'done', 'resolved', 'rejected',
        'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
    }:
        return item
    item.status = 'cancelled'
    item.save(update_fields=['status', 'updated_at'])
    audit, created = QaVerificationAudit.objects.get_or_create(
        demand=demand,
        from_generation=generation.generation,
        to_generation=generation.generation,
        effect_kind='current_generation_failed',
        defaults={
            'deployment_event': event,
            'item': item,
            'payload': {'state': event.state, 'smoke_result': event.smoke_result},
        },
    )
    if created:
        from tasks.models import Comment
        from tasks.services.mechanical_comments import (
            MechanicalCommentFactory,
            deterministic_event_id,
        )

        MechanicalCommentFactory.create(
            item=item,
            origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
            origin_event='qa_demand.failed',
            template_version=1,
            structured_payload={
                'generation': int(generation.generation),
                'commit_sha': str(event.commit_sha),
                'state': str(event.state),
                'smoke_result': str(event.smoke_result),
            },
            origin_event_id=deterministic_event_id(
                'qa_demand.failed', item, demand.id, generation.generation,
            ),
        )
    return item


@transaction.atomic
def mark_regression_watchlist_event_failed(event: DeploymentEvent):
    """Fence failure handling to the generation that currently owns the demand."""
    generation = (
        QaVerificationGeneration.objects.select_for_update(of=('self',))
        .select_related('demand__active_item')
        .filter(
            deployment_event=event,
            demand__demand_kind=REGRESSION_WATCHLIST,
        )
        .first()
    )
    if generation is None:
        return None
    generation.state = 'failed'
    generation.save(update_fields=['state'])
    demand = QaVerificationDemand.objects.select_for_update().get(pk=generation.demand_id)
    if demand.current_generation != generation.generation:
        return None
    return _retire_current_item(demand, generation, event)


def materialize_due_qa_demands(*, limit: int = 32, now=None) -> list[dict]:
    """Bounded recovery/materialization sweep; it never recomputes demand."""
    now = now or timezone.now()
    ids = list(
        QaVerificationOutbox.objects.filter(
            delivered_at__isnull=True,
            available_at__lte=now,
        ).order_by('available_at', 'created_at').values_list('id', flat=True)[:limit]
    )
    return [materialize_qa_verification_outbox(oid, now=now) for oid in ids]
