"""PLS-529 durable operator-wait classification.

The marker is proof-derived, not a new workflow status: a dependency root stays
visible and keeps ownership, but every runnable-demand caller treats it as
waiting after a current EVOL-36 acknowledgement proves the operator ask was
surfaced.  Mutations tombstone the exact digest revision, so availability churn
cannot re-arm it and the same stale digest cannot hide new evidence.

PLS-527 extends the same marker with an evidence fingerprint: the first complete
probe records checklist+fingerprint; unchanged probes are no-ops (no external
re-probe, no re-lease); a changed fingerprint is the sole evidence-event re-arm
and queues exactly one verification.  No parallel authority and no Backlog/
Scheduled park of active blocker roots.
"""

from __future__ import annotations

import hashlib
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from tasks.models import (
    Comment,
    Item,
    OperatorWaitDigestState,
    OperatorWaitMarker,
)


DIGEST_ITEM_KEY = 'EVOL-36'
logger = logging.getLogger(__name__)
_OPERATOR_LANES = frozenset({'admin-ops', 'human-ops', 'ceo-decisions'})
_OPERATOR_CLASSIFICATIONS = frozenset({
    'genuine_decision_or_physical_action',
    'operator_held_dependency',
})
_DIGEST_HEADING = re.compile(
    r'(?im)^##\s+(?:early\s+)?blocked-on-operator digest\b'
)
_MESSAGE_ID = re.compile(
    r'(?i)message[_ -]?id\s*:?[\s`]*(?P<id>[0-9a-f]{8}-[0-9a-f-]{27,36})'
)
_ITEM_KEY = re.compile(r'\b[A-Z][A-Z0-9]{1,11}-\d+\b')


def record_operator_wait_health(result=None, *, state='success', source='reconcile', error=''):
    """Persist and emit the PLAT-1254 liveness contract for this mechanism."""
    from tasks import liveness

    result = result if isinstance(result, dict) else {}
    payload = {
        'state': state,
        'source': str(source or 'unknown')[:80],
        'activated': int(result.get('activated') or 0),
        'invalidated': int(result.get('invalidated') or 0),
        'candidates': int(result.get('candidates') or 0),
        'skipped': str(result.get('skipped') or '')[:80],
        'error': str(error or '')[:500],
        'at': timezone.now().isoformat(),
        'invariant': (
            'only surfaced canonical operator-only blocker roots are non-runnable; '
            'proof mutation or deadline re-arms exactly one eligible lease'
        ),
    }
    try:
        # ``operator_wait_last_run_ts`` is a success-liveness signal. A failed
        # parser/write must leave it stale so /statusz detects the outage rather
        # than making a broken reconciliation look healthy.
        if state == 'success':
            payload['last_run_ts'] = liveness.record_heartbeat(
                liveness.OPERATOR_WAIT,
            )
        cache.set('pulse:operator_wait:last', payload, timeout=None)
        cache.set(f'pulse:operator_wait:last_{state}', payload, timeout=None)
    except Exception:
        logger.exception('PLS-529 operator-wait liveness persistence failed')
    logger.warning(
        'metric:operator_wait_health state=%s source=%s activated=%d '
        'invalidated=%d candidates=%d skipped=%s error=%s',
        payload['state'], payload['source'], payload['activated'],
        payload['invalidated'], payload['candidates'], payload['skipped'],
        payload['error'],
    )
    return payload


