"""Defer the whole graph transaction, never a terminal root's required restores."""
import hashlib
import json
import logging
import os
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.forms.models import model_to_dict
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from tasks.models import DeferredWorkflowCommand, Item, ItemLink, WorkflowTransition
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

logger = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()


def _graph(item, *, lock=False):
    """Pin transitive dependents and all their other blockers before mutation."""
    descendants = set()
    frontier = {item.pk}
    while frontier:
        outgoing = ItemLink.objects.filter(
            Q(source_item_id__in=frontier, link_type='blocks') |
            Q(target_item_id__in=frontier, link_type__in=['is_blocked_by', 'blocked_by']))
        found = {
            edge.target_item_id if edge.link_type == 'blocks' else edge.source_item_id
            for edge in outgoing
        } - descendants - {item.pk}
        descendants.update(found)
        frontier = found
    edges = ItemLink.objects.filter(
        Q(source_item_id__in=descendants, link_type__in=['is_blocked_by', 'blocked_by']) |
        Q(target_item_id__in=descendants, link_type='blocks'))
    links = list(edges.order_by('pk').values('id', 'source_item_id', 'target_item_id', 'link_type'))
    ids = {item.pk} | descendants
    for edge in links:
        ids.update((edge['source_item_id'], edge['target_item_id']))
    rows = Item.objects.filter(pk__in=ids).order_by('pk')
    if lock:
        # Lock Items in one order before any root lock. A concurrent FK writer
        # may insert, but its FK check/commit waits on these locks; the existing
        # deferred graph invariant still decides whether its commit is valid.
        list(rows.select_for_update().values_list('pk', flat=True))
        list(edges.select_for_update().values_list('pk', flat=True))
    states = list(rows.values('pk', 'state_generation', 'project_id', 'status',
                             'workflow_name', 'is_archived', 'title', 'description',
                             'current_state', 'metadata'))
    for state in states:
        # Validator heartbeat timestamps are observations, not task intent.
        metadata = dict(state['metadata'] or {})
        metadata.pop('mechanism_liveness', None)
        state['metadata'] = metadata
    digest = _digest({'items': states, 'links': links})
    if lock and digest != _graph(item):
        # Do not accept an edge inserted while the graph locks were acquired.
        raise WorkflowValidationError('Dependency graph changed while acquiring its locks.',
                                      code='deferred_transition_stale')
    return digest


def _transition(payload):
    pk = payload.get('transition_id')
    return WorkflowTransition.objects.select_related('workflow').filter(pk=pk).first() if pk else None


def _definition_digest(payload):
    transition = _transition(payload)
    if transition is None:
        return ''
    return _digest({'transition': model_to_dict(transition),
                    'workflow': model_to_dict(transition.workflow)})


def _screen(payload):
    screen = dict(payload.get('screen_data') or {})
    for key in ('resolution', 'comment'):
        if payload.get(key) and (key != 'comment' or not screen.get(key)):
            screen[key] = payload[key]
    return screen


def operation_payload(command, item):
    return {
        'operation_id': str(command.pk), 'operation_state': command.state,
        'pending': command.state == 'pending',
        'committed': command.state == 'succeeded',
        'id': item.pk, 'item_key': item.item_key, 'title': item.title,
        'status': item.status,
        'operation_url': f'/pulse/api/items/{item.pk}/workflow-operations/?operation_id={command.pk}',
        'message': ('Transition queued; task status is unchanged until the graph transaction commits.'
                    if command.state == 'pending' else command.result.get('detail', command.state)),
        'result': command.result,
        **({'error': command.result.get('detail', command.state),
            'code': command.result.get('code', command.state)}
           if command.state in {'failed', 'stale'} else {}),
    }


