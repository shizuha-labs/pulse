"""PLS-482 prepare -> Wiki CAS -> Pulse finalize/outbox publication saga."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

import jwt
import requests
from django.conf import settings
from django.db import IntegrityError, transaction

from ..models import (
    Item,
    PublicationAttempt,
    PublicationBinding,
    PublicationConsumedRound,
    PublicationOutbox,
)


_SHA_RE = re.compile(r'^[0-9a-f]{64}$')
_RESULT_RE = re.compile(r'^[a-z][a-z0-9_.-]{0,99}$')
_TERMINAL_HOLDS = {
    'artifact_authority_unavailable_hold', 'artifact_hold',
    'stale_publication_hold', 'carrier_conflict_hold', 'aborted',
}
_TERMINAL_SUCCESS = {'published', 'delivered'}


class PublicationInputError(Exception):
    pass


class PublicationWikiError(Exception):
    def __init__(self, code, detail, evidence=None):
        super().__init__(detail)
        self.code = code
        self.evidence = evidence or {}


class PublicationCarrierConflict(Exception):
    pass


def _item_has_marker(item, marker) -> bool:
    marker = str(marker or '').strip().lower()
    labels = getattr(item, 'labels', None) or {}
    if isinstance(labels, dict):
        label_values = {str(key).lower() for key, value in labels.items() if value is not False}
        label_values |= {str(value).lower() for value in labels.values() if isinstance(value, str)}
    elif isinstance(labels, (list, tuple, set)):
        label_values = {str(value).lower() for value in labels}
    else:
        label_values = {part.strip().lower() for part in str(labels).split(',') if part.strip()}
    tags = {
        part.strip().lower()
        for part in str(getattr(item, 'tags', '') or '').split(',')
        if part.strip()
    }
    return marker in (label_values | tags)


def is_publication_transition(transition, item=None) -> bool:
    for pf in (getattr(transition, 'post_functions', None) or []):
        if not isinstance(pf, dict) or pf.get('type') != 'publication_handoff':
            continue
        required_label = pf.get('required_label')
        if not required_label or (item is not None and _item_has_marker(item, required_label)):
            return True
    name = str(getattr(transition, 'name', '') or '').strip().lower()
    return bool(
        item is not None
        and _item_has_marker(item, 'publication-binding-required')
        and 'submit for' in name
        and 'review' in name
    )


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _digest(domain, value) -> str:
    return hashlib.sha256(domain.encode('utf-8') + b'\0' + _canonical(value)).hexdigest()


def _normalize_input(screen_data) -> dict:
    raw = (screen_data or {}).get('publication_binding')
    # §17.2.1: the caller supplies only the review/publication currency and the
    # expected Wiki tuples. Artifact authority is NEVER accepted from transition
    # screen data — the server classifies the artifact policy (wiki-only vs
    # artifact-bearing), so no artifact_record_id/version is present.
    expected = {
        'operation_key', 'review_round_id', 'publication_result', 'pages',
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise PublicationInputError(
            'publication_binding must contain exactly operation_key, review_round_id, '
            'publication_result, pages.'
        )
    operation_key = str(raw.get('operation_key') or '').strip()
    review_round_id = str(raw.get('review_round_id') or '').strip()
    publication_result = str(raw.get('publication_result') or '').strip()
    if not operation_key or len(operation_key) > 128:
        raise PublicationInputError('operation_key is required and limited to 128 characters.')
    if not review_round_id or len(review_round_id) > 128:
        raise PublicationInputError('review_round_id is required and limited to 128 characters.')
    if not _RESULT_RE.fullmatch(publication_result):
        raise PublicationInputError('publication_result must be a lowercase typed result.')

    pages = raw.get('pages')
    if not isinstance(pages, list) or not pages or len(pages) > 100:
        raise PublicationInputError('pages must be a non-empty ordered list of at most 100 entries.')
    normalized_pages = []
    seen_pages = set()
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or set(page) != {'page_id', 'version', 'anchors'}:
            raise PublicationInputError(f'pages[{index}] has an invalid shape.')
        try:
            page_id = str(uuid.UUID(str(page['page_id'])))
            version = int(page['version'])
        except (TypeError, ValueError):
            raise PublicationInputError(f'pages[{index}] has an invalid id/version.')
        if version < 1 or page_id in seen_pages:
            raise PublicationInputError(f'pages[{index}] has a duplicate id or invalid version.')
        seen_pages.add(page_id)
        anchors = page['anchors']
        if not isinstance(anchors, list) or not anchors:
            raise PublicationInputError(f'pages[{index}].anchors must be non-empty.')
        normalized_anchors = []
        seen_anchors = set()
        for anchor_index, anchor in enumerate(anchors):
            if not isinstance(anchor, dict) or set(anchor) != {'anchor', 'section_hash'}:
                raise PublicationInputError(f'pages[{index}].anchors[{anchor_index}] has an invalid shape.')
            name = str(anchor['anchor'] or '').strip()
            section_hash = str(anchor['section_hash'] or '').strip().lower()
            if not name or name in seen_anchors or not _SHA_RE.fullmatch(section_hash):
                raise PublicationInputError(f'pages[{index}].anchors[{anchor_index}] is invalid/duplicate.')
            seen_anchors.add(name)
            normalized_anchors.append({'anchor': name, 'section_hash': section_hash})
        normalized_pages.append({'page_id': page_id, 'version': version, 'anchors': normalized_anchors})
    return {
        'operation_key': operation_key,
        'review_round_id': review_round_id,
        'publication_result': publication_result,
        'pages': normalized_pages,
    }


def _mint_wiki_token(organization_id: int) -> str:
    signing_key = settings.SIMPLE_JWT.get('SIGNING_KEY') or settings.SECRET_KEY
    if not signing_key or 'django-insecure' in signing_key:
        raise PublicationWikiError('wiki_auth_unavailable', 'Wiki service signing key is unavailable.')
    # Supplemental P1 #1: Wiki's CAS endpoint accepts ONLY the configured
    # service principal and rejects unset/non-positive IDs, so minting with an
    # unconfigured principal would guarantee a cross-service 403. Fail closed
    # here, before any network call, with a typed error.
    service_user_id = int(getattr(settings, 'SHIZUHA_WIKI_SERVICE_USER_ID', 0) or 0)
    if service_user_id <= 0:
        raise PublicationWikiError(
            'wiki_auth_unavailable',
            'SHIZUHA_WIKI_SERVICE_USER_ID is not configured; refusing to mint an '
            'unprincipled Wiki service token.',
        )
    now = int(time.time())
    token = jwt.encode({
        'token_type': 'access',
        'exp': now + 300,
        'iat': now,
        'jti': uuid.uuid4().hex,
        'user_id': service_user_id,
        'email': 'pulse-publication@shizuha.com',
        'username': 'pulse-publication',
        'is_agent': True,
        'enabled_services': ['wiki'],
        'organization_memberships': {str(organization_id): 'member'},
        'aud': ['wiki'],
    }, signing_key, algorithm='HS256')
    return token if isinstance(token, str) else token.decode('utf-8')


def _wiki_snapshot(*, organization_id, payload) -> dict:
    url = getattr(
        settings, 'SHIZUHA_WIKI_API_URL', 'http://shizuha-wiki:8013/api'
    ).rstrip('/') + '/publication-snapshots/'
    try:
        response = requests.post(
            url,
            json={
                'operation_key': payload['operation_key'],
                'organization_id': organization_id,
                'publication_result': payload['publication_result'],
                'pages': payload['pages'],
            },
            headers={
                'Authorization': f'Bearer {_mint_wiki_token(organization_id)}',
                'Content-Type': 'application/json',
                'X-Organization-ID': str(organization_id),
            },
            timeout=(3.05, 12),
        )
    except (requests.RequestException, PublicationWikiError) as exc:
        if isinstance(exc, PublicationWikiError):
            raise
        raise PublicationWikiError('wiki_unavailable', f'Wiki snapshot service unavailable: {exc}')
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code in (200, 201):
        try:
            # Close the receipt before it can reach the UUID-backed attempt row.
            # Accept any UUID spelling the decoder understands, but normalize it
            # to the canonical textual form used by the request/binding digest.
            snapshot_id = str(uuid.UUID(str(body.get('publication_snapshot_id') or '')))
        except (AttributeError, TypeError, ValueError):
            raise PublicationWikiError(
                'wiki_invalid_response',
                'Wiki returned an invalid publication snapshot ID.',
            )
        if not _SHA_RE.fullmatch(str(body.get('snapshot_digest') or '')):
            raise PublicationWikiError('wiki_invalid_response', 'Wiki returned an invalid snapshot receipt.')
        body['publication_snapshot_id'] = snapshot_id
        return body
    code = str(body.get('error') or 'wiki_snapshot_failed')
    detail = str(body.get('detail') or f'Wiki snapshot failed with HTTP {response.status_code}.')
    raise PublicationWikiError(code, detail, {'current_pages': body.get('current_pages') or []})


def _classify_artifact_policy(transition) -> str:
    """Server-side artifact-policy classification (HLD v23 §17.1).

    The caller cannot choose the class and never supplies artifact authority via
    screen data. A transition whose ``publication_handoff`` post-function
    declares ``artifact_bearing: true`` is an artifact-bearing / cross-runtime
    handoff (requires a separately-implemented PLAT-4595 provider); everything
    else is a Wiki-only publication with an explicit null artifact authority.
    """
    for pf in (getattr(transition, 'post_functions', None) or []):
        if (
            isinstance(pf, dict)
            and pf.get('type') == 'publication_handoff'
            and pf.get('artifact_bearing') is True
        ):
            return 'bearing'
    return 'not_required'


def _receipt_matches_request(snapshot, *, organization_id, payload) -> bool:
    """P1 #5 / §17.2.4: the closed Wiki receipt must match the requested tuple
    exactly — an ID plus 64 hex characters is not proof. Verify organization,
    publication result, the ordered page/version/anchor-hash tuples, and
    recompute the domain-separated snapshot digest."""
    if str(snapshot.get('organization_id')) != str(organization_id):
        return False
    if str(snapshot.get('publication_result') or '') != payload['publication_result']:
        return False
    returned_pages = snapshot.get('pages')
    if returned_pages != payload['pages']:
        return False
    expected_digest = _digest(
        'io.shizuha.wiki-publication-snapshot/v1',
        {
            'organization_id': int(organization_id),
            'publication_result': payload['publication_result'],
            'pages': payload['pages'],
        },
    )
    return str(snapshot.get('snapshot_digest') or '').lower() == expected_digest


def _hold(attempt_id, state, code, evidence=None):
    # P1 #3 / §17.3: a hold can NEVER rewrite a terminal-success state
    # (PUBLISHED/DELIVERED). A concurrent exact replay that loses the finalize
    # race must not downgrade the already-published attempt.
    PublicationAttempt.objects.filter(pk=attempt_id).exclude(
        state__in=_TERMINAL_SUCCESS
    ).update(
        state=state,
        failure_code=code,
        failure_evidence=evidence or {},
    )


def _stale_hold_and_consume_round(attempt_id, item_id, review_round_id, evidence):
    """§17.2.3 / supplemental P1 #2: source drift atomically produces BOTH the
    terminal ``stale_publication_hold`` and the retired review round — one
    transaction, locked on the attempt row, so no crash can commit a consumed
    round with a non-terminal attempt (or vice versa). Concurrent stale holds
    serialize on the attempt lock; the ``uniq_pub_consumed_round`` constraint
    remains the cross-attempt fence."""
    with transaction.atomic():
        locked = PublicationAttempt.objects.select_for_update().get(pk=attempt_id)
        PublicationConsumedRound.objects.get_or_create(
            item_id=item_id,
            review_round_id=review_round_id,
            defaults={'consumed_by_attempt': locked.pk, 'reason': 'stale_publication_hold'},
        )
        # Monotonic: never rewrite terminal success (P1 #3 discipline).
        if locked.state not in _TERMINAL_SUCCESS:
            locked.state = 'stale_publication_hold'
            locked.failure_code = 'stale_publication_hold'
            locked.failure_evidence = evidence or {}
            locked.save(update_fields=[
                'state', 'failure_code', 'failure_evidence', 'updated_at',
            ])


def publication_binding_enabled() -> bool:
    """P2 #2 / §17.3 gate-off rollback: a reviewed, disabled-by-default
    activation gate. When off, NEW publication attempts are stopped fail-closed
    (never a fallback to legacy unvalidated emission), while existing snapshots,
    bindings, terminal holds, outbox rows, and audit history are preserved."""
    return bool(getattr(settings, 'PLS482_PUBLICATION_BINDING_ENABLED', False))


class PublicationHandoffService:
    @classmethod
    def execute(cls, item_id, transition, *, user=None, screen_data=None):
        from .workflow_service import WorkflowService, WorkflowValidationError

        try:
            payload = _normalize_input(screen_data)
        except PublicationInputError as exc:
            raise WorkflowValidationError(str(exc), code='INVALID_PUBLICATION_BINDING')

        request_body = {
            'item_id': int(item_id),
            'transition_id': str(transition.id),
            **payload,
        }
        request_digest = _digest('io.shizuha.pulse-publication-attempt/v1', request_body)

        # §17.1/17.2: the server classifies the artifact policy; the caller
        # cannot choose it and supplies no artifact authority.
        artifact_policy = _classify_artifact_policy(transition)
        with transaction.atomic():
            item = Item.objects.select_for_update(of=('self',)).select_related('project').get(pk=item_id)
            existing = PublicationAttempt.objects.select_for_update().filter(
                operation_key=payload['operation_key']
            ).first()
            if existing:
                if existing.request_digest != request_digest:
                    raise WorkflowValidationError(
                        'operation_key was already used with different publication inputs.',
                        code='PUBLICATION_OPERATION_CONFLICT',
                    )
                # §17.3: exact replay of terminal success is success — checked
                # BEFORE the activation gate, so a lost-response retry of an
                # already published/delivered attempt stays idempotent even
                # after gate-off rollback (supplemental P1 #3).
                if existing.state in _TERMINAL_SUCCESS:
                    return item
                if existing.state in _TERMINAL_HOLDS:
                    raise WorkflowValidationError(
                        f'publication attempt is terminally held: {existing.failure_code or existing.state}',
                        code=existing.state.upper(),
                    )
            # P2 #2: fail closed while gated off — applies only to NEW and
            # non-terminal work (the interception still raises, so the carrier
            # can never take the legacy unfenced path while disabled).
            if not publication_binding_enabled():
                raise WorkflowValidationError(
                    'publication binding is gated off; new publication attempts are '
                    'disabled (existing snapshots/bindings/holds/outbox are preserved).',
                    code='PUBLICATION_GATE_OFF',
                )
            # P1 #2 / §17.4.5: a review round retired by a prior stale hold can
            # never be reused, even under a fresh operation key.
            if PublicationConsumedRound.objects.filter(
                item=item, review_round_id=payload['review_round_id']
            ).exists():
                raise WorkflowValidationError(
                    'publication review round was retired by a prior stale hold; '
                    'start a fresh review round.',
                    code='STALE_PUBLICATION_HOLD',
                )
            current_round = str(
                (item.metadata or {}).get('publication_review_round_id')
                or (item.metadata or {}).get('review_round_id')
                or ''
            )
            # PLS-960: the feature RFC same-carrier preparation action is the
            # authority that starts its first (and every replacement) review
            # round.  Ordinary handoff transitions still require a pre-existing
            # round minted by their own review-entry protocol.
            prepares_round = bool(
                item.workflow_name == 'feature-request-rfc'
                and item.status == 'rfc_design'
                and transition.from_status == 'rfc_design'
                and transition.to_status == 'rfc_design'
                and str(transition.name or '').strip().lower()
                == 'prepare publication binding'
            )
            if prepares_round:
                if PublicationBinding.objects.filter(item=item).exists():
                    raise WorkflowValidationError(
                        'a current PublicationBinding already exists for this RFC; '
                        'use its review round instead of minting a duplicate.',
                        code='CURRENT_PUBLICATION_BINDING_EXISTS',
                    )
                # Treat the caller's round as current only inside this saga.
                # Persisting it before Wiki CAS would leave a false review round
                # behind when the CAS fails or the receipt is stale.
                current_round = payload['review_round_id']
            if current_round != payload['review_round_id']:
                raise WorkflowValidationError(
                    'publication review round is missing or stale; start a fresh review round.',
                    code='CARRIER_CONFLICT_HOLD',
                )
            # Revalidate the exact current transition before persisting PREPARED.
            WorkflowService.validate_transition(
                item, transition.to_status, user, screen_data, transition_id=str(transition.id)
            )
            if existing:
                attempt = existing
            else:
                try:
                    # The operation key is global while the carrier lock is
                    # per Item.  Isolate the insert in a savepoint so two
                    # different carriers racing on one key cannot poison this
                    # transaction with a raw unique-constraint IntegrityError.
                    with transaction.atomic():
                        attempt = PublicationAttempt.objects.create(
                            operation_key=payload['operation_key'],
                            request_digest=request_digest,
                            item=item,
                            transition_id=transition.id,
                            expected_item_updated_at=item.updated_at,
                            expected_status=item.status,
                            review_round_id=payload['review_round_id'],
                            publication_result=payload['publication_result'],
                            artifact_policy=artifact_policy,
                            artifact_authority_ref=None,
                            expected_pages=payload['pages'],
                            state='prepared',
                        )
                except IntegrityError:
                    # The unique-key winner committed while this carrier held
                    # only its own Item lock. Re-read that canonical attempt
                    # under lock and return the typed idempotency verdict.
                    attempt = PublicationAttempt.objects.select_for_update().get(
                        operation_key=payload['operation_key']
                    )
                    if attempt.request_digest != request_digest:
                        raise WorkflowValidationError(
                            'operation_key was already used with different publication inputs.',
                            code='PUBLICATION_OPERATION_CONFLICT',
                        )
            # §17.1/17.2.2: an artifact-bearing handoff requires a separately
            # implemented canonical PLAT-4595 provider. None exists, so this path
            # fails closed and can never be armed. PLS-482 mints no substitute.
            # Persist the hold in THIS transaction and raise only after it
            # commits — raising inside the atomic block would roll the hold back.
            if attempt.artifact_policy == 'bearing':
                if attempt.state not in _TERMINAL_HOLDS:
                    attempt.state = 'artifact_authority_unavailable_hold'
                    attempt.failure_code = 'artifact_authority_unavailable'
                    attempt.failure_evidence = {
                        'detail': 'No canonical PLAT-4595 artifact-authority provider exists.',
                    }
                    attempt.save(update_fields=[
                        'state', 'failure_code', 'failure_evidence', 'updated_at',
                    ])
                unavailable_hold = True
            else:
                unavailable_hold = False
                # Wiki-only publication: explicit null artifact authority (§17.2).
                if attempt.state == 'prepared':
                    attempt.state = 'artifact_not_required'
                    attempt.artifact_authority_ref = None
                    attempt.save(update_fields=['state', 'artifact_authority_ref', 'updated_at'])
        if unavailable_hold:
            raise WorkflowValidationError(
                'artifact-bearing publication requires a PLAT-4595 authority provider '
                'that is not implemented; failing closed.',
                code='ARTIFACT_AUTHORITY_UNAVAILABLE_HOLD',
            )

        # Wiki CAS is outside the Pulse transaction by design (§17.2.3).
        if attempt.state != 'snapshot_committed':
            try:
                snapshot = _wiki_snapshot(organization_id=item.project.org_id, payload=payload)
            except PublicationWikiError as exc:
                if exc.code == 'stale_publication_hold':
                    # §17.2.3 / P1 #2: hold + round retirement commit atomically.
                    _stale_hold_and_consume_round(
                        attempt.pk, item_id, payload['review_round_id'], exc.evidence
                    )
                elif exc.code == 'wiki_invalid_response':
                    # A malformed closed receipt is a terminal authority
                    # conflict, not a retryable transport failure. Persist the
                    # typed hold before surfacing it so replay cannot loop on a
                    # remote CAS that has already linearized.
                    _hold(
                        attempt.pk,
                        'carrier_conflict_hold',
                        'wiki_invalid_response',
                        {'detail': str(exc)},
                    )
                    raise WorkflowValidationError(
                        str(exc), code='CARRIER_CONFLICT_HOLD'
                    )
                raise WorkflowValidationError(str(exc), code=exc.code.upper())
            # P1 #5 / §17.2.4: an ID plus 64 hex characters is not proof. The
            # closed Wiki receipt must match the requested organization, result,
            # and ordered page/version/anchor-hash tuples, and recompute the
            # domain-separated snapshot digest, before SNAPSHOT_COMMITTED.
            if not _receipt_matches_request(
                snapshot, organization_id=item.project.org_id, payload=payload
            ):
                _hold(
                    attempt.pk, 'carrier_conflict_hold', 'wiki_receipt_mismatch',
                    {'detail': 'Wiki snapshot receipt did not match the requested tuple.'},
                )
                raise WorkflowValidationError(
                    'Wiki snapshot receipt did not match the requested '
                    'organization/result/pages/digest.',
                    code='CARRIER_CONFLICT_HOLD',
                )
            with transaction.atomic():
                locked_attempt = PublicationAttempt.objects.select_for_update().get(pk=attempt.pk)
                if (
                    locked_attempt.state not in _TERMINAL_SUCCESS
                    and locked_attempt.state not in (
                        'artifact_not_required', 'artifact_prepared', 'snapshot_committed'
                    )
                ):
                    raise WorkflowValidationError(
                        f'publication attempt cannot accept a snapshot from {locked_attempt.state}.',
                        code='PUBLICATION_STATE_CONFLICT',
                    )
                # P1 #3 / §17.3: monotonic — do not re-commit a snapshot if a
                # concurrent caller already advanced to snapshot_committed or
                # terminal success. The finalization block below reloads the
                # carrier and returns the already-published result.
                if locked_attempt.state not in {'snapshot_committed', *_TERMINAL_SUCCESS}:
                    locked_attempt.wiki_snapshot_id = snapshot['publication_snapshot_id']
                    locked_attempt.snapshot_digest = snapshot['snapshot_digest']
                    locked_attempt.snapshot_pages = snapshot['pages']
                    locked_attempt.state = 'snapshot_committed'
                    locked_attempt.save(update_fields=[
                        'wiki_snapshot_id', 'snapshot_digest', 'snapshot_pages', 'state', 'updated_at',
                    ])

        # Pulse finalization: carrier effect + binding + one outbox in one commit.
        try:
            with transaction.atomic():
                attempt = PublicationAttempt.objects.select_for_update(of=('self',)).select_related(
                    'item__project'
                ).get(pk=attempt.pk)
                item = Item.objects.select_for_update(of=('self',)).select_related('project').get(pk=item_id)
                # P1 #3 / §17.3: a concurrent exact replay that already reached a
                # terminal-success state is success, not a carrier conflict.
                if attempt.state in _TERMINAL_SUCCESS:
                    return item
                current_round = str(
                    (item.metadata or {}).get('publication_review_round_id')
                    or (item.metadata or {}).get('review_round_id')
                    or ''
                )
                if prepares_round and not current_round:
                    current_round = attempt.review_round_id
                if (
                    attempt.state != 'snapshot_committed'
                    or item.status != attempt.expected_status
                    or item.updated_at != attempt.expected_item_updated_at
                    or current_round != attempt.review_round_id
                    or _classify_artifact_policy(transition) != attempt.artifact_policy
                ):
                    raise PublicationCarrierConflict(
                        'carrier/review/artifact-policy fence changed before finalize.'
                    )

                resume_data = dict(screen_data or {})
                resume_data['_publication_attempt_id'] = str(attempt.id)
                item._suppress_standard_notifications = True
                item._publication_handoff_suppress_external_effects = True
                result_item = WorkflowService._execute_transition_impl(
                    item, transition.to_status, user=user, screen_data=resume_data,
                    transition_id=str(transition.id),
                )
                if not result_item.assignee_id:
                    raise PublicationCarrierConflict('publication transition produced no concrete recipient.')

                # PLS-634 / PLS-481: freeze exact-org + carrier CAS into the
                # producer-minted binding tuple so Accept — Capacity-Held can
                # fail closed without fixture-only keys or timestamp heuristics.
                carrier_org_id = None
                try:
                    project = getattr(result_item, 'project', None)
                    if project is not None and getattr(project, 'org_id', None) is not None:
                        carrier_org_id = int(project.org_id)
                    elif getattr(result_item, 'project_id', None):
                        from ..models import Project as _Project
                        carrier_org_id = int(
                            _Project.objects.values_list('org_id', flat=True).get(
                                pk=result_item.project_id,
                            )
                        )
                except Exception:
                    carrier_org_id = None
                binding_tuple = {
                    'publication_snapshot_id': str(attempt.wiki_snapshot_id),
                    'pages': attempt.snapshot_pages,
                    'review_round_id': attempt.review_round_id,
                    'publication_result': attempt.publication_result,
                    # The downstream object must preserve the exact digest Pulse
                    # verified, not merely the snapshot ID and ordered pages.
                    'snapshot_digest': attempt.snapshot_digest,
                    # Explicit null for a server-classified Wiki-only publication
                    # (§17.1). Artifact-bearing paths never reach finalize until a
                    # canonical PLAT-4595 provider exists.
                    'artifact_authority_ref': attempt.artifact_authority_ref,
                    # Immutable Accept authorities (PLS-634 rei P1): exact org and
                    # the carrier state_generation observed at finalize.
                    'organization_id': carrier_org_id,
                    'carrier_state_generation': int(
                        getattr(result_item, 'state_generation', 0) or 0
                    ),
                }
                binding_digest = _digest('io.shizuha.pulse-publication-binding/v1', binding_tuple)
                binding, _ = PublicationBinding.objects.get_or_create(
                    attempt=attempt,
                    defaults={
                        'item': result_item,
                        'publication_snapshot_id': attempt.wiki_snapshot_id,
                        'binding_digest': binding_digest,
                        'binding_tuple': binding_tuple,
                    },
                )
                metadata = dict(result_item.metadata or {})
                if prepares_round:
                    metadata['publication_review_round_id'] = attempt.review_round_id
                metadata['publication_binding'] = binding_tuple
                metadata['publication_binding_digest'] = binding_digest
                result_item.metadata = metadata
                result_item.save(update_fields=['metadata', 'updated_at'])
                outbox, _ = PublicationOutbox.objects.get_or_create(
                    binding=binding,
                    defaults={
                        'recipient_user_id': result_item.assignee_id,
                        'payload': binding_tuple,
                    },
                )
                attempt.state = 'published'
                attempt.failure_code = ''
                attempt.failure_evidence = {}
                attempt.save(update_fields=['state', 'failure_code', 'failure_evidence', 'updated_at'])

                def _enqueue():
                    try:
                        from ..tasks import deliver_publication_outbox
                        deliver_publication_outbox.delay(str(outbox.id))
                    except Exception:
                        # The periodic outbox sweep is the durable recovery path.
                        pass
                transaction.on_commit(_enqueue)
                return result_item
        except PublicationCarrierConflict as exc:
            _hold(attempt.pk, 'carrier_conflict_hold', 'carrier_conflict_hold', {'detail': str(exc)})
            raise WorkflowValidationError(str(exc), code='CARRIER_CONFLICT_HOLD')
        except WorkflowValidationError as exc:
            _hold(
                attempt.pk,
                'carrier_conflict_hold',
                'carrier_conflict_hold',
                {'detail': str(exc), 'underlying_code': exc.code or ''},
            )
            raise WorkflowValidationError(
                f'carrier finalization rejected after Wiki snapshot: {exc}',
                code='CARRIER_CONFLICT_HOLD',
            )


def refence_publication_binding_cas(item):
    """PLAT-7783 / PLS-481: move the CAS fence to the post-resume generation.

    Block/unblock (and every later legitimate resume) advances
    ``item.state_generation`` while the frozen PublicationBinding tuple keeps
    the mint-time generation. Re-mint is refused (CURRENT_PUBLICATION_BINDING_EXISTS),
    so Accept — Capacity-Held deadlocks.

    A resume with unchanged frozen bytes is not a stale plan: only the fence
    value moves. Content keys (pages, digest, round, org) stay identical.
    """
    if item is None:
        return False
    binding = (
        PublicationBinding.objects.filter(item_id=item.pk)
        .order_by('-created_at')
        .first()
    )
    if binding is None:
        return False
    bt = dict(binding.binding_tuple or {})
    if not bt:
        return False
    new_gen = int(getattr(item, 'state_generation', 0) or 0)
    try:
        old_gen = int(bt.get('carrier_state_generation'))
    except (TypeError, ValueError):
        old_gen = None
    if old_gen is not None and old_gen == new_gen:
        return False
    bt['carrier_state_generation'] = new_gen
    digest = _digest('io.shizuha.pulse-publication-binding/v1', bt)
    binding.binding_tuple = bt
    binding.binding_digest = digest
    binding.save(update_fields=['binding_tuple', 'binding_digest'])
    meta = dict(item.metadata or {})
    meta['publication_binding'] = bt
    meta['publication_binding_digest'] = digest
    item.metadata = meta
    item.save(update_fields=['metadata', 'updated_at'])
    return True


def refence_if_fence_only_drift(item):
    """PLAT-7783 residual: lazily re-fence when ONLY the CAS fence is stale.

    The trigger-time re-fences (explicit Blocker-Resolved transitions and the
    auto-unblock restore) are point-in-time: any later legitimate state-kind
    mutation on the carrier — an auto-failover assignment, a priority bump —
    advances ``state_generation`` past the fence again and re-deadlocks the
    Accept — Capacity-Held round-trip (live-verified on CTX-625, 09-08).

    A fence-only drift with intact frozen content is not a stale plan. This
    helper re-fences ONLY when the binding row's content keys are identical to
    the item-level mirror (``metadata.publication_binding``) apart from
    ``carrier_state_generation`` — i.e. nothing bypassed the refence path and
    edited the row directly. Any content disagreement (or a missing mirror)
    leaves the fence untouched so the gate fails closed.

    Returns True when a re-fence was performed, False otherwise.
    """
    if item is None:
        return False
    binding = (
        PublicationBinding.objects.filter(item_id=item.pk)
        .order_by('-created_at')
        .first()
    )
    if binding is None:
        return False
    bt = dict(binding.binding_tuple or {})
    if not bt or bt.get('carrier_state_generation') is None:
        return False
    current_gen = int(getattr(item, 'state_generation', 0) or 0)
    try:
        fenced_gen = int(bt['carrier_state_generation'])
    except (TypeError, ValueError):
        return False
    if fenced_gen == current_gen:
        return False
    meta = dict(item.metadata or {})
    mirror = meta.get('publication_binding')
    if not isinstance(mirror, dict) or not mirror:
        return False
    mirrored_gen = mirror.get('carrier_state_generation')
    try:
        mirrored_gen = int(mirrored_gen)
    except (TypeError, ValueError):
        return False
    if mirrored_gen != fenced_gen:
        # Mirror disagrees with the row's fence: the row was edited outside
        # the refence path. Fail closed.
        return False
    content_keys = (set(bt.keys()) | set(mirror.keys())) - {'carrier_state_generation'}
    for key in content_keys:
        if bt.get(key) != mirror.get(key):
            return False
    return bool(refence_publication_binding_cas(item))