def notify_operator_wait_owner(payload):
    """DM the Engineering owner when operator-wait reconciliation goes dark."""
    try:
        from tasks.services.connect_origin import system_message_headers
        from tasks.services.notifications import UserNotificationService
        from tasks.services.workflow_service import WorkflowService
        from tasks.user_service import user_service

        lead_email = (
            WorkflowService._resolve_team_lead_email('engineering')
            or 'kai@shizuha.com'
        )
        info = user_service.get_user_by_email(lead_email)
        user_id = info.get('id') if isinstance(info, dict) else None
        if not user_id:
            logger.error(
                'PLS-529 fail-loud DM recipient resolution failed for %s',
                lead_email,
            )
            return False

        content = (
            '🔴 ANDON [PLS-529 operator-wait health]\n'
            f"- owner: Engineering / `{lead_email}`\n"
            f"- source: `{payload.get('source')}`\n"
            f"- state: `{payload.get('state')}`\n"
            f"- error: `{payload.get('error') or ''}`\n"
            f"- invariant: {payload.get('invariant')}\n"
            '- liveness: `operator_wait_last_run_ts` on `/statusz` + '
            '`pulse:operator_wait:last_*` cache markers\n'
            '- need: inspect operator-wait reconciliation before the next '
            'failover/rebalance interval.'
        )
        connect_base_url = getattr(
            settings, 'SHIZUHA_CONNECT_API_URL',
            'http://shizuha-connect:8019/api',
        ).rstrip('/')
        body = {
            'recipient_user_id': int(user_id),
            'content': content,
            'sender_name': 'Pulse Mechanism Health',
            'sender_service': 'shizuha-pulse',
        }
        response = requests.post(
            f'{connect_base_url}/internal/system-message/',
            json=body,
            headers=system_message_headers(body),
            timeout=5,
        )
        UserNotificationService().notify_user(
            int(user_id),
            'Operator-wait reconciliation failure',
            content,
            event_type='alert',
            severity='error',
        )
        delivered = 200 <= getattr(response, 'status_code', 0) < 300
        if not delivered:
            logger.error(
                'PLS-529 operator-wait Connect DM failed for %s: HTTP %s',
                lead_email, getattr(response, 'status_code', None),
            )
        return delivered
    except Exception:
        logger.exception('PLS-529 operator-wait owner notification failed')
        return False


def report_operator_wait_failure(source, error, *, target=''):
    """Fail-loud path shared by the sweep and every mutation hook.

    The triggering write remains available, but the failure is never log-only:
    it updates durable health evidence and DMs the owning Engineering lead.
    Identical bursts are DM-deduped for five minutes while every occurrence
    still refreshes the failure metric/cache marker.
    """
    try:
        error_text = f'{type(error).__name__}: {error}'
        if target:
            error_text = f'{error_text} target={target}'
        payload = record_operator_wait_health(
            state='failure', source=source, error=error_text,
        )
        digest = hashlib.sha256(
            f"{payload['source']}\n{payload['error']}".encode('utf-8')
        ).hexdigest()[:20]
        try:
            should_notify = cache.add(
                f'pulse:operator_wait:failure_dm:{digest}', 1, timeout=300,
            )
        except Exception:
            should_notify = True
        if should_notify:
            notify_operator_wait_owner(payload)
        return payload
    except Exception:
        logger.exception('PLS-529 operator-wait fail-loud path failed')
        return None


def exclude_operator_wait_items(queryset):
    """Remove only currently-active, pre-deadline operator-wait roots."""
    live = OperatorWaitMarker.objects.filter(
        item_id=OuterRef('pk'),
        state='active',
    ).filter(Q(deadline_at__isnull=True) | Q(deadline_at__gt=timezone.now()))
    return queryset.annotate(_operator_wait_active=Exists(live)).filter(
        _operator_wait_active=False,
    )


def _revision(comment: Comment) -> str:
    material = f'{comment.pk}\n{comment.content or ""}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _digest_payload(comment: Comment):
    content = comment.content or ''
    if not _DIGEST_HEADING.search(content):
        return None
    if not re.search(r'(?i)\bDM\s+(?:delivered|sent)\b', content):
        return None
    match = _MESSAGE_ID.search(content)
    if not match:
        return None
    keys = tuple(dict.fromkeys(_ITEM_KEY.findall(content.upper())))
    if not keys:
        return None
    return {
        'message_id': match.group('id').lower(),
        'keys': keys,
        'revision': _revision(comment),
    }


def _authorized_digest_author(
    comment: Comment,
    *,
    authority_item: Item | None = None,
) -> bool:
    """Require Admin Ops authority in the item organization being controlled."""
    from tasks.services.workflow_service import WorkflowService

    email = (comment.author_email or '').strip().lower()
    if not email:
        return False
    tiers, _org_slug = WorkflowService._team_roster_entry_for_item(
        'admin-ops', authority_item or comment.item,
    )
    members = WorkflowService._flat_roster(tiers or [])
    aliases = WorkflowService._agent_email_aliases(email)
    return any(
        aliases & WorkflowService._agent_email_aliases(member)
        for member in members
    )


