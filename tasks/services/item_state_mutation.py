"""PLS-467 transactional Item state generations and durable occurrences."""

from __future__ import annotations

import contextvars
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, transaction
from django.utils import timezone


_ACTIVE_ITEMS = contextvars.ContextVar('pulse_item_state_active_items', default=frozenset())

_FIELD_KIND = {
    'assignee_id': 'assignment',
    'assignee_email': 'assignment',
    'assignment_group': 'assignment',
    'project_id': 'routing_scope',
    'workflow_name': 'status',
    'workflow_status': 'status',
    'status': 'status',
    'resolution_id': 'status',
    'resolution_date': 'status',
    'is_archived': 'status',
    'archived_at': 'status',
    'priority': 'priority',
}


@dataclass(frozen=True)
class PreparedMutation:
    item_id: int
    token: uuid.UUID
    old_generation: int
    new_generation: int
    changed_kinds: tuple[str, ...]
    old_values: Mapping[str, object]


def item_state_mutation_active(item_id: int) -> bool:
    """Return whether this execution context is finalizing ``item_id``."""

    return item_id in _ACTIVE_ITEMS.get()


def _selected(update_fields: Optional[Iterable[str]], field: str) -> bool:
    if update_fields is None:
        return True
    selected = set(update_fields)
    aliases = {field, field.removesuffix('_id')}
    return bool(selected & aliases)


def _changed_kinds(item, old: Mapping[str, object], update_fields) -> tuple[str, ...]:
    kinds = set()
    for field, kind in _FIELD_KIND.items():
        if not _selected(update_fields, field):
            continue
        if old.get(field) != getattr(item, field):
            kinds.add(kind)

    if _selected(update_fields, 'metadata'):
        old_prs = (old.get('metadata') or {}).get('pull_requests') or []
        new_prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
        if old_prs != new_prs:
            kinds.add('pull_request')
    return tuple(sorted(kinds))


def _set_marker(item_id: int, token: uuid.UUID) -> None:
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('pulse.item_state_mutation_token', %s, true), "
            "set_config('pulse.item_state_mutation_item_id', %s, true)",
            [str(token), str(item_id)],
        )


def _clear_marker() -> None:
    # A failed INSERT inside atomic marks the connection rollback-only; issuing
    # cleanup SQL then masks the root error with TransactionManagementError.
    # Transaction-local GUCs clear with that rollback, so only clear eagerly on
    # a healthy connection.
    if connection.vendor != 'postgresql' or connection.needs_rollback:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('pulse.item_state_mutation_token', '', true), "
            "set_config('pulse.item_state_mutation_item_id', '', true)"
        )


def prepare_model_save(item, old_values: Mapping[str, object], update_fields=None):
    """Arm one typed mutation immediately before ``Item.save`` writes.

    The PostgreSQL trigger validates the marked ``OLD + 1`` allocation and
    leaves outbox ownership to :func:`finish_model_save`.  Unmarked bulk/raw
    writers are handled by the trigger's generic fallback.
    """

    old_generation = int(old_values.get('state_generation') or 0)
    if update_fields is None and int(getattr(item, 'state_generation', 0) or 0) != old_generation:
        # A full save from a long-lived instance is ambiguous after a bulk/link
        # writer advanced authority.  Preserve every registered DB state field
        # (and only the state-bearing PR slice of metadata); callers intending a
        # state mutation must name it via update_fields/the typed service.  This
        # turns stale full saves into context-only writes instead of N→N-1 or
        # equal-looking ABA rollbacks.
        for field in _FIELD_KIND:
            setattr(item, field, old_values.get(field))
        current_metadata = dict(getattr(item, 'metadata', None) or {})
        old_metadata = old_values.get('metadata') or {}
        if 'pull_requests' in old_metadata:
            current_metadata['pull_requests'] = old_metadata['pull_requests']
        else:
            current_metadata.pop('pull_requests', None)
        item.metadata = current_metadata
        item.state_generation = old_generation

    kinds = _changed_kinds(item, old_values, update_fields)
    if not kinds:
        return None
    active = _ACTIVE_ITEMS.get()
    if item.pk in active:
        raise ValidationError({
            'state_generation': (
                'Nested state mutation for the same Item is forbidden; fold '
                'the delta into the active PLS-467 mutation.'
            ),
        })
    prepared = PreparedMutation(
        item_id=item.pk,
        token=uuid.uuid4(),
        old_generation=old_generation,
        new_generation=old_generation + 1,
        changed_kinds=kinds,
        old_values=dict(old_values),
    )
    item.state_generation = prepared.new_generation
    _ACTIVE_ITEMS.set(active | {item.pk})
    try:
        from ..models import ItemStateMutationMarker
        ItemStateMutationMarker.objects.create(
            mutation_token=prepared.token,
            item_id=item.pk,
            state_generation=prepared.new_generation,
        )
        _set_marker(item.pk, prepared.token)
    except Exception:
        _ACTIVE_ITEMS.set(active)
        raise
    return prepared


def _artifact_snapshot(item):
    artifacts = []
    for raw in (item.metadata or {}).get('pull_requests') or []:
        if not isinstance(raw, dict):
            continue
        artifacts.append({
            'provider': raw.get('provider') or 'origin-forgejo',
            'repo': raw.get('repo'),
            'number': raw.get('number'),
            'head_sha': raw.get('head_sha'),
            'tree_sha': raw.get('tree_sha'),
        })
    artifacts.sort(key=lambda row: (
        str(row.get('provider') or ''), str(row.get('repo') or ''),
        str(row.get('number') or ''), str(row.get('head_sha') or ''),
        str(row.get('tree_sha') or ''),
    ))
    canonical = json.dumps(artifacts, sort_keys=True, separators=(',', ':'))
    return artifacts, hashlib.sha256(canonical.encode()).hexdigest()