def maybe_enqueue(item, actor, *, kind, payload, external_token=None):
    """Called only after the ordinary endpoint's object permission check."""
    target = 'completed' if kind == 'complete' else payload.get('to_status')
    explicit = _transition(payload)
    if explicit:
        target = explicit.to_status
    threshold = int(os.environ.get('PULSE_DEPENDENCY_RELEASE_INLINE_MAX', '10'))
    if (threshold < 0 or not WorkflowService._status_releases_blocks(target)
            or len(WorkflowService._blocked_dependent_ids(item)) <= threshold):
        return None
    # Keep only validated screen input; auth headers/tokens are never serialized.
    payload = json.loads(json.dumps(payload, default=str))
    if len(json.dumps(payload).encode()) > 65536:
        raise WorkflowValidationError('Deferred transition input exceeds 64 KiB.',
                                      code='deferred_transition_input_too_large')
    with transaction.atomic():
        _graph(item, lock=True)
        item = Item.objects.select_related('project').get(pk=item.pk)
        if WorkflowService._status_releases_blocks(item.status):
            return None
        if kind != 'complete':
            chosen = WorkflowService.validate_transition(
                item, target, user=actor, screen_data=_screen(payload),
                transition_id=payload.get('transition_id'))
            if isinstance(chosen, WorkflowTransition):
                payload['transition_id'] = str(chosen.pk)
            payload['to_status'] = target
        graph = _graph(item, lock=True)
        definition = _definition_digest(payload)
        digest = _digest({'actor': [actor.id, actor.email], 'kind': kind,
                          'token_id': str(external_token.pk) if external_token else None,
                          'payload': payload, 'generation': item.state_generation,
                          'graph': graph, 'definition': definition})
        command, created = DeferredWorkflowCommand.objects.get_or_create(
            item=item, request_digest=digest, state='pending',
            defaults={'actor_id': actor.id, 'actor_email': actor.email,
                      'organization_id': item.project.org_id if item.project else None,
                      'external_token': external_token, 'kind': kind, 'payload': payload,
                      'source_generation': item.state_generation, 'graph_digest': graph,
                      'transition_digest': definition})
        if created:
            from tasks.tasks import execute_deferred_workflow_command
            transaction.on_commit(
                lambda: execute_deferred_workflow_command.delay(str(command.pk)), robust=True)
        return operation_payload(command, item)


def _fresh_actor(command):
    """Use the same ID/Admin authorities as JWT authentication, without a cache."""
    from tasks.authentication import FederatedUser
    from tasks.permissions import _email_variants
    from tasks.user_service import UserService
    from tasks.services.admin_service import admin_service
    identity = UserService().get_user_by_id(command.actor_id)
    if (not identity or str(identity.get('id')) != str(command.actor_id)
            or identity.get('is_active') is not True
            or str(identity.get('email') or '').lower() not in _email_variants(command.actor_email)):
        raise PermissionDenied('Deferred transition actor is inactive or identity cannot be verified.')
    memberships = {}
    if command.organization_id is not None and command.kind != 'external':
        # Uncached existing membership-check route; unlike /memberships/ this
        # endpoint does not create a personal workspace as a read side effect.
        membership = admin_service._make_request(
            'GET', f'/organizations/{command.organization_id}/members/{command.actor_id}/',
            params={'email': identity['email']})
        if not membership or not membership.get('is_member'):
            raise PermissionDenied('Deferred transition organization membership is no longer valid.')
        memberships[command.organization_id] = membership.get('role', '')
    if command.kind == 'external':
        # External MCP is authorized by current token/client project grants,
        # not Admin membership. Preserve the inline gateway authority boundary.
        # The delegated actor is never platform staff.
        identity = {**identity, 'is_staff': False, 'is_superuser': False}
        memberships = {}
    return FederatedUser(command.actor_id, email=identity['email'],
                         is_staff=identity.get('is_staff') is True,
                         is_superuser=identity.get('is_superuser') is True,
                         organization_memberships=memberships)


def _authorize(command, item, actor):
    from tasks.permissions import ItemProjectPermission
    from tasks.services.permissions import PermissionService
    if command.kind == 'external':
        from tasks.views import (_external_mcp_visible_items, _external_mcp_scopes,
                                 _external_mcp_enforce_write_policy)
        token = command.external_token
        if (token is None or not token.is_active() or token.client.owner_user_id != actor.id
                or token.client.owner_email.lower() != command.actor_email.lower()
                or 'transition.execute' not in _external_mcp_scopes(token)):
            raise PermissionDenied('Deferred external token authority is no longer valid.')
        _external_mcp_enforce_write_policy(token, 'pulse_execute_transition', command.payload)
        visible = _external_mcp_visible_items(token)
    else:
        visible = PermissionService.get_user_item_queryset(actor)
        if not ItemProjectPermission().has_object_permission(
                SimpleNamespace(user=actor, method='POST'), None, item):
            raise PermissionDenied('Deferred transition write permission was lost.')
    if not visible.filter(pk=item.pk).exists():
        raise PermissionDenied('Deferred transition item visibility was lost.')
    return visible