def _gate_proves_operator_only(item: Item) -> bool:
    metadata = item.metadata or {}
    if metadata.get('operator_dependency_kind') == 'operator_only':
        return True
    gate = metadata.get('escalation_routing_gate') or {}
    classification = str(gate.get('classification') or '').strip().lower()
    reason = str(gate.get('reason_code') or '').strip().lower()
    outcome = str(gate.get('outcome') or '').strip().lower()
    return (
        classification in _OPERATOR_CLASSIFICATIONS
        and outcome in {'allowed', 'routed'}
    ) or (
        classification == 'operator_held_dependency'
        and reason == 'validated_operator_held'
    )


def _operator_blocker_shape(item: Item) -> bool:
    """Typed negative fence: ordinary team-doable blockers never qualify."""
    from tasks.services.workflow_service import WorkflowService

    tags = {
        value.strip().lower()
        for value in str(item.tags or '').split(',')
        if value.strip()
    }
    lane = WorkflowService._canonical_team_name(
        item.assignment_group or ''
    )
    return (
        lane in _OPERATOR_LANES
        and 'blocker' in tags
        and 'decision-required' in tags
    )


def _is_active(item: Item) -> bool:
    from tasks.services.workflow_service import WorkflowService

    return (
        item.resolution_id is None
        and not item.is_archived
        and not WorkflowService._status_is_terminal(item.status)
    )


def _operator_roots_for(surfaced: Item, organization_id: int):
    """Return runnable operator-only leaves below one digest-listed item."""
    from tasks.services.workflow_service import WorkflowService

    found = []

    def walk(node, lineage, typed_anchor, visiting):
        if (
            node.pk in visiting
            or node.project.org_id != organization_id
            or not _is_active(node)
        ):
            return
        visiting = visiting | {node.pk}
        lineage = lineage + [node]
        typed_anchor = typed_anchor or _gate_proves_operator_only(node)
        blockers = sorted(
            WorkflowService._active_blockers_for(node),
            key=lambda value: value.pk,
        )
        if blockers:
            for blocker in blockers:
                walk(blocker, lineage, typed_anchor, visiting)
            return
        if not typed_anchor or not _operator_blocker_shape(node):
            return
        if not WorkflowService._has_active_blocked_descendants(node):
            return
        deadlines = [value.due_date for value in lineage if value.due_date]
        found.append({
            'root': node,
            'lineage_ids': [value.pk for value in lineage],
            'deadline_at': min(deadlines) if deadlines else None,
        })

    walk(surfaced, [], False, set())
    return found


def _set_marker_inactive(marker: OperatorWaitMarker, reason: str):
    if marker.state != 'active':
        return False
    marker.state = 'inactive'
    marker.invalidated_at = timezone.now()
    marker.invalidated_reason = reason[:80]
    marker.generation += 1
    marker.save(update_fields=[
        'state', 'invalidated_at', 'invalidated_reason', 'generation',
        'updated_at',
    ])
    return True


@transaction.atomic
def invalidate_markers_for_item(item_id: int, reason='evidence_mutation') -> int:
    """Invalidate markers whose proof lineage contains the changed item."""
    changed = 0
    for marker in OperatorWaitMarker.objects.select_for_update().filter(
        state='active',
    ).iterator():
        lineage = {int(value) for value in (marker.lineage_item_ids or [])}
        if int(item_id) not in lineage and int(item_id) not in {
            marker.item_id, marker.surfaced_item_id,
        }:
            continue
        changed += int(_set_marker_inactive(marker, reason))
    return changed


@transaction.atomic
def invalidate_markers_for_digest(digest_item_id: int, reason='digest_withdrawn') -> int:
    changed = 0
    for marker in OperatorWaitMarker.objects.select_for_update().filter(
        digest_item_id=digest_item_id,
        state='active',
    ).iterator():
        changed += int(_set_marker_inactive(marker, reason))
    return changed


@transaction.atomic
def _withdraw_digest(digest_item: Item, source_comment=None):
    digest_item = Item.objects.select_for_update().get(pk=digest_item.pk)
    state, _ = OperatorWaitDigestState.objects.select_for_update().get_or_create(
        digest_item=digest_item,
    )
    state.source_comment = source_comment
    if source_comment is not None:
        state.source_comment_order = max(
            state.source_comment_order,
            source_comment.pk,
        )
        state.source_revision = _revision(source_comment)
    state.source_message_id = ''
    state.state = 'withdrawn'
    state.save(update_fields=[
        'source_comment', 'source_comment_order', 'source_revision',
        'source_message_id', 'state', 'updated_at',
    ])
    invalidate_markers_for_digest(digest_item.pk, 'digest_withdrawn')