def _envelope(item, prepared: PreparedMutation):
    artifacts, fingerprint = _artifact_snapshot(item)
    actor = getattr(item, '_activity_actor', None)
    actor_id, actor_email = actor if actor else (
        getattr(item, 'created_by_id', None), getattr(item, 'created_by_email', '') or ''
    )
    organization_id = None
    if item.project_id:
        from ..models import Project
        organization_id = Project.objects.filter(pk=item.project_id).values_list(
            'org_id', flat=True
        ).first()
    event_id = str(prepared.token)
    envelope = {
        'specversion': '1.0',
        'id': event_id,
        'source': f'/pulse/items/{item.pk}/state',
        'type': 'io.shizuha.pulse.item-state.v1',
        'subject': item.item_key or str(item.pk),
        'time': timezone.now().isoformat(),
        'data': {
            'organization_id': organization_id,
            'item_id': str(item.pk),
            'item_key': item.item_key,
            # JSON strings preserve BIGINT exactly across JS/Connect boundaries.
            'state_generation': str(prepared.new_generation),
            'changed_kinds': list(prepared.changed_kinds),
            'actor': {'user_id': actor_id, 'email': actor_email},
            'cause': getattr(item, '_state_mutation_cause', {'kind': 'item_save'}),
            'state': {
                'status': item.status,
                'resolution_id': item.resolution_id,
                'assignee_id': item.assignee_id,
                'assignee_email': item.assignee_email,
                'assignment_group': item.assignment_group,
                'priority': item.priority,
            },
            'artifacts': {
                'fingerprint_sha256': fingerprint,
                'pull_requests': artifacts,
            },
            'supersession_key': (
                f'pulse:org:{organization_id}:item:{item.pk}:state'
            ),
        },
    }
    # Resolution IDs and other typed model values may be UUID/date/Decimal.
    # Persist the same JSON-safe representation Django exposes over its APIs.
    return json.loads(json.dumps(envelope, cls=DjangoJSONEncoder))


def _is_generation_unique_violation(exc: BaseException) -> bool:
    """True when ``uniq_item_state_event_generation`` caused ``exc``."""

    needle = 'uniq_item_state_event_generation'
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diag = getattr(current, 'diag', None)
        if getattr(diag, 'constraint_name', None) == needle:
            return True
        if needle in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _adopt_generation_event(existing, item, prepared: PreparedMutation) -> None:
    """Bind a leftover ``(item, state_generation)`` row to this mutation.

    The deferred PLS-467 marker trigger requires exactly one typed outbox
    row whose ``mutation_token`` matches the in-flight marker.  Keep the
    stable event UUID so relay dedup still holds.
    """

    existing.mutation_token = prepared.token
    existing.writer_kind = 'typed'
    update_fields = ['mutation_token', 'writer_kind']
    if existing.disposition != 'applied':
        existing.changed_kinds = list(prepared.changed_kinds)
        envelope = _envelope(item, prepared)
        envelope['id'] = str(existing.id)
        existing.envelope = envelope
        update_fields.extend(['changed_kinds', 'envelope'])
    existing.save(update_fields=update_fields)


def finish_model_save(item, prepared: PreparedMutation) -> None:
    """Append the exact typed occurrence; failure rolls the outer save back.

    PLS-1024: the append is idempotent on ``(item, state_generation)``.  A
    failed transition can leave a partial outbox row for the same generation
    while the item write rolled back, so a retry collides on
    ``uniq_item_state_event_generation`` and 500s forever.  IntegrityError on
    that constraint adopts the existing row and continues — including an
    already-applied generation, which still has to satisfy the deferred
    marker trigger so the item save can complete.
    """

    try:
        from ..models import ItemStateEventOutbox
        try:
            with transaction.atomic():
                ItemStateEventOutbox.objects.create(
                    id=prepared.token,
                    item=item,
                    mutation_token=prepared.token,
                    state_generation=prepared.new_generation,
                    changed_kinds=list(prepared.changed_kinds),
                    envelope=_envelope(item, prepared),
                    writer_kind='typed',
                )
        except IntegrityError as exc:
            if not _is_generation_unique_violation(exc):
                raise
            existing = (
                ItemStateEventOutbox.objects
                .select_for_update()
                .filter(item=item, state_generation=prepared.new_generation)
                .first()
            )
            if existing is None:
                raise
            _adopt_generation_event(existing, item, prepared)
    finally:
        _clear_marker()
        active = _ACTIVE_ITEMS.get()
        _ACTIVE_ITEMS.set(frozenset(pk for pk in active if pk != item.pk))


def abort_model_save(item, prepared: PreparedMutation) -> None:
    _clear_marker()
    active = _ACTIVE_ITEMS.get()
    _ACTIVE_ITEMS.set(frozenset(pk for pk in active if pk != item.pk))


class ItemStateMutationService:
    """Canonical typed entry point for a complete tracked-field delta."""

    @classmethod
    def mutate(cls, item_id, *, changes, actor=None, cause=None):
        with transaction.atomic():
            from ..models import Item
            item = Item.objects.select_for_update().get(pk=item_id)
            if actor:
                item._activity_actor = actor
            if cause:
                item._state_mutation_cause = cause
            for field, value in changes.items():
                if field not in _FIELD_KIND and field != 'metadata':
                    raise ValidationError({field: 'Not a registered PLS-467 state field.'})
                setattr(item, field, value)
            item.save(update_fields=list(changes) + ['updated_at'])
            return item
