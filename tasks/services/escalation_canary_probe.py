"""PLAT-9682: typed-escalation channel health probe (prevention for PLAT-9678).

PLAT-9678's retro-finding: the Raise-to-Admin-Ops typed-escalation path failed
3x (``escalation_hold_commit_failed``, 503) while every conventional health
signal was green — nothing exercised the escalation-hold COMMIT itself, so a
broken channel was only discovered when a real, sanctioned escalation rode it
and bounced. A channel that only fails under real traffic is unmonitored.

This probe closes that blind spot with a canary that exercises the REAL
forced-hold commit machinery — the exact code path that raises
``escalation_hold_commit_failed`` (``EscalationRoutingService._execute_forced_hold``:
fresh transaction, typed-evidence envelope preparation/validation, digest
chain, append-only disposition/receipt writes, external-wait park) — via the
``org_local_operator_destination_unresolved`` preserve-without-routing branch,
so the canary NEVER creates an admin-ops blocker child and NEVER pages a
human. Synthetic ask, zero operator noise, full record-layer fidelity.

Named fallback channel (ren's PLAT-9682 requirement — sanctioned escalation
never rides an unmonitored path): if this probe fires its commit-failure
alert, sanctioned Raise-to-Admin-Ops escalations ride (1) a direct Connect DM
to the current admin-ops owner carrying the typed-evidence payload, and (2) a
comment on PLAT-9682 recording the failed commit — until this probe reports
green again. The typed path is NOT retried while broken.

Scheduled by the Pulse recurring engine / cron via the management command
``probe_escalation_channel``. Self-provisioning: on first run the probe
creates its canary workflow (statuses open/blocked, one Raise-to-Admin-Ops
edge, no post-functions) and canary item inside the configured canary project
(``PULSE_ESCALATION_CANARY_PROJECT_ID``); subsequent runs reuse them.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

CANARY_TITLE = 'escalation-channel canary (PLAT-9682)'
CANARY_WORKFLOW_SLUG = 'escalation-canary-wf'
CANARY_ITEM_KEY_PREFIX = 'ESCC'
EVIDENCE_SCHEMA = 'shizuha.pulse.escalation-evidence.v1'
CANARY_REASON_CODE = 'org_local_operator_destination_unresolved'

ALERT_SOURCE = 'pulse-escalation-canary'
FALLBACK_INSTRUCTION = (
    'NAMED FALLBACK while the typed channel is broken: sanctioned '
    'Raise-to-Admin-Ops escalations ride a direct Connect DM to the current '
    'admin-ops owner with the typed-evidence payload, plus a comment on '
    'PLAT-9682 recording the failed commit. Do NOT retry the typed path '
    'until this probe reports green.'
)


def _canary_actor():
    from django.contrib.auth import get_user_model
    user_model = get_user_model()
    email = str(
        getattr(settings, 'PULSE_ESCALATION_CANARY_ACTOR_EMAIL', '')
        or 'shizuha@agents.shizuha.io'
    ).strip().lower()
    user = user_model.objects.filter(email__iexact=email).first()
    if user is None:
        user = user_model.objects.create_user(
            username=email.split('@')[0][:150] or 'escalation-canary',
            email=email,
            is_staff=True,
        )
    return user


def _ensure_canary_workflow(project):
    from tasks.models import Status, Workflow, WorkflowStatus, WorkflowTransition

    workflow = Workflow.objects.filter(
        slug=CANARY_WORKFLOW_SLUG, is_active=True,
    ).first()
    if workflow is not None:
        return workflow
    open_status, _ = Status.objects.get_or_create(
        slug='open', defaults={'name': 'Open', 'category': 'todo'},
    )
    blocked_status, _ = Status.objects.get_or_create(
        slug='blocked', defaults={'name': 'Blocked', 'category': 'in_progress'},
    )
    workflow = Workflow.objects.create(
        name='Escalation channel canary',
        slug=CANARY_WORKFLOW_SLUG,
        initial_status=open_status,
        is_active=True,
    )
    WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=0)
    WorkflowStatus.objects.create(workflow=workflow, status=blocked_status, position=1)
    # No post-functions: the canary calls the forced-hold commit directly and
    # the preserve-without-routing branch synthesizes nothing for humans.
    WorkflowTransition.objects.create(
        workflow=workflow,
        name='Raise to Admin Ops (canary)',
        from_status='open',
        to_status='blocked',
    )
    return workflow


def _ensure_canary_item(project, actor):
    from tasks.models import Item

    item = Item.objects.filter(
        project=project, title=CANARY_TITLE,
    ).exclude(status__in=('done', 'cancelled')).first()
    if item is not None:
        return item
    workflow = _ensure_canary_workflow(project)
    return Item.objects.create(
        project=project,
        item_key=f'{CANARY_ITEM_KEY_PREFIX}-0',
        mode='task',
        title=CANARY_TITLE,
        description=(
            'Canary carrier for the typed-escalation channel health probe '
            '(PLAT-9682). Synthetic; no human action ever required. Owned by '
            'the probe; may park in waiting_external between runs.'
        ),
        workflow_name=workflow.slug,
        status='open',
        workflow_status='open',
        assignment_group='engineering',
        assignee_id=actor.id,
        assignee_email=actor.email,
        created_by_id=actor.id,
        created_by_email=actor.email,
    )


def _build_envelope(item) -> dict[str, Any]:
    from tasks.models import EscalationRoutingDisposition, EscalationRoutingReceipt
    from django.db.models import Max

    latest = EscalationRoutingDisposition.objects.filter(item=item).aggregate(
        value=Max('generation')
    )['value'] or 0
    receipt_gen = EscalationRoutingReceipt.objects.filter(item=item).aggregate(
        value=Max('source_generation')
    )['value'] or 0
    return {
        'schema': EVIDENCE_SCHEMA,
        'request_id': str(uuid.uuid4()),
        'expected_generation': max(latest, receipt_gen),
        'dependency': {
            'evidence_class': 'human_held_source_evidence',
            'requested_kind': 'artifact',
            'requested_subject': (
                'escalation-channel canary — synthetic ask; no operator '
                'action is required'
            ),
            'holder_type': 'operator',
            'holder_ref': 'escalation-canary-probe@pulse.internal',
            'acceptance_check': (
                'canary receipt committed and readable; no human action'
            ),
            'source_refs': [{'kind': 'task', 'id': str(item.item_key)}],
        },
        # PLS-527 TOP_KEYS requires the key present; a pure human-held ask
        # carries technical_action=None.
        'technical_action': None,
    }


def _pick_transition(item, actor):
    """First executable WorkflowTransition from the item's current status.

    ``WorkflowService.get_available_transitions`` returns serialized dicts;
    the hold commit needs the model instance.
    """
    from tasks.models import WorkflowTransition
    from tasks.services.workflow_service import WorkflowService

    candidates = WorkflowTransition.objects.filter(
        workflow__slug=item.workflow_name, from_status=item.status,
    )
    for transition in candidates:
        try:
            if WorkflowService._check_transition_conditions(transition, item, actor):
                return transition
        except Exception:
            continue
    return None


def _fire_alert(*, severity: str, source_id: str, title: str, payload: dict,
                description: str) -> None:
    from tasks.services.deduplication import AlertDeduplicationService

    AlertDeduplicationService.submit_alert({
        'item_type': 'alert.escalation_channel',
        'title': title,
        'source': ALERT_SOURCE,
        'source_id': source_id,
        'severity': severity,
        'labels': {'alertname': 'EscalationChannelCanary', **{
            k: str(v) for k, v in payload.items() if k in (
                'error_class', 'phase', 'reason',
            )
        }},
        'payload': {**payload, 'generated_at': timezone.now().isoformat()},
        'description': description,
    })


def run_escalation_canary() -> dict:
    """Exercise the typed-escalation hold-commit path end to end.

    Returns ``{"outcome": "green" | "commit_failed" | "unconfigured"
    | "no_transition", "alert_fired": bool, ...}``.
    """
    from django.db.models import Max

    from tasks.models import (
        EscalationRoutingDisposition, EscalationRoutingReceipt, Project,
    )
    from tasks.services import escalation_routing
    from tasks.services.external_wait import release_human_wait

    project_id = getattr(settings, 'PULSE_ESCALATION_CANARY_PROJECT_ID', None)
    if not project_id:
        payload = {'phase': 'config', 'reason': 'canary project not configured'}
        _fire_alert(
            severity='warning',
            source_id='escalation-canary:unconfigured',
            title='Escalation-channel canary is not configured — typed '
                  'escalation path unmonitored (PLAT-9682)',
            payload=payload,
            description=(
                'PULSE_ESCALATION_CANARY_PROJECT_ID is unset, so the '
                'typed-escalation channel probe cannot run and the channel '
                'that failed 3x under PLAT-9678 is unmonitored again. ' +
                FALLBACK_INSTRUCTION
            ),
        )
        return {'outcome': 'unconfigured', 'alert_fired': True, **payload}

    started = time.monotonic()
    actor = _canary_actor()
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        payload = {'phase': 'config', 'reason': f'canary project {project_id} missing'}
        _fire_alert(
            severity='warning',
            source_id='escalation-canary:unconfigured',
            title='Escalation-channel canary project missing — typed '
                  'escalation path unmonitored (PLAT-9682)',
            payload=payload,
            description=(
                'The configured canary project no longer exists. ' +
                FALLBACK_INSTRUCTION
            ),
        )
        return {'outcome': 'unconfigured', 'alert_fired': True, **payload}

    item = _ensure_canary_item(project, actor)

    # A legacy row parked before waiting_external was retired still needs
    # a typed release so this run can start from open. New runs stay open.
    item.refresh_from_db()
    if item.status == 'waiting_external':
        try:
            release_human_wait(item, user=actor)
            item.refresh_from_db()
        except Exception:
            logger.exception('escalation canary: wait release failed')

    transition = _pick_transition(item, actor)
    if transition is None:
        payload = {'phase': 'transition', 'reason': 'no available transition'}
        _fire_alert(
            severity='warning',
            source_id='escalation-canary:no-transition',
            title='Escalation-channel canary has no executable transition '
                  '(PLAT-9682)',
            payload=payload,
            description=(
                'The canary item cannot start the Raise-to-Admin-Ops canary '
                'commit; the channel commit leg is unexercised this run. ' +
                FALLBACK_INSTRUCTION
            ),
        )
        return {
            'outcome': 'no_transition', 'alert_fired': True,
            'canary_item': item.item_key, **payload,
        }

    envelope = _build_envelope(item)
    screen_data = {
        'escalation_evidence': envelope,
        'comment': 'escalation-channel canary (PLAT-9682) — synthetic, no '
                   'human action required',
    }
    try:
        escalation_routing.EscalationRoutingService._execute_forced_hold(
            item.pk, transition, user=actor, screen_data=screen_data,
            reason_code=CANARY_REASON_CODE,
        )
    except Exception as exc:
        logger.exception('escalation canary: hold commit FAILED')
        payload = {
            'phase': 'commit',
            'error_class': type(exc).__name__,
            'detail': str(exc)[:500],
            'canary_item': item.item_key,
        }
        _fire_alert(
            severity='critical',
            source_id=f'escalation-canary:{timezone.now().date().isoformat()}',
            title='Typed-escalation channel BROKEN: canary hold commit '
                  'failed (escalation_hold_commit_failed class) (PLAT-9682)',
            payload=payload,
            description=(
                'The canary exercising the REAL escalation-hold commit '
                'machinery failed — the same path raised '
                '`escalation_hold_commit_failed` 3x under PLAT-9678 while '
                'health was green. Every sanctioned escalation will bounce '
                'until fixed. ' + FALLBACK_INSTRUCTION
            ),
        )
        return {
            'outcome': 'commit_failed', 'alert_fired': True,
            'duration_ms': int((time.monotonic() - started) * 1000),
            **payload,
        }

    item.refresh_from_db()
    receipt = EscalationRoutingReceipt.objects.filter(item=item).order_by('-id').first()
    disposition_gen = EscalationRoutingDisposition.objects.filter(item=item).aggregate(
        value=Max('generation')
    )['value'] or 0
    result = {
        'outcome': 'green',
        'alert_fired': False,
        'canary_item': item.item_key,
        'receipt_pk': receipt.pk if receipt else None,
        'request_id': envelope['request_id'],
        'disposition_generation': disposition_gen,
        'item_status': item.status,
        'duration_ms': int((time.monotonic() - started) * 1000),
    }
    if receipt is None:
        # The commit did not raise but left no receipt — the record layer is
        # lying. Treat as a commit failure for alerting purposes.
        result['outcome'] = 'commit_failed'
        result['phase'] = 'verify'
        result['reason'] = 'no receipt row after successful hold commit'
        _fire_alert(
            severity='critical',
            source_id=f'escalation-canary:{timezone.now().date().isoformat()}',
            title='Typed-escalation channel BROKEN: canary commit left no '
                  'receipt (PLAT-9682)',
            payload={k: result.get(k) for k in ('phase', 'reason', 'canary_item')},
            description=(
                'The canary hold commit returned without error but no '
                'EscalationRoutingReceipt row exists — the append-only '
                'record layer is not trustworthy. ' + FALLBACK_INSTRUCTION
            ),
        )
        result['alert_fired'] = True
    return result