@transaction.atomic
def reconcile_operator_wait_markers(comment: Comment | None = None):
    """Converge markers from the newest durable EVOL-36 acknowledgement."""
    digest_item = (
        comment.item if comment is not None
        else Item.objects.filter(item_key=DIGEST_ITEM_KEY).first()
    )
    if digest_item is None or digest_item.item_key != DIGEST_ITEM_KEY:
        return {'activated': 0, 'invalidated': 0, 'skipped': 'digest_missing'}

    # The digest item exists before the first cursor/marker row, so it is the
    # stable serialization fence shared by request-side signals and the sweep.
    digest_item = Item.objects.select_for_update().get(pk=digest_item.pk)
    state = OperatorWaitDigestState.objects.select_for_update().filter(
        digest_item=digest_item,
    ).first()
    if (
        comment is not None
        and state is not None
        and state.source_comment_order > comment.pk
    ):
        return {
            'activated': 0,
            'invalidated': 0,
            'skipped': 'stale_digest_edit',
        }
    if comment is None:
        if state is not None:
            if state.state != 'active' or not state.source_comment_id:
                return {'activated': 0, 'invalidated': 0, 'skipped': 'digest_withdrawn'}
            comment = Comment.objects.filter(pk=state.source_comment_id).first()
            if comment is None:
                _withdraw_digest(digest_item)
                return {'activated': 0, 'invalidated': 0, 'skipped': 'digest_deleted'}
        else:
            for candidate in digest_item.comments.order_by('-created_at', '-pk'):
                if not _DIGEST_HEADING.search(candidate.content or ''):
                    continue
                if not _authorized_digest_author(candidate):
                    continue
                comment = candidate
                break
            if comment is None:
                return {'activated': 0, 'invalidated': 0, 'skipped': 'ack_missing'}

    if not _authorized_digest_author(comment):
        return {'activated': 0, 'invalidated': 0, 'skipped': 'unauthorized_ack'}
    payload = _digest_payload(comment)
    if payload is None:
        if state is None or state.source_comment_id == comment.pk:
            _withdraw_digest(digest_item, comment)
        return {'activated': 0, 'invalidated': 0, 'skipped': 'ack_invalid'}

    state, _ = OperatorWaitDigestState.objects.update_or_create(
        digest_item=digest_item,
        defaults={
            'source_comment': comment,
            'source_comment_order': comment.pk,
            'source_revision': payload['revision'],
            'source_message_id': payload['message_id'],
            'state': 'active',
        },
    )

    candidates = {}
    surfaced_items = {
        item.item_key: item
        for item in Item.objects.filter(
            item_key__in=payload['keys'],
        )
    }
    for key in payload['keys']:
        surfaced = surfaced_items.get(key)
        if surfaced is None:
            continue
        # The global digest is only the acknowledgement envelope. Each named
        # item remains governed by its own immutable project organization, so
        # the author must also hold exact-org Admin Ops authority there.
        if not _authorized_digest_author(comment, authority_item=surfaced):
            continue
        organization_id = surfaced.project.org_id
        for candidate in _operator_roots_for(surfaced, organization_id):
            root = candidate['root']
            candidates.setdefault(root.pk, {
                **candidate,
                'surfaced': surfaced,
            })

    invalidated = activated = 0
    active = OperatorWaitMarker.objects.select_for_update().filter(
        digest_item=digest_item,
        state='active',
    )
    for marker in active.iterator():
        if marker.source_revision == payload['revision'] and marker.item_id in candidates:
            continue
        invalidated += int(_set_marker_inactive(marker, 'digest_replaced'))

    now = timezone.now()
    for root_id, candidate in candidates.items():
        marker = OperatorWaitMarker.objects.select_for_update().filter(
            item_id=root_id,
        ).first()
        if marker is not None and marker.source_revision == payload['revision']:
            # An evidence/blocker mutation tombstones this exact digest proof.
            if marker.state != 'active':
                continue
            continue
        defaults = {
            'surfaced_item': candidate['surfaced'],
            'digest_item': digest_item,
            'digest_comment': comment,
            'source_revision': payload['revision'],
            'source_message_id': payload['message_id'],
            'lineage_item_ids': candidate['lineage_ids'],
            'deadline_at': candidate['deadline_at'],
            'state': 'active',
            'invalidated_at': None,
            'invalidated_reason': '',
        }
        if marker is None:
            OperatorWaitMarker.objects.create(item_id=root_id, **defaults)
        else:
            for field, value in defaults.items():
                setattr(marker, field, value)
            marker.generation += 1
            marker.activated_at = now
            marker.save(update_fields=[
                *defaults.keys(), 'generation', 'activated_at', 'updated_at',
            ])
        activated += 1

    # Deadline passage is an independent re-arm event even if no row changed.
    for marker in OperatorWaitMarker.objects.select_for_update().filter(
        digest_item=digest_item,
        state='active',
        deadline_at__isnull=False,
        deadline_at__lte=now,
    ).iterator():
        invalidated += int(_set_marker_inactive(marker, 'deadline_event'))

    return {'activated': activated, 'invalidated': invalidated, 'candidates': len(candidates)}