def execute(command_id):
    """The command row lock fences redelivery for the entire DB transaction.

    No wall-clock lease can expire under a still-running database writer. A
    worker/process loss rolls back the same transaction, leaving pending intent
    for redelivery; terminal failure is recorded only after rollback completes.
    """
    try:
        with transaction.atomic():
            command = DeferredWorkflowCommand.objects.select_for_update().get(pk=command_id)
            if command.state != 'pending':
                return {'operation_id': str(command.pk), 'state': command.state, **command.result}
            if command.item_id is None:
                raise WorkflowValidationError('Deferred item was deleted.', code='deferred_transition_stale')
            item = Item.objects.get(pk=command.item_id)
            graph = _graph(item, lock=True)
            item = Item.objects.select_related('project').get(pk=command.item_id)
            if (item.state_generation != command.source_generation or item.is_archived
                    or (item.project.org_id if item.project else None) != command.organization_id
                    or graph != command.graph_digest
                    or _definition_digest(command.payload) != command.transition_digest):
                raise WorkflowValidationError('Task, dependency graph or workflow changed while queued.',
                                              code='deferred_transition_stale')
            actor = _fresh_actor(command)
            visible = _authorize(command, item, actor)
            old_status = item.status
            if command.kind == 'complete':
                item.mark_completed()
                WorkflowService._reconcile_dependency_release(item, user=actor)
            else:
                payload = command.payload
                item = WorkflowService.execute_transition(
                    item, payload.get('to_status'), user=actor, screen_data=_screen(payload),
                    transition_id=payload.get('transition_id'), focus_queryset=visible)
                if command.kind == 'transition':
                    from .transition_finalization import finalize_transition
                    finalize_transition(item, old_status=old_status, user=actor,
                                        payload=payload, screen_data=_screen(payload),
                                        transition_id=payload.get('transition_id'))
                else:
                    from tasks.views import _external_mcp_add_provenance_comment
                    _external_mcp_add_provenance_comment(
                        item, command.external_token, 'pulse_execute_transition',
                        SimpleNamespace(headers={'Idempotency-Key': payload.get('_idempotency_key', str(command.pk))},
                                        data={'arguments': payload}),
                        f'Transitioned to `{item.status}` through workflow engine.')
            command.state = 'succeeded'
            command.result = {'status': item.status, 'detail': 'Workflow graph transaction committed.'}
            command.finished_at = timezone.now()
            command.save(update_fields=['state', 'result', 'finished_at'])
        return {'operation_id': str(command_id), 'state': 'succeeded', **command.result}
    except Exception as exc:
        # This block runs AFTER the failed mutation transaction has rolled back.
        logger.exception('Deferred workflow command %s failed', command_id)
        with transaction.atomic():
            command = DeferredWorkflowCommand.objects.select_for_update().get(pk=command_id)
            if command.state == 'pending':
                code = getattr(exc, 'code', '') or type(exc).__name__
                command.state = 'stale' if code == 'deferred_transition_stale' else 'failed'
                command.result = {'code': str(code), 'detail': str(exc)[:2000]}
                command.finished_at = timezone.now()
                command.save(update_fields=['state', 'result', 'finished_at'])
            return {'operation_id': str(command_id), 'state': command.state, **command.result}


def relay_pending(limit=64):
    """Recovery under the existing dependency sweep; event dispatch is primary."""
    from tasks.tasks import execute_deferred_workflow_command
    # Skip the row lock held by an active worker; never declare it dead by TTL.
    with transaction.atomic():
        ids = list(DeferredWorkflowCommand.objects.select_for_update(skip_locked=True)
                   .filter(state='pending').order_by('created_at').values_list('pk', flat=True)[:limit])
        for pk in ids:
            transaction.on_commit(lambda pk=pk: execute_deferred_workflow_command.delay(str(pk)), robust=True)
    return len(ids)