def handle_digest_comment_saved(comment: Comment):
    if comment.item.item_key == DIGEST_ITEM_KEY:
        return reconcile_operator_wait_markers(comment)
    if (comment.author_email or '').strip().lower() != 'system@pulse':
        return invalidate_markers_for_item(comment.item_id, 'evidence_mutation')
    return 0


@transaction.atomic
def handle_comment_deleted(comment: Comment):
    if comment.item.item_key == DIGEST_ITEM_KEY:
        digest_item = Item.objects.select_for_update().get(pk=comment.item_id)
        state = OperatorWaitDigestState.objects.select_for_update().filter(
            digest_item_id=comment.item_id,
        ).first()
        # SET_NULL foreign-key actions may clear source_comment_id before the
        # post_delete signal runs.  The persisted comment order is the durable
        # identity fence; revision equality alone is unsafe because two digest
        # comments may have identical content.
        if state and (
            state.source_comment_id == comment.pk
            or state.source_comment_order == comment.pk
            or (
                not state.source_comment_order
                and state.source_revision == _revision(comment)
            )
        ):
            _withdraw_digest(digest_item)
        return
    if (comment.author_email or '').strip().lower() != 'system@pulse':
        invalidate_markers_for_item(comment.item_id, 'evidence_mutation')


def _normalize_fingerprint(fingerprint) -> str:
    value = str(fingerprint or '').strip()
    if not value:
        raise ValueError('evidence_fingerprint_required')
    if len(value) > 128:
        raise ValueError('evidence_fingerprint_too_long')
    return value


def _active_marker_for_item(item_id: int) -> OperatorWaitMarker | None:
    return (
        OperatorWaitMarker.objects.select_for_update()
        .filter(item_id=item_id, state='active')
        .filter(Q(deadline_at__isnull=True) | Q(deadline_at__gt=timezone.now()))
        .first()
    )


def operator_wait_state_for_item(item_id: int) -> dict | None:
    """Read-only projection agents use before any external evidence probe."""
    marker = (
        OperatorWaitMarker.objects.filter(item_id=item_id)
        .order_by('-generation', '-pk')
        .first()
    )
    if marker is None:
        return None
    live = (
        marker.state == 'active'
        and (
            marker.deadline_at is None
            or marker.deadline_at > timezone.now()
        )
    )
    return {
        'item_id': marker.item_id,
        'state': marker.state,
        'live': live,
        'generation': marker.generation,
        'evidence_fingerprint': marker.evidence_fingerprint or '',
        'evidence_source': marker.evidence_source or '',
        'evidence_checklist': marker.evidence_checklist or {},
        'evidence_recorded_at': (
            marker.evidence_recorded_at.isoformat()
            if marker.evidence_recorded_at else None
        ),
        'evidence_unchanged_probes': marker.evidence_unchanged_probes,
        'should_probe_external': bool(
            live and not (marker.evidence_fingerprint or '').strip()
        ),
        'deadline_at': (
            marker.deadline_at.isoformat() if marker.deadline_at else None
        ),
        'invalidated_reason': marker.invalidated_reason or '',
    }


def _queue_verification_once(marker: OperatorWaitMarker, *, fingerprint: str, source: str):
    """Queue exactly one verification comment per marker generation."""
    if marker.verification_queued_generation >= marker.generation:
        return False
    from tasks.services.mechanical_comments import (
        MechanicalCommentFactory,
        deterministic_event_id,
    )

    item = Item.objects.get(pk=marker.item_id)
    MechanicalCommentFactory.create(
        item=item,
        origin_kind='WORKFLOW_LIFECYCLE',
        origin_event='operator_wait.evidence_reactivated',
        template_version=1,
        structured_payload={
            'fingerprint': fingerprint,
            'source': source or 'event',
            'generation': int(marker.generation),
        },
        origin_event_id=deterministic_event_id(
            'operator_wait.evidence_reactivated',
            item,
            marker.generation,
            fingerprint,
        ),
    )
    marker.verification_queued_generation = marker.generation
    marker.save(update_fields=['verification_queued_generation', 'updated_at'])
    return True


@transaction.atomic
def record_operator_evidence_probe(
    item_id: int,
    fingerprint,
    *,
    checklist=None,
    source: str = '',
) -> dict:
    """First complete probe records fingerprint; unchanged probes are no-ops.

    Returns a stable contract for heartbeat/agent callers:
    - recorded: first fingerprint on an active marker (still parked)
    - unchanged: same fingerprint (zero external re-probe warranted)
    - reactivated: fingerprint changed → marker tombstoned once + one verification
    - not_waiting: no live operator_wait marker
    """
    fingerprint = _normalize_fingerprint(fingerprint)
    source = (source or '').strip()[:64]
    checklist = checklist if isinstance(checklist, dict) else {}

    marker = _active_marker_for_item(item_id)
    if marker is None:
        return {
            'outcome': 'not_waiting',
            'reactivated': False,
            'should_probe_external': True,
            'verification_queued': False,
        }

    stored = (marker.evidence_fingerprint or '').strip()
    if not stored:
        marker.evidence_fingerprint = fingerprint
        marker.evidence_source = source
        marker.evidence_checklist = checklist
        marker.evidence_recorded_at = timezone.now()
        marker.evidence_unchanged_probes = 0
        marker.save(update_fields=[
            'evidence_fingerprint', 'evidence_source', 'evidence_checklist',
            'evidence_recorded_at', 'evidence_unchanged_probes', 'updated_at',
        ])
        return {
            'outcome': 'recorded',
            'reactivated': False,
            'should_probe_external': False,
            'verification_queued': False,
            'generation': marker.generation,
            'evidence_fingerprint': fingerprint,
            'evidence_unchanged_probes': 0,
        }

    if stored == fingerprint:
        marker.evidence_unchanged_probes = int(marker.evidence_unchanged_probes or 0) + 1
        if source and not marker.evidence_source:
            marker.evidence_source = source
        if checklist and not marker.evidence_checklist:
            marker.evidence_checklist = checklist
        marker.save(update_fields=[
            'evidence_unchanged_probes', 'evidence_source', 'evidence_checklist',
            'updated_at',
        ])
        return {
            'outcome': 'unchanged',
            'reactivated': False,
            'should_probe_external': False,
            'verification_queued': False,
            'generation': marker.generation,
            'evidence_fingerprint': stored,
            'evidence_unchanged_probes': marker.evidence_unchanged_probes,
        }

    # Relevant evidence mutation: tombstone this digest proof exactly once.
    _set_marker_inactive(marker, 'evidence_fingerprint_changed')
    marker.refresh_from_db()
    queued = _queue_verification_once(
        marker, fingerprint=fingerprint, source=source or 'evidence_probe',
    )
    return {
        'outcome': 'reactivated',
        'reactivated': True,
        'should_probe_external': True,
        'verification_queued': queued,
        'generation': marker.generation,
        'evidence_fingerprint': fingerprint,
        'previous_fingerprint': stored,
        'evidence_unchanged_probes': marker.evidence_unchanged_probes,
    }


@transaction.atomic
def apply_operator_evidence_event(
    item_id: int,
    fingerprint,
    *,
    source: str = '',
) -> dict:
    """Canonical evidence-event path (Drive mutation adapter, agent report, etc.).

    Unrelated/identical fingerprints do not clear the marker. A changed
    fingerprint reactivates exactly once for the current digest revision.
    """
    return record_operator_evidence_probe(
        item_id,
        fingerprint,
        source=source or 'evidence_event',
    )
