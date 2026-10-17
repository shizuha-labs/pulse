"""PLAT-4572 exact-org routing authority, independent of access membership.

The module intentionally owns no TeamRoster or ProjectMembership writes.  It
implements the dark-by-default ADR-0041 aggregate, payload-bound maker/checker
commands, local freshness leases, and the receive-assignment authorization
predicate consumed by the item-aware resolver.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import os
import random
import uuid

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    Item,
    CrossOrgAssignmentDelegation,
    Project,
    ReceiveAssignmentEntitlement,
    RoutingAuthorityCheckpoint,
    RoutingAuthorityCommand,
    RoutingAuthorityConsumerReceipt,
    RoutingAuthorityConsumerState,
    RoutingAuthorityOutbox,
    RoutingAuthorityHoldSignal,
    RoutingAuthorityQuarantine,
    RoutingAssignmentReceipt,
    RoutingDenialAudit,
    RoutingSubject,
    RoutingTeam,
    RoutingTeamBinding,
    Workflow,
)


CHECKPOINT_INTERVAL_SECONDS = 30
MAX_LEASE_SECONDS = 90
MAX_CLOCK_SKEW_SECONDS = 15
ACTION_RECEIVE_ASSIGNMENT = 'receive_assignment'
CLEAR_ASSIGNMENT_REASONS = frozenset({
    'explicit_unassign',
    'no_candidate',
    'permission_drift',
    'review_starvation',
    'recurring_owner_unresolved',
    'routability_hold',
    'wip_overload_reclaim',
    'wip_ready_overflow',
    # PLAT-8222 suggestion 3 / PLAT-9119: the sanctioned cannot-execute
    # self-service return (POST /items/{id}/return-to-lane/) clears the
    # concrete owner back to the lane queue through this same guarded
    # command. The reason was missing here, so every return call raised
    # assignment_clear_reason_invalid → HTTP 500 (haru ANDON 2026-09-18).
    'cannot_execute_return',
})


class RoutingAuthorityError(Exception):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    subject_id: int | None = None
    item_id: int | None = None
    organization_id: int | None = None
    project_id: int | None = None
    routing_team_id: str | None = None
    assignment_group: str | None = None
    workflow_name: str | None = None
    binding_version: int | None = None
    entitlement_version: int | None = None
    subject_generation: int | None = None
    revocation_high_water: int | None = None
    grant_source: str | None = None
    grant_event_id: str | None = None
    freshness_generation: int | None = None
    delegation_version: int | None = None
    freshness_vector_digest: str | None = None
    policy_manifest_digest: str | None = None


@dataclass(frozen=True)
class AssignmentClearDecision:
    """Reviewed intent to clear one exact owner into one exact result lane."""

    action: str
    item_id: int
    organization_id: int
    project_id: int
    routing_team_id: str | None
    assignment_group: str
    workflow_name: str
    subject_id: int | None
    assignee_email: str
    result_organization_id: int
    result_project_id: int
    result_routing_team_id: str | None
    result_assignment_group: str
    result_workflow_name: str
    reason: str


@dataclass(frozen=True)
class AssignmentDirectHandoffDecision:
    """PLS-539 (hiro/ADR-0010): reviewed intent for an authorized direct handoff
    that crosses OUT of one exact materialized guarded lane into one exact concrete
    NEW owner in a (possibly unguarded) result lane.

    Minted only by the shared direct-handoff command AFTER the entrypoint's reason
    gate + task-org authorization pass (the same trust boundary that sets
    ``_pls539_direct_override`` today). ``Item.save``'s fence keys on the presence
    of this typed decision (not a bare group-diff) to permit the old_guarded ->
    new_unguarded-with-owner crossing, revalidating the exact tuple under the row
    lock before arming the one-use DB receipt the trigger consumes. The OLD guarded
    lane fields bind what is being left; ``subject_id``/``assignee_email`` bind the
    NEW owner who lands; ``result_*`` bind the NEW lane.
    """

    item_id: int
    # OLD guarded lane being left.
    organization_id: int
    project_id: int
    routing_team_id: str | None
    assignment_group: str
    workflow_name: str
    # NEW concrete owner who lands.
    subject_id: int | None
    assignee_email: str
    # NEW (possibly unguarded) result lane.
    result_organization_id: int
    result_project_id: int
    result_routing_team_id: str | None
    result_assignment_group: str
    result_workflow_name: str
    reason: str


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


# PLS-277 §16.4: this is a closed, reviewed action-policy manifest.  Local
# heads are locked/CASed; projected ID freshness is accepted only from its
# ordered event checkpoint.  Cross-org delegation is a conditional required
# authority, never inferred from the receive entitlement.
RECEIVE_ASSIGNMENT_POLICY = {
    'version': 1,
    'action': ACTION_RECEIVE_ASSIGNMENT,
    'required_local_heads': (
        'item', 'project', 'workflow', 'routing_team', 'routing_binding',
        'routing_subject', 'receive_entitlement', 'revocation_high_water',
    ),
    'required_projected_checkpoints': ('shizuha-id',),
    'conditional_heads': {'cross_org': ('cross_org_delegation',)},
    'max_lease_seconds': MAX_LEASE_SECONDS,
    'max_clock_skew_seconds': MAX_CLOCK_SKEW_SECONDS,
}
RECEIVE_ASSIGNMENT_POLICY_DIGEST = _digest(RECEIVE_ASSIGNMENT_POLICY)


def _team_key(value) -> str:
    from .workflow_service import WorkflowService
    return WorkflowService._canonical_team_name(value)


def bindings_enforced() -> bool:
    return os.environ.get('PULSE_ROUTING_BINDINGS_ENFORCE', '').strip().lower() in {
        '1', 'true', 'yes', 'on', 'enforce',
    }


def binding_team_enabled(team_name: str) -> bool:
    """Whether the feature gate currently permits new binding candidates."""
    if not bindings_enforced():
        return False
    configured = {
        _team_key(value) for value in
        os.environ.get('PULSE_ROUTING_BINDINGS_TEAMS', '').split(',') if value.strip()
    }
    return not configured or '*' in configured or _team_key(team_name) in configured


def routing_authority_applies_to_item(item: Item) -> bool:
    """Sticky containment predicate for an exact routing lane.

    Before a reviewed RoutingTeam exists, a dark schema is inert.  Once an
    exact team has been materialized, gate-off means HOLD rather than legacy
    TeamRoster fallback. This is the rollback invariant from §16.9.
    """
    if item is None:
        return False
    return routing_authority_applies_to_context(
        getattr(item, 'project_id', None),
        getattr(item, 'assignment_group', ''),
    )


def routing_authority_applies_to_context(project_id, assignment_group) -> bool:
    """Return whether an exact persisted project/lane tuple is guarded.

    This lookup deliberately ignores an Item's possibly stale ``project``
    relation cache.  Model writers use it while changing ``project_id`` and
    the database trigger applies the same exact-project rule independently.
    """
    if not project_id:
        return False
    key = _team_key(assignment_group)
    if not key:
        return False
    org_id = Project.objects.filter(pk=project_id).values_list(
        'org_id', flat=True,
    ).first()
    if org_id is None:
        return False
    org_id = int(org_id)
    if RoutingTeam.objects.filter(organization_id=org_id, key=key).exists():
        return True
    # A bare team-name gate is never authority for another organization.  The
    # reviewed create_team command materializes the exact (org, team) boundary;
    # until then this lane remains on its exact-org TeamRoster compatibility
    # source even if a same-named lane is armed elsewhere.
    return False


def _actor_email(actor) -> str:
    return (getattr(actor, 'email', '') or '').strip().lower()


def _actor_id(actor) -> int:
    value = getattr(actor, 'id', None)
    if value is None or not str(value).isdigit():
        raise RoutingAuthorityError('actor_identity_missing')
    return int(value)


def _actor_role_snapshot(actor, *, lock=False) -> tuple[set[str], str]:
    """Current maker/checker authority with a commit-time row lock/digest."""
    from ..models import TeamRoster
    from .workflow_service import WorkflowService
    email = _actor_email(actor)
    qs = TeamRoster.objects.filter(is_active=True).order_by('id')
    if lock:
        qs = qs.select_for_update()
    rows = list(qs)
    teams = set()
    evidence = []
    for roster in rows:
        members = {
            str(member).strip().lower()
            for tier in (roster.tiers or [])
            for member in (tier or [])
            if isinstance(member, str)
        } | {
            str(member).strip().lower()
            for member in (roster.junior_emails or [])
            if isinstance(member, str)
        }
        if email in members:
            teams.add(WorkflowService._canonical_team_name(roster.team_name))
            # The authority digest is actor-scoped.  An unrelated Review row
            # changing must not invalidate a maker's Engineering proof, while
            # removing this actor makes the row disappear and changes the
            # digest.  All active rows remain locked above to close races.
            evidence.append({
                'id': roster.id,
                'organization_slug': roster.organization_slug,
                'team_name': roster.team_name,
                'tiers': roster.tiers,
                'junior_emails': roster.junior_emails,
                'is_active': roster.is_active,
                'updated_at': roster.updated_at.isoformat() if roster.updated_at else None,
            })
    return teams, _digest({'actor_id': _actor_id(actor), 'email': email, 'rows': evidence})


def _actor_teams(actor) -> set[str]:
    return _actor_role_snapshot(actor)[0]


def _require_maker(actor, *, lock=False) -> str:
    teams, digest = _actor_role_snapshot(actor, lock=lock)
    if not (teams & {'engineering', 'devops', 'architecture'}):
        raise RoutingAuthorityError('maker_rbac_denied')
    return digest


def _require_checker(actor, *, lock=False) -> str:
    teams, digest = _actor_role_snapshot(actor, lock=lock)
    if not (teams & {'security', 'review', 'merge', 'architecture'}):
        raise RoutingAuthorityError('checker_rbac_denied')
    return digest


def _normalize_payload(command_type: str, payload: dict) -> dict:
    raw = dict(payload or {})
    if command_type == 'create_team':
        return {
            'team_key': _team_key(raw.get('team_key')),
            'display_name': str(raw.get('display_name') or raw.get('team_key') or '').strip(),
        }
    if command_type in {'activate_subject', 'revoke_subject'}:
        return {'subject_email': str(raw.get('subject_email') or '').strip().lower()}
    if command_type in {'activate_binding', 'tombstone_binding'}:
        return {
            'team_key': _team_key(raw.get('team_key')),
            'subject_email': str(raw.get('subject_email') or '').strip().lower(),
            'subject_organization_id': int(raw.get('subject_organization_id') or 0),
        }
    if command_type in {
        'grant_receive', 'revoke_receive',
        'grant_cross_org_delegation', 'revoke_cross_org_delegation',
    }:
        return {
            'team_key': _team_key(raw.get('team_key')),
            'subject_email': str(raw.get('subject_email') or '').strip().lower(),
            'subject_organization_id': int(raw.get('subject_organization_id') or 0),
            'project_key': str(raw.get('project_key') or '').strip().upper(),
            'workflow_name': str(raw.get('workflow_name') or '').strip(),
            'action': ACTION_RECEIVE_ASSIGNMENT,
            'grant_source': str(raw.get('grant_source') or '').strip(),
            'grant_event_id': str(raw.get('grant_event_id') or '').strip(),
            'expires_at': str(raw.get('expires_at') or '').strip(),
        }
    raise RoutingAuthorityError('command_type_unknown')


def _aggregate_key(command_type: str, org_id: int, payload: dict) -> str:
    if command_type == 'create_team':
        return f'team:{org_id}:{payload["team_key"]}'
    if command_type in {'activate_subject', 'revoke_subject'}:
        return f'subject:{org_id}:{payload["subject_id"]}'
    if command_type in {'activate_binding', 'tombstone_binding'}:
        return (f'binding:{org_id}:{payload["team_key"]}:'
                f'{payload["subject_organization_id"]}:{payload["subject_id"]}')
    prefix = ('delegation' if command_type in {
        'grant_cross_org_delegation', 'revoke_cross_org_delegation'
    } else 'entitlement')
    return (
        f'{prefix}:{org_id}:{payload["team_key"]}:'
        f'{payload["subject_organization_id"]}:{payload["subject_id"]}:'
        f'{payload["project_key"]}:{payload["workflow_name"]}:{payload["action"]}'
    )


def propose_command(*, actor, command_type: str, organization_id: int, payload: dict,
                    expected_version: int, reason: str, idempotency_key,
                    approval_ttl_seconds: int = 900) -> RoutingAuthorityCommand:
    """Create a command proposal; same-key/same-payload retry returns the row."""
    maker_authority_digest = _require_maker(actor)
    if not reason or not str(reason).strip():
        raise RoutingAuthorityError('reason_required')
    try:
        key = uuid.UUID(str(idempotency_key))
    except (ValueError, TypeError, AttributeError):
        raise RoutingAuthorityError('idempotency_key_invalid')
    ttl = max(60, min(int(approval_ttl_seconds), 3600))
    normalized = _normalize_payload(command_type, payload)
    if 'subject_email' in normalized:
        subject_id, canonical, active = _resolve_identity(normalized['subject_email'])
        if command_type not in {
            'revoke_subject', 'tombstone_binding', 'revoke_receive',
            'revoke_cross_org_delegation',
        } and not active:
            raise RoutingAuthorityError('identity_inactive')
        normalized['subject_id'] = subject_id
        normalized['subject_email'] = canonical
        if command_type in {'activate_subject', 'revoke_subject'}:
            normalized['subject_organization_id'] = int(organization_id)
        elif not normalized.get('subject_organization_id'):
            normalized['subject_organization_id'] = int(organization_id)
    if any(v == '' for k, v in normalized.items()
           if k in {'team_key', 'display_name', 'subject_email', 'workflow_name'}):
        raise RoutingAuthorityError('command_payload_incomplete')
    bound = {
        'command_type': command_type,
        'organization_id': int(organization_id),
        'expected_version': int(expected_version),
        'payload': normalized,
        'maker_id': _actor_id(actor),
        'maker_email': _actor_email(actor),
        'maker_authority_digest': maker_authority_digest,
        'reason': str(reason).strip(),
        'authority_generation': 1,
    }
    payload_digest = _digest(bound)
    existing = RoutingAuthorityCommand.objects.filter(idempotency_key=key).first()
    if existing:
        if existing.payload_digest != payload_digest:
            raise RoutingAuthorityError('idempotency_payload_conflict')
        return existing
    return RoutingAuthorityCommand.objects.create(
        command_type=command_type,
        organization_id=int(organization_id),
        aggregate_key=_aggregate_key(command_type, int(organization_id), normalized),
        expected_version=int(expected_version),
        normalized_payload=normalized,
        payload_digest=payload_digest,
        idempotency_key=key,
        maker_id=_actor_id(actor),
        maker_email=_actor_email(actor),
        maker_authority_digest=maker_authority_digest,
        reason=str(reason).strip(),
        approval_expires_at=timezone.now() + timedelta(seconds=ttl),
    )


def _resolve_identity(email: str) -> tuple[int, str, bool]:
    from ..user_service import user_service
    row = user_service.get_user_by_email(email)
    if not row:
        raise RoutingAuthorityError('identity_unresolved')
    uid = row.get('id') or row.get('user_id')
    canonical = (row.get('email') or '').strip().lower()
    if not uid or not str(uid).isdigit() or not canonical:
        raise RoutingAuthorityError('identity_unresolved')
    status = str(row.get('status') or '').strip().lower()
    active = not (
        row.get('is_active') is False or row.get('active') is False
        or status in {'inactive', 'disabled', 'deleted', 'suspended', 'revoked'}
    )
    return int(uid), canonical, active


def _parse_expiry(value: str):
    if not value:
        return None
    from django.utils.dateparse import parse_datetime
    parsed = parse_datetime(value)
    if parsed is None:
        raise RoutingAuthorityError('expiry_invalid')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def apply_authority_checkpoint(*, authority_id: str, aggregate_key: str,
                               subject_generation: int, observed_version: int,
                               digest: str, revocation_high_water: int,
                               source_event_id, observed_at=None,
                               source_expires_at=None):
    """Apply one causally-bound ordered checkpoint without remote renewal.

    Only an event carrying the source event id/version/watermark may advance a
    lease.  Replay is idempotent, conflicts or version/high-water rewind HOLD,
    and source expiry is an upper bound.  Request handlers and the periodic
    worker never call an identity endpoint to manufacture freshness.
    """
    now = timezone.now()
    observed_at = observed_at or now
    if timezone.is_naive(observed_at):
        observed_at = timezone.make_aware(observed_at)
    if source_expires_at and timezone.is_naive(source_expires_at):
        source_expires_at = timezone.make_aware(source_expires_at)
    if observed_at > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise RoutingAuthorityError('checkpoint_clock_ahead')
    event_id = uuid.UUID(str(source_event_id))
    lease = observed_at + timedelta(seconds=MAX_LEASE_SECONDS)
    if source_expires_at:
        lease = min(lease, source_expires_at)
    first_received = now
    with transaction.atomic():
        checkpoint, created = RoutingAuthorityCheckpoint.objects.select_for_update().get_or_create(
            authority_id=authority_id, aggregate_key=aggregate_key,
            defaults={
                'subject_generation': int(subject_generation),
                'observed_version': int(observed_version),
                'digest': str(digest),
                'revocation_high_water': int(revocation_high_water),
                'checkpoint_generation': 1,
                'source_event_id': event_id,
                'first_received_at': first_received,
                'source_expires_at': source_expires_at,
                'observed_at': observed_at,
                'lease_until': lease,
            },
        )
        if not created:
            if int(observed_version) < checkpoint.observed_version:
                _record_scope_hold(authority_id, aggregate_key,
                                   checkpoint.checkpoint_generation,
                                   'checkpoint_version_rewind')
                raise RoutingAuthorityError('checkpoint_version_rewind')
            if int(revocation_high_water) < checkpoint.revocation_high_water:
                _record_scope_hold(authority_id, aggregate_key,
                                   checkpoint.checkpoint_generation,
                                   'checkpoint_high_water_rewind')
                raise RoutingAuthorityError('checkpoint_high_water_rewind')
            if int(observed_version) == checkpoint.observed_version:
                if (checkpoint.digest != str(digest)
                        or checkpoint.source_event_id != event_id):
                    _record_scope_hold(authority_id, aggregate_key,
                                       checkpoint.checkpoint_generation,
                                       'checkpoint_digest_conflict')
                    raise RoutingAuthorityError('checkpoint_digest_conflict')
                return checkpoint
            checkpoint.subject_generation = int(subject_generation)
            checkpoint.observed_version = int(observed_version)
            checkpoint.digest = str(digest)
            checkpoint.revocation_high_water = int(revocation_high_water)
            checkpoint.checkpoint_generation += 1
            checkpoint.source_event_id = event_id
            checkpoint.first_received_at = first_received
            checkpoint.source_expires_at = source_expires_at
            checkpoint.observed_at = observed_at
            checkpoint.lease_until = lease
            checkpoint.save(update_fields=[
                'subject_generation', 'observed_version', 'digest',
                'revocation_high_water', 'checkpoint_generation',
                'source_event_id', 'first_received_at', 'source_expires_at',
                'observed_at', 'lease_until', 'updated_at',
            ])
        # Recovery is causal only when a newer ordered checkpoint commits.
        # Merely running the watchdog (or successfully sending its alert) must
        # never clear the HOLD.
        RoutingAuthorityHoldSignal.objects.filter(
            authority_id=authority_id,
            aggregate_key=aggregate_key,
            resolved_at__isnull=True,
        ).update(
            resolved_at=now,
            in_flight=False,
            in_flight_until=None,
            last_error='',
        )
        return checkpoint


def _invalidate_subject_checkpoint(subject: RoutingSubject, now=None):
    now = now or timezone.now()
    return RoutingAuthorityCheckpoint.objects.filter(
        authority_id='shizuha-id',
        aggregate_key=f'{subject.organization_id}:{subject.subject_id}',
    ).update(
        subject_generation=subject.generation,
        digest=subject.digest,
        revocation_high_water=subject.revocation_high_water,
        lease_until=now,
    )


def _write_outbox(*, authority_kind: str, aggregate_key: str, version: int,
                  digest: str, payload: dict, origin_event_id, tombstone=False):
    return RoutingAuthorityOutbox.objects.create(
        origin_event_id=origin_event_id,
        authority_kind=authority_kind,
        aggregate_key=aggregate_key,
        version=version,
        digest=digest,
        payload=payload,
        tombstone=tombstone,
    )


def _current_subject(org_id: int, email: str, *, lock=False):
    qs = RoutingSubject.objects
    if lock:
        qs = qs.select_for_update()
    return qs.filter(organization_id=org_id, canonical_email__iexact=email).first()


def _current_team(org_id: int, key: str, *, lock=False):
    qs = RoutingTeam.objects
    if lock:
        qs = qs.select_for_update()
    return qs.filter(organization_id=org_id, key=key).first()


def _apply_command(command: RoutingAuthorityCommand):
    payload = command.normalized_payload
    org_id = command.organization_id
    now = timezone.now()

    if command.command_type == 'create_team':
        team = _current_team(org_id, payload['team_key'], lock=True)
        actual = team.version if team else 0
        if actual != command.expected_version:
            raise RoutingAuthorityError('expected_version_conflict')
        if team is None:
            version = 1
            value = {'organization_id': org_id, 'key': payload['team_key'],
                     'display_name': payload['display_name'], 'state': 'active', 'version': version}
            digest = _digest(value)
            team = RoutingTeam.objects.create(
                organization_id=org_id, key=payload['team_key'],
                display_name=payload['display_name'], state=RoutingTeam.STATE_ACTIVE,
                version=version, digest=digest,
            )
        else:
            version = team.version + 1
            value = {'organization_id': org_id, 'id': str(team.id), 'key': team.key,
                     'display_name': payload['display_name'], 'state': 'active', 'version': version}
            digest = _digest(value)
            team.display_name = payload['display_name']
            team.state = RoutingTeam.STATE_ACTIVE
            team.version = version
            team.digest = digest
            team.save(update_fields=['display_name', 'state', 'version', 'digest', 'updated_at'])
        origin = uuid.uuid4()
        _write_outbox(authority_kind='routing_team', aggregate_key=command.aggregate_key,
                      version=version, digest=digest,
                      payload={**value, 'routing_team_id': str(team.id)}, origin_event_id=origin)
        return {'routing_team_id': str(team.id), 'version': version, 'digest': digest}

    if command.command_type in {'activate_subject', 'revoke_subject'}:
        uid, canonical, identity_active = _resolve_identity(payload['subject_email'])
        if uid != payload['subject_id'] or canonical != payload['subject_email']:
            raise RoutingAuthorityError('identity_changed')
        subject = RoutingSubject.objects.select_for_update().filter(
            organization_id=org_id, subject_id=uid).first()
        actual = subject.generation if subject else 0
        if actual != command.expected_version:
            raise RoutingAuthorityError('expected_version_conflict')
        desired_active = command.command_type == 'activate_subject'
        if desired_active and not identity_active:
            raise RoutingAuthorityError('identity_inactive')
        generation = actual + 1
        state = RoutingSubject.STATE_ACTIVE if desired_active else RoutingSubject.STATE_REVOKED
        high_water = (subject.revocation_high_water if subject else 0) + (0 if desired_active else 1)
        value = {'organization_id': org_id, 'subject_id': uid, 'email': canonical,
                 'state': state, 'generation': generation,
                 'revocation_high_water': high_water}
        digest = _digest(value)
        if subject is None:
            subject = RoutingSubject.objects.create(
                organization_id=org_id, subject_id=uid, canonical_email=canonical,
                state=state, generation=generation, digest=digest,
                revocation_high_water=high_water,
            )
        else:
            subject.canonical_email = canonical
            subject.state = state
            subject.generation = generation
            subject.identity_event_id = uuid.uuid4()
            subject.digest = digest
            subject.revocation_high_water = high_water
            subject.lease_until = now if not desired_active else subject.lease_until
            subject.save(update_fields=[
                'canonical_email', 'state', 'generation', 'identity_event_id',
                'digest', 'revocation_high_water', 'lease_until', 'updated_at',
            ])
        # A reviewed local command may activate/revoke the local subject head,
        # but it cannot manufacture an ID-stream freshness lease.  Revocation
        # cuts the existing lease off synchronously; activation remains HOLD
        # until apply_authority_checkpoint receives a causally-bound ID event.
        if not desired_active:
            _invalidate_subject_checkpoint(subject, now=now)
        origin = uuid.uuid4()
        _write_outbox(authority_kind='routing_subject', aggregate_key=command.aggregate_key,
                      version=generation, digest=digest, payload=value,
                      origin_event_id=origin, tombstone=not desired_active)
        return {'subject_id': uid, 'generation': generation, 'digest': digest,
                'checkpoint_generation': None}

    team = _current_team(org_id, payload['team_key'], lock=True)
    uid, canonical, identity_active = _resolve_identity(payload['subject_email'])
    if (uid != payload['subject_id'] or canonical != payload['subject_email']
            or not identity_active):
        raise RoutingAuthorityError('identity_changed')
    source_org_id = int(payload.get('subject_organization_id') or org_id)
    subject = RoutingSubject.objects.select_for_update().filter(
        organization_id=source_org_id, subject_id=uid).first()
    if not team or team.state != RoutingTeam.STATE_ACTIVE:
        raise RoutingAuthorityError('routing_team_missing')
    if not subject or subject.state != RoutingSubject.STATE_ACTIVE:
        raise RoutingAuthorityError('subject_inactive')
    if team.organization_id != org_id:
        raise RoutingAuthorityError('routing_team_org_mismatch')

    if command.command_type in {'activate_binding', 'tombstone_binding'}:
        head = RoutingTeamBinding.objects.select_for_update().filter(
            organization_id=org_id, routing_team=team, subject=subject,
            is_current=True).first()
        actual = head.version if head else 0
        if actual != command.expected_version:
            raise RoutingAuthorityError('expected_version_conflict')
        if head:
            head.is_current = False
            head.save(update_fields=['is_current'])
        version = actual + 1
        state = (RoutingTeamBinding.STATE_ACTIVE if command.command_type == 'activate_binding'
                 else RoutingTeamBinding.STATE_TOMBSTONED)
        value = {'organization_id': org_id, 'routing_team_id': str(team.id),
                 'subject_id': subject.subject_id, 'state': state, 'version': version,
                 'subject_generation': subject.generation}
        digest = _digest(value)
        origin = uuid.uuid4()
        RoutingTeamBinding.objects.create(
            organization_id=org_id, routing_team=team, subject=subject,
            state=state, version=version, subject_generation=subject.generation,
            command_id=command.id, origin_event_id=origin, digest=digest,
        )
        _write_outbox(authority_kind='routing_binding', aggregate_key=command.aggregate_key,
                      version=version, digest=digest, payload=value,
                      origin_event_id=origin,
                      tombstone=state == RoutingTeamBinding.STATE_TOMBSTONED)
        return {'routing_team_id': str(team.id), 'subject_id': subject.subject_id,
                'version': version, 'state': state, 'digest': digest}

    project = None
    if payload['project_key']:
        project = Project.objects.select_for_update().filter(
            org_id=org_id, key__iexact=payload['project_key']).first()
        if project is None:
            raise RoutingAuthorityError('project_missing')
    if source_org_id != org_id and project is None:
        raise RoutingAuthorityError('cross_org_project_required')

    if command.command_type in {
        'grant_cross_org_delegation', 'revoke_cross_org_delegation',
    }:
        if source_org_id == org_id:
            raise RoutingAuthorityError('cross_org_delegation_same_org')
        if project is None:
            raise RoutingAuthorityError('cross_org_project_required')
        scope = dict(
            source_organization_id=source_org_id,
            target_organization_id=org_id,
            subject=subject,
            routing_team=team,
            project=project,
            action=ACTION_RECEIVE_ASSIGNMENT,
        )
        head = CrossOrgAssignmentDelegation.objects.select_for_update().filter(
            **scope, is_current=True).first()
        actual = head.version if head else 0
        if actual != command.expected_version:
            raise RoutingAuthorityError('expected_version_conflict')
        if head:
            head.is_current = False
            head.save(update_fields=['is_current'])
        active = command.command_type == 'grant_cross_org_delegation'
        expiry = _parse_expiry(payload['expires_at'])
        if active and (not expiry or expiry <= now):
            raise RoutingAuthorityError('cross_org_expiry_required')
        version = actual + 1
        revocation_generation = (
            (head.revocation_generation if head else 0) + (0 if active else 1)
        )
        state = (CrossOrgAssignmentDelegation.STATE_ACTIVE if active
                 else CrossOrgAssignmentDelegation.STATE_REVOKED)
        value = {
            'source_organization_id': source_org_id,
            'target_organization_id': org_id,
            'subject_id': subject.subject_id,
            'subject_generation': subject.generation,
            'routing_team_id': str(team.id),
            'project_id': project.id,
            'action': ACTION_RECEIVE_ASSIGNMENT,
            'state': state,
            'version': version,
            'revocation_generation': revocation_generation,
            'expires_at': expiry.isoformat() if expiry else None,
        }
        digest = _digest(value)
        origin = uuid.uuid4()
        delegation = CrossOrgAssignmentDelegation.objects.create(
            **scope,
            state=state,
            version=version,
            subject_generation=subject.generation,
            revocation_generation=revocation_generation,
            expires_at=expiry or now,
            command_id=command.id,
            origin_event_id=origin,
            digest=digest,
        )
        _write_outbox(
            authority_kind='cross_org_delegation',
            aggregate_key=command.aggregate_key,
            version=version,
            digest=digest,
            payload=value,
            origin_event_id=origin,
            tombstone=not active,
        )
        return {
            'delegation_id': delegation.id,
            'version': version,
            'state': state,
            'digest': digest,
        }
    scope = dict(
        organization_id=org_id, subject=subject, routing_team=team, project=project,
        workflow_name=payload['workflow_name'], action=ACTION_RECEIVE_ASSIGNMENT,
    )
    head = ReceiveAssignmentEntitlement.objects.select_for_update().filter(
        **scope, is_current=True).first()
    actual = head.version if head else 0
    if actual != command.expected_version:
        raise RoutingAuthorityError('expected_version_conflict')
    if head:
        head.is_current = False
        head.save(update_fields=['is_current'])
    version = actual + 1
    active = command.command_type == 'grant_receive'
    if active and (not payload['grant_source'] or not payload['grant_event_id']):
        raise RoutingAuthorityError('grant_provenance_required')
    try:
        grant_event_id = uuid.UUID(payload['grant_event_id']) if payload['grant_event_id'] else uuid.uuid4()
    except ValueError:
        raise RoutingAuthorityError('grant_event_id_invalid')
    expiry = _parse_expiry(payload['expires_at'])
    if active and source_org_id != org_id and (not expiry or expiry <= now):
        raise RoutingAuthorityError('cross_org_expiry_required')
    high_water = (head.revocation_high_water if head else 0) + (0 if active else 1)
    state = (ReceiveAssignmentEntitlement.STATE_ACTIVE if active
             else ReceiveAssignmentEntitlement.STATE_REVOKED)
    value = {'organization_id': org_id, 'subject_id': subject.subject_id,
             'routing_team_id': str(team.id), 'project_id': project.id if project else None,
             'workflow_name': payload['workflow_name'], 'action': ACTION_RECEIVE_ASSIGNMENT,
             'state': state, 'version': version, 'subject_generation': subject.generation,
             'grant_source': payload['grant_source'] if active else (head.grant_source if head else 'revocation'),
             'grant_event_id': str(grant_event_id), 'revocation_high_water': high_water,
             'expires_at': expiry.isoformat() if expiry else None}
    digest = _digest(value)
    origin = uuid.uuid4()
    entitlement = ReceiveAssignmentEntitlement.objects.create(
        **scope, state=state, version=version, subject_generation=subject.generation,
        grant_source=value['grant_source'], grant_issuer_id=command.checker_id,
        grant_issuer_email=command.checker_email, grant_event_id=grant_event_id,
        revocation_high_water=high_water, expires_at=expiry, command_id=command.id,
        origin_event_id=origin, digest=digest,
    )
    _write_outbox(authority_kind='receive_entitlement', aggregate_key=command.aggregate_key,
                  version=version, digest=digest, payload=value, origin_event_id=origin,
                  tombstone=not active)
    return {'entitlement_id': entitlement.id, 'version': version, 'state': state,
            'digest': digest}


def approve_and_apply_command(*, actor, command_id, payload_digest: str) -> RoutingAuthorityCommand:
    """Checker approval and command effect linearize in one transaction."""
    with transaction.atomic():
        command = RoutingAuthorityCommand.objects.select_for_update().get(id=command_id)
        if command.state == RoutingAuthorityCommand.STATE_APPLIED:
            if command.payload_digest != payload_digest:
                raise RoutingAuthorityError('approval_payload_conflict')
            return command
        if command.state != RoutingAuthorityCommand.STATE_PROPOSED:
            raise RoutingAuthorityError('command_not_proposed')
        if command.payload_digest != payload_digest:
            raise RoutingAuthorityError('approval_payload_conflict')
        if command.approval_expires_at <= timezone.now():
            raise RoutingAuthorityError('approval_expired')
        checker_id = _actor_id(actor)
        checker_email = _actor_email(actor)
        if checker_id == command.maker_id or checker_email == command.maker_email:
            raise RoutingAuthorityError('maker_checker_same')
        # Revalidate both seats at commit; a maker removed after proposal cannot
        # consume a still-open approval.
        from types import SimpleNamespace
        maker_digest = _require_maker(
            SimpleNamespace(id=command.maker_id, email=command.maker_email), lock=True)
        if maker_digest != command.maker_authority_digest:
            raise RoutingAuthorityError('maker_authority_changed')
        checker_digest = _require_checker(actor, lock=True)
        command.checker_id = checker_id
        command.checker_email = checker_email
        command.checker_authority_digest = checker_digest
        result = _apply_command(command)
        command.state = RoutingAuthorityCommand.STATE_APPLIED
        command.result = result
        command.result_digest = _digest(result)
        command.applied_at = timezone.now()
        command.save(update_fields=[
            'checker_id', 'checker_email', 'checker_authority_digest', 'state',
            'result', 'result_digest', 'applied_at',
        ])
        return command


def refresh_identity_checkpoints(limit=16) -> dict:
    """Observe expired event checkpoints; never synthesize freshness remotely.

    The old worker called ID synchronously and renewed a 90-second lease from
    that lookup.  PLS-277 §16.4 forbids that because a healthy point lookup can
    hide a lost ordered revocation.  This worker is now observability/reconcile
    scheduling only; a fresh event must arrive through apply_authority_checkpoint.
    """
    now = timezone.now()
    limit = max(1, min(int(limit), 16))
    expired = list(RoutingAuthorityCheckpoint.objects.filter(
        authority_id='shizuha-id', lease_until__lte=now,
    ).order_by('lease_until', 'id')[:limit])
    for checkpoint in expired:
        _record_scope_hold(
            checkpoint.authority_id,
            checkpoint.aggregate_key,
            checkpoint.checkpoint_generation,
            'authority_stale',
        )
    reconcile = process_freshness_reconciles(limit=limit)
    return {
        'refreshed': 0,
        'revoked': 0,
        'failed': 0,
        'expired': len(expired),
        **reconcile,
    }


def binding_candidates_for_item(team_name: str, item: Item):
    """Return exact-org binding candidates or ``None`` when the team is legacy.

    ``None`` means no RoutingTeam aggregate exists, so the caller may use the
    legacy roster while the feature is dark.  Once an exact team aggregate
    exists and the gate is armed, missing bindings fail closed—never Admin or
    TeamRoster fallback.
    """
    if item is None or not getattr(item, 'project_id', None):
        return None
    org_id = int(item.project.org_id)
    key = _team_key(team_name)
    team = RoutingTeam.objects.filter(organization_id=org_id, key=key).first()
    gate_enabled = binding_team_enabled(key)
    if team is None:
        # Feature-gate text is not an authority aggregate.  Only the separately
        # reviewed exact-org create_team command can arm a lane; a same-named
        # foreign aggregate or bare gate entry must not suppress this org's
        # TeamRoster compatibility source.
        return None
    if not gate_enabled:
        return {'team': team, 'candidates': [], 'reason': 'routing_authority_gate_off'}
    if team.state != RoutingTeam.STATE_ACTIVE:
        return {'team': team, 'candidates': [], 'reason': 'routing_team_tombstoned'}
    rows = RoutingTeamBinding.objects.filter(
        organization_id=org_id, routing_team=team, is_current=True,
        state=RoutingTeamBinding.STATE_ACTIVE,
        subject__state=RoutingSubject.STATE_ACTIVE,
    ).select_related('subject').order_by('subject__subject_id')
    candidates = [
        {'user_id': row.subject.subject_id, 'email': row.subject.canonical_email,
         'subject_id': row.subject.subject_id, 'binding_version': row.version,
         'subject_generation': row.subject_generation}
        for row in rows if row.subject_generation == row.subject.generation
    ]
    return {'team': team, 'candidates': candidates,
            'reason': '' if candidates else 'routing_binding_missing'}


def routing_candidate_snapshot_for_item(team_name: str, item: Item) -> dict:
    """Return the versioned candidate-discovery source for one exact item lane.

    This is the shared detector/resolver boundary.  An armed or materialized
    ``RoutingTeam`` is sticky and never falls back to legacy ``TeamRoster``.
    Until a lane has been materialized, the compatibility source is only the
    exact ``(project.organization_id, canonical team)`` roster row; the global
    flattened roster and Admin directory are deliberately excluded.
    """
    from ..models import TeamRoster
    from .workflow_service import WorkflowService

    key = _team_key(team_name)
    project = getattr(item, 'project', None) if item is not None else None
    org_id = getattr(project, 'org_id', None) if project is not None else None
    if org_id is None:
        return {
            'organization_id': None,
            'organization_slug': '',
            'team_key': key,
            'source': 'authority_unknown',
            'source_version': None,
            'candidates': [],
            'tiers': None,
            'reason': 'project_organization_missing',
        }
    org_id = int(org_id)
    binding = binding_candidates_for_item(key, item)
    if binding is not None:
        team = binding.get('team')
        candidates = list(binding.get('candidates') or [])
        # Version provenance must survive a rollback.  Looking only at ACTIVE
        # candidates loses the tombstone version and makes detector evidence
        # appear to rewind to the RoutingTeam head.  Current heads include the
        # reviewed tombstone while candidate discovery remains ACTIVE-only.
        binding_heads = list(
            RoutingTeamBinding.objects.filter(
                organization_id=org_id,
                routing_team=team,
                is_current=True,
            ).order_by('subject_id').values('version', 'state')
        ) if team is not None else []
        binding_versions = [
            f"{int(row['version'])}:{row['state']}" for row in binding_heads
        ]
        return {
            'organization_id': org_id,
            'organization_slug': WorkflowService.org_slug_for_project(project),
            'team_key': key,
            'source': 'routing_binding',
            'source_version': (
                f'team:{team.version};bindings:{",".join(binding_versions) or "none"}'
                if team is not None else 'team:missing'
            ),
            'routing_team_id': str(team.id) if team is not None else None,
            'routing_team_version': team.version if team is not None else None,
            'candidates': candidates,
            'tiers': [[row['email'] for row in candidates]],
            'reason': binding.get('reason') or '',
        }

    row = TeamRoster.objects.filter(
        organization_id=org_id,
        team_name=key,
        is_active=True,
    ).first()
    # PLS-1080: TeamRoster rows are commonly authored by slug
    # (``organization_slug='shizuha'``) and may carry a NULL ``organization_id``
    # (the PLS-1069-documented shape). The resolver must resolve the SAME row the
    # assignment guard validates (``_team_roster_entry_for_item`` keys by
    # organization_slug first, then id). Without this slug fallback the resolver
    # saw ``tiers=None`` for a slug-only row while the guard found the row — the
    # two sides diverged on the exact engineering roster. Mirror the guard's
    # slug-first lookup so a resolved assignee is always a member of the row the
    # guard validates.
    if row is None:
        slug = WorkflowService.org_slug_for_project(project)
        if slug:
            row = TeamRoster.objects.filter(
                organization_slug=slug,
                team_name=key,
                is_active=True,
            ).first()
    tiers = row.tiers if row is not None else None
    members = WorkflowService._flat_roster(tiers or [])
    return {
        'organization_id': org_id,
        'organization_slug': (
            (row.organization_slug or '').strip().lower()
            if row is not None else WorkflowService.org_slug_for_project(project)
        ),
        'team_key': key,
        'source': 'team_roster_compat',
        'source_version': (
            f'row:{row.pk};updated:{row.updated_at.isoformat()}'
            if row is not None else 'row:missing'
        ),
        'routing_team_id': None,
        'routing_team_version': None,
        'candidates': [{'email': email} for email in members],
        'tiers': tiers,
        'reason': (
            '' if members else
            ('team_roster_empty' if row is not None else 'team_roster_missing')
        ),
    }


def _freshness_vector(*, item, project, workflow, team, binding, subject,
                      entitlement, checkpoint, delegation=None):
    """Canonical complete local-head/checkpoint vector for one decision."""
    vector = {
        'policy_manifest_digest': RECEIVE_ASSIGNMENT_POLICY_DIGEST,
        'item': {
            'id': item.id,
            'project_id': item.project_id,
            'assignment_group': _team_key(item.assignment_group),
            'workflow_name': (item.workflow_name or '').strip(),
            'updated_at': item.updated_at.isoformat() if item.updated_at else None,
        },
        'project': {
            'id': project.id,
            'organization_id': project.org_id,
            'access_level': project.access_level,
            'is_active': project.is_active,
            'updated_at': project.updated_at.isoformat() if project.updated_at else None,
        },
        'workflow': {
            'id': str(workflow.id) if workflow else None,
            'slug': workflow.slug if workflow else (item.workflow_name or '').strip(),
            'is_active': workflow.is_active if workflow else None,
            'updated_at': (workflow.updated_at.isoformat()
                           if workflow and workflow.updated_at else None),
        },
        'routing_team': {
            'id': str(team.id), 'version': team.version, 'digest': team.digest,
        },
        'routing_binding': {
            'id': binding.id, 'version': binding.version, 'digest': binding.digest,
            'subject_generation': binding.subject_generation,
        },
        'routing_subject': {
            'id': str(subject.id), 'generation': subject.generation,
            'digest': subject.digest,
            'revocation_high_water': subject.revocation_high_water,
        },
        'receive_entitlement': {
            'id': entitlement.id, 'version': entitlement.version,
            'digest': entitlement.digest,
            'subject_generation': entitlement.subject_generation,
            'revocation_high_water': entitlement.revocation_high_water,
        },
        'shizuha-id': {
            'aggregate_key': checkpoint.aggregate_key,
            'subject_generation': checkpoint.subject_generation,
            'observed_version': checkpoint.observed_version,
            'digest': checkpoint.digest,
            'revocation_high_water': checkpoint.revocation_high_water,
            'checkpoint_generation': checkpoint.checkpoint_generation,
            'source_event_id': (str(checkpoint.source_event_id)
                                if checkpoint.source_event_id else None),
            'first_received_at': (checkpoint.first_received_at.isoformat()
                                  if checkpoint.first_received_at else None),
            'source_expires_at': (checkpoint.source_expires_at.isoformat()
                                  if checkpoint.source_expires_at else None),
            'observed_at': checkpoint.observed_at.isoformat(),
            'lease_until': checkpoint.lease_until.isoformat(),
        },
        'revocation_high_water': max(
            subject.revocation_high_water,
            entitlement.revocation_high_water,
            checkpoint.revocation_high_water,
            delegation.revocation_generation if delegation else 0,
        ),
    }
    if delegation:
        vector['cross_org_delegation'] = {
            'id': delegation.id,
            'source_organization_id': delegation.source_organization_id,
            'target_organization_id': delegation.target_organization_id,
            'project_id': delegation.project_id,
            'action': delegation.action,
            'version': delegation.version,
            'state': delegation.state,
            'subject_generation': delegation.subject_generation,
            'revocation_generation': delegation.revocation_generation,
            'expires_at': delegation.expires_at.isoformat(),
            'digest': delegation.digest,
        }
    return vector, _digest(vector)


def _record_scope_hold(authority_id, aggregate_key, checkpoint_generation,
                       reason, *, owner_group='devops'):
    """Persist one observable HOLD/reconcile signal per exact scope/generation."""
    authority_id = str(authority_id or 'unknown')[:64]
    aggregate_key = str(aggregate_key or '__unknown__')[:255]
    generation = max(0, int(checkpoint_generation or 0))
    dedupe = _digest({
        'authority_id': authority_id,
        'aggregate_key': aggregate_key,
        'checkpoint_generation': generation,
        'reason': reason,
    })
    now = timezone.now()
    signal, _ = RoutingAuthorityHoldSignal.objects.get_or_create(
        dedupe_key=dedupe,
        defaults={
            'authority_id': authority_id,
            'aggregate_key': aggregate_key,
            'checkpoint_generation': generation,
            'reason': str(reason)[:100],
            'owner_group': owner_group,
            'next_attempt_at': now,
        },
    )
    return signal


def claim_freshness_reconciles(limit=16, *, now=None):
    """Distributed per-scope claim, capped at 16 per worker invocation."""
    now = now or timezone.now()
    limit = max(1, min(int(limit), 16))
    with transaction.atomic():
        candidates = list(RoutingAuthorityHoldSignal.objects.select_for_update(
            skip_locked=True).filter(
                resolved_at__isnull=True,
                next_attempt_at__lte=now,
            ).filter(
                Q(in_flight=False) | Q(in_flight_until__lte=now)
            ).order_by('next_attempt_at', 'id')[:max(16, limit * 4)])
        rows = []
        for row in candidates:
            scope = f'{row.authority_id}:{row.aggregate_key}'
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))',
                        [scope],
                    )
                    if not cursor.fetchone()[0]:
                        continue
            scope_busy = RoutingAuthorityHoldSignal.objects.filter(
                authority_id=row.authority_id,
                aggregate_key=row.aggregate_key,
                resolved_at__isnull=True,
                in_flight=True,
                in_flight_until__gt=now,
            ).exclude(pk=row.pk).exists()
            if scope_busy:
                continue
            row.in_flight = True
            row.in_flight_until = now + timedelta(seconds=30)
            row.save(update_fields=['in_flight', 'in_flight_until', 'updated_at'])
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows


def finish_freshness_reconcile(signal_id, *, success, error=''):
    """Finish one claim with full-jitter 30s→5m bounded backoff."""
    with transaction.atomic():
        row = RoutingAuthorityHoldSignal.objects.select_for_update().get(pk=signal_id)
        now = timezone.now()
        row.in_flight = False
        row.in_flight_until = None
        if success:
            row.resolved_at = now
            row.last_error = ''
        else:
            row.attempts += 1
            cap = min(300, 30 * (2 ** max(0, row.attempts - 1)))
            row.next_attempt_at = now + timedelta(seconds=random.uniform(0, cap))
            row.last_error = str(error or 'reconcile_failed')[:1000]
        row.save(update_fields=[
            'in_flight', 'in_flight_until', 'resolved_at', 'attempts',
            'next_attempt_at', 'last_error', 'updated_at',
        ])
        return row


def _routing_hold_recipient(owner_group, organization_id=None):
    """Resolve one current accountable lead; never broadcast to a roster."""
    from .workflow_service import WorkflowService

    organization_slug = ''
    if organization_id is not None:
        try:
            from .admin_service import admin_service
            org = admin_service.get_organization(int(organization_id)) or {}
            organization_slug = str(org.get('slug') or '').strip().lower()
        except Exception:
            organization_slug = ''
    if organization_id is not None and not organization_slug:
        return None
    email = WorkflowService._resolve_team_lead_email(
        owner_group,
        organization_slug=organization_slug or None,
    )
    return str(email or '').strip().lower() or None


def _notify_routing_authority_hold(signal) -> tuple[bool, str]:
    """Send the deduplicated HOLD to the exact owning lead via Connect."""
    if signal.notified_at:
        return True, signal.notification_recipient
    try:
        import requests as req
        from django.conf import settings
        from .connect_origin import system_message_headers
        from ..user_service import user_service

        email = _routing_hold_recipient(
            signal.owner_group,
            organization_id=signal.organization_id,
        )
        info = user_service.get_user_by_email(email) if email else None
        user_id = info.get('id') if isinstance(info, dict) else None
        if not user_id:
            return False, email or ''
        content = (
            '[Routing Authority HOLD] assignment authority cannot be proven\n'
            f'- authority: `{signal.authority_id}`\n'
            f'- aggregate: `{signal.aggregate_key}`\n'
            f'- checkpoint generation: `{signal.checkpoint_generation}`\n'
            f'- reason: `{signal.reason}`\n'
            f'- owner group: `{signal.owner_group}`\n'
            'Assignments remain fail-closed for this exact scope. Restore the '
            'ordered causal checkpoint or relay, then verify this HOLD clears; '
            'do not substitute a synchronous lookup or legacy roster.'
        )
        body = {
            'recipient_user_id': int(user_id),
            'content': content,
            'sender_name': 'Pulse Routing Authority',
            'sender_service': 'shizuha-pulse',
        }
        connect_base_url = getattr(
            settings, 'SHIZUHA_CONNECT_API_URL',
            'http://shizuha-connect:8019/api',
        ).rstrip('/')
        response = req.post(
            f'{connect_base_url}/internal/system-message/',
            json=body,
            headers=system_message_headers(body),
            timeout=5,
        )
        if not 200 <= getattr(response, 'status_code', 0) < 300:
            return False, email
        RoutingAuthorityHoldSignal.objects.filter(
            pk=signal.pk, notified_at__isnull=True,
        ).update(
            notified_at=timezone.now(),
            notification_recipient=email,
        )
        return True, email
    except Exception:
        return False, ''


def process_freshness_reconciles(limit=16) -> dict:
    """Bounded fail-loud worker for exact-scope freshness HOLDs.

    Claims are distributed and per-row, capped at sixteen.  Alert delivery is
    deduplicated by ``notified_at``; waiting for the next causal event uses
    full-jitter 30s->5m backoff and never clears the HOLD itself.
    """
    rows = claim_freshness_reconciles(limit=limit)
    notified = failed = 0
    for row in rows:
        delivered, _ = _notify_routing_authority_hold(row)
        if delivered:
            notified += 1
            error = 'awaiting_causal_checkpoint'
        else:
            failed += 1
            error = 'owner_notification_failed'
        finish_freshness_reconcile(row.pk, success=False, error=error)
    return {'claimed': len(rows), 'notified': notified, 'notify_failed': failed}


def render_routing_authority_metrics() -> str:
    """Prometheus liveness for HOLD, quarantine and relay mechanisms."""
    now = timezone.now()
    signals = list(RoutingAuthorityHoldSignal.objects.filter(
        resolved_at__isnull=True,
    ).values('authority_id', 'reason', 'owner_group', 'created_at', 'in_flight'))
    grouped = {}
    oldest = 0.0
    in_flight = 0
    for row in signals:
        key = (row['authority_id'], row['reason'], row['owner_group'])
        grouped[key] = grouped.get(key, 0) + 1
        oldest = max(oldest, max(0.0, (now - row['created_at']).total_seconds()))
        in_flight += int(bool(row['in_flight']))
    lines = [
        '# HELP pulse_routing_authority_hold_scopes Unresolved exact-scope routing authority HOLDs.',
        '# TYPE pulse_routing_authority_hold_scopes gauge',
    ]
    for (authority, reason, owner), count in sorted(grouped.items()):
        labels = (
            f'authority="{authority}",reason="{reason}",owner_group="{owner}"'
        )
        lines.append(f'pulse_routing_authority_hold_scopes{{{labels}}} {count}')
    lines.extend([
        '# HELP pulse_routing_authority_hold_oldest_seconds Age of the oldest unresolved HOLD.',
        '# TYPE pulse_routing_authority_hold_oldest_seconds gauge',
        f'pulse_routing_authority_hold_oldest_seconds {oldest:.3f}',
        '# HELP pulse_routing_authority_reconcile_in_flight Distributed HOLD reconciles currently claimed.',
        '# TYPE pulse_routing_authority_reconcile_in_flight gauge',
        f'pulse_routing_authority_reconcile_in_flight {in_flight}',
        '# HELP pulse_routing_authority_quarantines Unrecovered poison events.',
        '# TYPE pulse_routing_authority_quarantines gauge',
        'pulse_routing_authority_quarantines '
        f'{RoutingAuthorityQuarantine.objects.filter(recovered_at__isnull=True).count()}',
        '# HELP pulse_routing_authority_outbox_pending Unacknowledged routing authority events.',
        '# TYPE pulse_routing_authority_outbox_pending gauge',
        'pulse_routing_authority_outbox_pending '
        f'{RoutingAuthorityOutbox.objects.filter(acknowledged_at__isnull=True, quarantined_at__isnull=True).count()}',
        '# HELP pulse_routing_authority_outbox_quarantined Poison relay events.',
        '# TYPE pulse_routing_authority_outbox_quarantined gauge',
        'pulse_routing_authority_outbox_quarantined '
        f'{RoutingAuthorityOutbox.objects.filter(quarantined_at__isnull=False).count()}',
    ])
    return '\n'.join(lines) + '\n'


def _arm_assignment_receipt(receipt: RoutingAssignmentReceipt):
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('pulse.routing_assignment_receipt', %s, true)",
                [str(receipt.id)],
            )
    return receipt


def prepare_assignment_write_receipt(item: Item, decision: AuthorizationDecision):
    """Create and arm the exact one-use receipt consumed by the DB trigger."""
    project = Project.objects.filter(pk=item.project_id).only('id', 'org_id').first()
    group = _team_key(item.assignment_group)
    if (not decision.allowed or not item.pk or decision.item_id != item.pk
            or not project or decision.organization_id != project.org_id
            or decision.project_id != project.id
            or not decision.routing_team_id or not group
            or _team_key(decision.assignment_group) != group
            or (decision.workflow_name or '').strip()
               != (item.workflow_name or '').strip()):
        raise RoutingAuthorityError('assignment_receipt_decision_invalid')
    receipt = RoutingAssignmentReceipt.objects.create(
        action=RoutingAssignmentReceipt.ACTION_ASSIGN,
        reason=ACTION_RECEIVE_ASSIGNMENT,
        item=item,
        organization_id=project.org_id,
        project=project,
        routing_team_id=decision.routing_team_id,
        assignment_group=group,
        workflow_name=(item.workflow_name or '').strip(),
        subject_id=decision.subject_id,
        assignee_email=(item.assignee_email or '').strip().lower(),
        binding_version=decision.binding_version,
        entitlement_version=decision.entitlement_version,
        delegation_version=decision.delegation_version,
        subject_generation=decision.subject_generation,
        revocation_high_water=decision.revocation_high_water,
        freshness_vector_digest=decision.freshness_vector_digest,
        result_organization_id=project.org_id,
        result_project=project,
        result_routing_team_id=decision.routing_team_id,
        result_assignment_group=group,
        result_workflow_name=(item.workflow_name or '').strip(),
        expires_at=timezone.now() + timedelta(seconds=30),
    )
    return _arm_assignment_receipt(receipt)


def _assignment_clear_decision(
        item: Item, *, reason: str, action: str, lock: bool,
        result_project_id=None, result_assignment_group=None,
        result_workflow_name=None):
    """Snapshot the exact OLD owner/lane and exact no-owner result tuple.

    ``Item.save`` always re-locks and revalidates this snapshot.  The lock here
    keeps the public ``save=True`` command linear from intent through receipt;
    batched ``save=False`` workflow writers carry the immutable snapshot until
    that same locked model boundary executes.
    """
    reason = str(reason or '').strip().lower()
    if reason not in CLEAR_ASSIGNMENT_REASONS:
        raise RoutingAuthorityError('assignment_clear_reason_invalid')
    if not getattr(item, 'pk', None):
        return None
    rows = Item.objects
    if lock:
        rows = rows.select_for_update()
    old = rows.filter(pk=item.pk).values(
        'id', 'project_id', 'assignment_group', 'workflow_name',
        'assignee_id', 'assignee_email',
    ).first()
    if not old:
        raise RoutingAuthorityError('assignment_clear_item_missing')
    group = _team_key(old['assignment_group'])
    email = (old['assignee_email'] or '').strip().lower()
    if not old['assignee_id'] and not email:
        return None
    if not old['project_id']:
        return None

    old_project = Project.objects.only('id', 'org_id').get(pk=old['project_id'])
    old_team = RoutingTeam.objects.filter(
        organization_id=old_project.org_id, key=group,
    ).only('id').first()
    target_project_id = int(result_project_id or old_project.id)
    target_project = Project.objects.only('id', 'org_id').filter(
        pk=target_project_id,
    ).first()
    if target_project is None:
        raise RoutingAuthorityError('assignment_clear_result_project_missing')
    target_group = _team_key(
        old['assignment_group']
        if result_assignment_group is None else result_assignment_group
    )
    target_workflow = (
        (old['workflow_name'] or '').strip()
        if result_workflow_name is None else str(result_workflow_name or '').strip()
    )
    target_team = RoutingTeam.objects.filter(
        organization_id=target_project.org_id, key=target_group,
    ).only('id').first() if target_group else None
    action = str(action or '').strip().lower()
    old_context = (old_project.id, group, (old['workflow_name'] or '').strip())
    result_context = (target_project.id, target_group, target_workflow)
    if action == RoutingAssignmentReceipt.ACTION_CLEAR:
        if result_context != old_context:
            raise RoutingAuthorityError('assignment_clear_action_context_invalid')
    elif action == RoutingAssignmentReceipt.ACTION_UNASSIGN:
        if (target_group or target_project.id != old_project.id
                or target_workflow != old_context[2]):
            raise RoutingAuthorityError('assignment_unassign_result_invalid')
    elif action == RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE:
        # A reroute to the SAME lane is still a real command when it clears a
        # concrete assignee (the lane stays, the concrete owner is removed) —
        # e.g. a transition post-function rerouting a task that is already in
        # the correct group queue back to that queue to drop the assignee.
        # Only a true no-op (same lane AND no assignee to clear) is invalid.
        if not target_group:
            raise RoutingAuthorityError('assignment_reroute_result_invalid')
        if (result_context == old_context
                and not old['assignee_id'] and not email):
            raise RoutingAuthorityError('assignment_reroute_result_invalid')
    else:
        raise RoutingAuthorityError('assignment_clear_action_invalid')

    # A context with no exact RoutingTeam on either side is outside the armed
    # authority boundary.  Preserve legacy behavior without minting a receipt.
    if old_team is None and target_team is None:
        return None
    return AssignmentClearDecision(
        action=action,
        item_id=old['id'],
        organization_id=old_project.org_id,
        project_id=old_project.id,
        routing_team_id=str(old_team.id) if old_team else None,
        assignment_group=group,
        workflow_name=(old['workflow_name'] or '').strip(),
        subject_id=old['assignee_id'],
        assignee_email=email,
        result_organization_id=target_project.org_id,
        result_project_id=target_project.id,
        result_routing_team_id=str(target_team.id) if target_team else None,
        result_assignment_group=target_group,
        result_workflow_name=target_workflow,
        reason=reason,
    )


def prepare_assignment_clear_receipt(item: Item, decision: AssignmentClearDecision):
    """Arm a typed one-use receipt for exact OLD owner -> exact no-owner tuple."""
    project = Project.objects.filter(pk=item.project_id).only('id', 'org_id').first()
    group = _team_key(item.assignment_group)
    workflow_name = (item.workflow_name or '').strip()
    if (not project or item.pk != decision.item_id
            or project.id != decision.result_project_id
            or project.org_id != decision.result_organization_id
            or group != decision.result_assignment_group
            or workflow_name != decision.result_workflow_name
            or item.assignee_id is not None
            or (item.assignee_email or '').strip()):
        raise RoutingAuthorityError('assignment_clear_receipt_decision_invalid')
    context_digest = _digest({
        'action': decision.action,
        'item_id': decision.item_id,
        'organization_id': decision.organization_id,
        'project_id': decision.project_id,
        'routing_team_id': decision.routing_team_id,
        'assignment_group': decision.assignment_group,
        'workflow_name': decision.workflow_name,
        'old_subject_id': decision.subject_id,
        'old_assignee_email': decision.assignee_email,
        'result_organization_id': decision.result_organization_id,
        'result_project_id': decision.result_project_id,
        'result_routing_team_id': decision.result_routing_team_id,
        'result_assignment_group': decision.result_assignment_group,
        'result_workflow_name': decision.result_workflow_name,
        'result_assignee_id': None,
        'result_assignee_email': '',
        'reason': decision.reason,
    })
    receipt = RoutingAssignmentReceipt.objects.create(
        action=decision.action,
        reason=decision.reason,
        item=item,
        organization_id=decision.organization_id,
        project_id=decision.project_id,
        routing_team_id=decision.routing_team_id,
        assignment_group=decision.assignment_group,
        workflow_name=decision.workflow_name,
        # On a clear receipt these identify the locked OLD concrete owner.
        subject_id=decision.subject_id,
        assignee_email=decision.assignee_email,
        binding_version=0,
        entitlement_version=0,
        delegation_version=None,
        subject_generation=0,
        revocation_high_water=0,
        freshness_vector_digest=context_digest,
        result_organization_id=decision.result_organization_id,
        result_project_id=decision.result_project_id,
        result_routing_team_id=decision.result_routing_team_id,
        result_assignment_group=decision.result_assignment_group,
        result_workflow_name=decision.result_workflow_name,
        expires_at=timezone.now() + timedelta(seconds=30),
    )
    return _arm_assignment_receipt(receipt)


def emit_assignment_clear_thread_receipt(item: Item, proof) -> None:
    """Thread-visible receipt for a persisted concrete-owner clear (PLAT-10496).

    RoutingAssignmentReceipt rows are DB-only. Force-assign notifications cite
    intent, then a later clear nulls the assignee with no comment, so a reader
    of the latest force-assign line believes an owner the live object lacks.
    Called from the clear command while its atomic block is still open, and
    writes the comment in that same transaction. A rollback therefore cannot
    leave a false receipt. Deduped by origin_event_id if the same proof retries.
    """
    prior = str(getattr(proof, 'assignee_email', '') or '').strip()
    if not prior or not getattr(item, 'pk', None):
        return
    reason = str(getattr(proof, 'reason', '') or '').strip() or 'unspecified'
    action = str(getattr(proof, 'action', '') or '').strip() or 'clear'
    result_group = str(getattr(proof, 'result_assignment_group', '') or '').strip()
    event_id = (
        f"assignment.cleared:{item.pk}:{prior}:{action}:{reason}:{result_group}"
    )[:255]

    from .mechanical_comments import MechanicalCommentFactory
    MechanicalCommentFactory.create(
        item=item,
        origin_kind='WORKFLOW_LIFECYCLE',
        origin_event='assignment.cleared',
        template_version=1,
        structured_payload={
            'prior_email': prior,
            'reason': reason,
            'action': action,
            'result_group': result_group,
        },
        origin_event_id=event_id,
    )


def _apply_assignment_clear_command(
        item: Item, *, reason: str, action: str, save: bool,
        result_project_id=None, result_assignment_group=None,
        result_workflow_name=None):
    """Apply one reviewed clear action and carry its immutable OLD/result proof."""
    def apply(*, lock):
        decision = _assignment_clear_decision(
            item,
            reason=reason,
            action=action,
            lock=lock,
            result_project_id=result_project_id,
            result_assignment_group=result_assignment_group,
            result_workflow_name=result_workflow_name,
        )
        if decision is not None:
            item._routing_authority_clear_decision = decision
            target_project_id = decision.result_project_id
            target_group = decision.result_assignment_group
            target_workflow = decision.result_workflow_name
        else:
            target_project_id = (
                int(result_project_id)
                if result_project_id is not None else item.project_id
            )
            target_group = _team_key(
                item.assignment_group
                if result_assignment_group is None else result_assignment_group
            )
            target_workflow = (
                (item.workflow_name or '').strip()
                if result_workflow_name is None
                else str(result_workflow_name or '').strip()
            )
        changed = bool(
            decision is not None
            or item.assignee_id
            or (item.assignee_email or '').strip()
            or item.project_id != target_project_id
            or _team_key(item.assignment_group) != target_group
            or (item.workflow_name or '').strip() != target_workflow
        )
        item.project_id = target_project_id
        item.assignment_group = target_group or None
        item.workflow_name = target_workflow
        item.assignee_id = None
        item.assignee_email = None
        if save and changed:
            item.save(update_fields=[
                'project', 'assignment_group', 'workflow_name',
                'assignee_id', 'assignee_email',
            ])
            if decision is not None:
                emit_assignment_clear_thread_receipt(item, decision)
        return changed

    if save:
        with transaction.atomic():
            return apply(lock=True)
    return apply(lock=False)


def clear_assignment_to_group_queue(
        item: Item, *, reason: str = 'explicit_unassign', save: bool = True):
    """Explicit command for concrete-owner -> same-lane group-queued HOLD.

    Bare ``save()``, QuerySet, bulk, and raw SQL clears never receive this
    command snapshot or its transaction-local receipt and remain denied.
    """
    return _apply_assignment_clear_command(
        item,
        reason=reason,
        action=RoutingAssignmentReceipt.ACTION_CLEAR,
        save=save,
    )


def unassign_assignment(item: Item, *, save: bool = True):
    """Public explicit-unassign command: exact OLD tuple -> no routing group."""
    return _apply_assignment_clear_command(
        item,
        reason='explicit_unassign',
        action=RoutingAssignmentReceipt.ACTION_UNASSIGN,
        save=save,
        result_assignment_group='',
    )


def reroute_assignment_to_group_queue(
        item: Item, assignment_group: str, *, reason: str = 'no_candidate',
        save: bool = True):
    """Reviewed exact-destination group-queue command with no concrete owner."""
    return _apply_assignment_clear_command(
        item,
        reason=reason,
        action=RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE,
        save=save,
        result_assignment_group=assignment_group,
    )


def _assignment_direct_handoff_decision(
        item: Item, *, target_subject_id, target_email, reason: str, lock: bool):
    """Snapshot the exact OLD guarded lane and exact NEW owner/lane for an
    authorized direct handoff (PLS-539 / hiro ADR-0010).

    Reads the OLD lane from the row-locked DB state and the NEW owner/lane from the
    caller's pending in-memory ``item`` (set by the entrypoint after its reason gate
    + task-org authorization). Returns None — deferring to the normal write path,
    NO direct-handoff receipt — unless the OLD lane is a materialized guarded lane
    (``old_team`` present): only leaving a guarded lane needs this crossing. The
    fence revalidates the returned tuple under its own lock before arming a receipt.
    """
    # A reason is NOT required to snapshot the crossing: an operator direct
    # override is reason-exempt (the entrypoint's reason gate already authorized
    # the handoff and only then set _pls539_direct_override, which is what gates
    # the mint). Reason is recorded on the receipt when present, for audit.
    reason = str(reason or '').strip()
    if not getattr(item, 'pk', None):
        return None
    target_email = (target_email or '').strip().lower()
    if not target_subject_id and not target_email:
        return None
    rows = Item.objects
    if lock:
        rows = rows.select_for_update()
    old = rows.filter(pk=item.pk).values(
        'id', 'project_id', 'assignment_group', 'workflow_name',
    ).first()
    if not old or not old['project_id']:
        return None
    old_group = _team_key(old['assignment_group'])
    old_project = Project.objects.only('id', 'org_id').filter(
        pk=old['project_id']).first()
    if old_project is None:
        return None
    old_team = RoutingTeam.objects.filter(
        organization_id=old_project.org_id, key=old_group,
    ).only('id').first() if old_group else None
    # Only an OLD materialized guarded lane needs the crossing receipt; otherwise
    # the ordinary (unguarded) write path already permits the assignment.
    if old_team is None:
        return None
    # NEW owner/lane come from the pending item the entrypoint is about to save.
    new_group = _team_key(item.assignment_group)
    new_project = Project.objects.only('id', 'org_id').filter(
        pk=item.project_id).first()
    if new_project is None:
        return None
    new_team = RoutingTeam.objects.filter(
        organization_id=new_project.org_id, key=new_group,
    ).only('id').first() if new_group else None
    return AssignmentDirectHandoffDecision(
        item_id=old['id'],
        organization_id=old_project.org_id,
        project_id=old_project.id,
        routing_team_id=str(old_team.id),
        assignment_group=old_group,
        workflow_name=(old['workflow_name'] or '').strip(),
        subject_id=int(target_subject_id) if target_subject_id else None,
        assignee_email=target_email,
        result_organization_id=new_project.org_id,
        result_project_id=new_project.id,
        result_routing_team_id=str(new_team.id) if new_team else None,
        result_assignment_group=new_group,
        result_workflow_name=(item.workflow_name or '').strip(),
        reason=reason,
    )


def prepare_assignment_direct_handoff_receipt(
        item: Item, decision: AssignmentDirectHandoffDecision):
    """Arm the one-use receipt for an authorized OLD guarded lane -> NEW owner/lane
    crossing, consumed by the trigger's ``direct_handoff`` branch."""
    project = Project.objects.filter(pk=item.project_id).only('id', 'org_id').first()
    new_group = _team_key(item.assignment_group)
    workflow_name = (item.workflow_name or '').strip()
    email = (item.assignee_email or '').strip().lower()
    # The NEW state on the item MUST equal the decision's result owner + lane.
    if (not project or item.pk != decision.item_id
            or project.id != decision.result_project_id
            or project.org_id != decision.result_organization_id
            or new_group != decision.result_assignment_group
            or workflow_name != decision.result_workflow_name
            or item.assignee_id != decision.subject_id
            or email != decision.assignee_email
            or not (item.assignee_id or email)):
        raise RoutingAuthorityError('assignment_direct_handoff_receipt_decision_invalid')
    context_digest = _digest({
        'action': RoutingAssignmentReceipt.ACTION_DIRECT_HANDOFF,
        'item_id': decision.item_id,
        'organization_id': decision.organization_id,
        'project_id': decision.project_id,
        'routing_team_id': decision.routing_team_id,
        'assignment_group': decision.assignment_group,
        'workflow_name': decision.workflow_name,
        'new_subject_id': decision.subject_id,
        'new_assignee_email': decision.assignee_email,
        'result_organization_id': decision.result_organization_id,
        'result_project_id': decision.result_project_id,
        'result_routing_team_id': decision.result_routing_team_id,
        'result_assignment_group': decision.result_assignment_group,
        'result_workflow_name': decision.result_workflow_name,
        'reason': decision.reason,
    })
    receipt = RoutingAssignmentReceipt.objects.create(
        action=RoutingAssignmentReceipt.ACTION_DIRECT_HANDOFF,
        reason=decision.reason[:100],
        item=item,
        # OLD guarded lane being left.
        organization_id=decision.organization_id,
        project_id=decision.project_id,
        routing_team_id=decision.routing_team_id,
        assignment_group=decision.assignment_group,
        workflow_name=decision.workflow_name,
        # NEW concrete owner who lands.
        subject_id=decision.subject_id,
        assignee_email=decision.assignee_email,
        binding_version=0,
        entitlement_version=0,
        delegation_version=None,
        subject_generation=0,
        revocation_high_water=0,
        freshness_vector_digest=context_digest,
        # NEW (possibly unguarded) result lane.
        result_organization_id=decision.result_organization_id,
        result_project_id=decision.result_project_id,
        result_routing_team_id=decision.result_routing_team_id,
        result_assignment_group=decision.result_assignment_group,
        result_workflow_name=decision.result_workflow_name,
        expires_at=timezone.now() + timedelta(seconds=30),
    )
    return _arm_assignment_receipt(receipt)


def apply_direct_handoff_command(item: Item, *, target_subject_id, target_email,
                                 reason: str):
    """Entrypoint-facing command: after the reason gate + task-org authorization
    pass, snapshot the authorized crossing and carry it into ``Item.save`` as the
    ONE typed intent signal the fence keys on. No-op (leaves no signal) when the
    OLD lane is not guarded — the ordinary write path handles that.
    """
    decision = _assignment_direct_handoff_decision(
        item, target_subject_id=target_subject_id, target_email=target_email,
        reason=reason, lock=True,
    )
    if decision is not None:
        item._routing_authority_direct_handoff = decision
    return decision


def _deny(reason: str, *, item=None, subject=None, team=None, binding=None,
          entitlement=None, checkpoint=None, delegation=None,
          freshness_vector_digest=None) -> AuthorizationDecision:
    decision = AuthorizationDecision(
        allowed=False,
        reason=reason,
        subject_id=getattr(subject, 'subject_id', None),
        item_id=getattr(item, 'id', None),
        organization_id=getattr(getattr(item, 'project', None), 'org_id', None),
        project_id=getattr(item, 'project_id', None),
        routing_team_id=str(team.id) if team else None,
        assignment_group=_team_key(getattr(item, 'assignment_group', '')),
        workflow_name=(getattr(item, 'workflow_name', '') or '').strip(),
        binding_version=getattr(binding, 'version', None),
        entitlement_version=getattr(entitlement, 'version', None),
        subject_generation=getattr(subject, 'generation', None),
        revocation_high_water=max(
            getattr(subject, 'revocation_high_water', 0) or 0,
            getattr(entitlement, 'revocation_high_water', 0) or 0,
        ),
        grant_source=getattr(entitlement, 'grant_source', None),
        grant_event_id=(str(entitlement.grant_event_id) if entitlement else None),
        freshness_generation=getattr(checkpoint, 'checkpoint_generation', None),
        delegation_version=getattr(delegation, 'version', None),
        freshness_vector_digest=freshness_vector_digest,
        policy_manifest_digest=RECEIVE_ASSIGNMENT_POLICY_DIGEST,
    )
    evidence = {k: v for k, v in asdict(decision).items()
                if k not in {'allowed', 'reason'} and v is not None}
    dedupe = _digest({'reason': reason, **evidence})
    try:
        RoutingDenialAudit.objects.get_or_create(
            dedupe_key=dedupe,
            defaults={
                'item_id_snapshot': decision.item_id,
                'organization_id': decision.organization_id,
                'routing_team_id': team.id if team else None,
                'subject_id': decision.subject_id,
                'reason': reason,
                'evidence': evidence,
            },
        )
    except IntegrityError:
        pass
    if reason in {
        'authority_stale', 'authority_checkpoint_missing',
        'authority_checkpoint_conflict', 'delegation_stale',
        'version_gap', 'poison_quarantined',
    }:
        _record_scope_hold(
            getattr(checkpoint, 'authority_id', 'routing-authority'),
            getattr(checkpoint, 'aggregate_key', evidence.get('item_id', '__unknown__')),
            getattr(checkpoint, 'checkpoint_generation', 0),
            reason,
        )
    return decision


def subject_authorized_for_item(subject_id: int, item: Item, *, team_name: str,
                                action=ACTION_RECEIVE_ASSIGNMENT,
                                lock=False, expected_project_id=None,
                                expected_assignment_group=None,
                                expected_workflow_name=None) -> AuthorizationDecision:
    """Server-loaded, exact-org, pre-existing receive-assignment authorization."""
    if action != ACTION_RECEIVE_ASSIGNMENT:
        return _deny('action_unknown', item=item)
    if item is None or not getattr(item, 'pk', None):
        return _deny('item_unpersisted', item=item)
    try:
        if lock:
            # ``Item.project`` is nullable, so PostgreSQL rejects a plain
            # SELECT FOR UPDATE across the outer join produced by
            # select_related('project').  Lock the two authoritative rows
            # explicitly instead: the item freezes routing context while the
            # project lock freezes org/access-level selection for the grant.
            loaded = Item.objects.select_for_update(of=('self',)).get(pk=item.pk)
            if loaded.project_id:
                loaded.project = Project.objects.select_for_update().get(
                    pk=loaded.project_id,
                )
        else:
            loaded = Item.objects.select_related('project').get(pk=item.pk)
    except Item.DoesNotExist:
        return _deny('item_missing', item=item)
    # A context-changing assignment command authorizes the resulting tuple,
    # not the tuple that happened to be persisted before the row lock.  The
    # caller cannot choose an organization: it supplies only the destination
    # project PK and the server loads/locks that Project authority here.
    if expected_project_id is not None:
        loaded.project_id = expected_project_id
        try:
            project_qs = Project.objects.select_for_update() if lock else Project.objects
            loaded.project = project_qs.get(pk=expected_project_id)
        except Project.DoesNotExist:
            return _deny('project_missing', item=loaded)
    if expected_assignment_group is not None:
        loaded.assignment_group = expected_assignment_group
    if expected_workflow_name is not None:
        loaded.workflow_name = expected_workflow_name
    if not loaded.project_id:
        return _deny('project_missing', item=loaded)
    group = _team_key(loaded.assignment_group)
    if group != _team_key(team_name):
        return _deny('routing_context_conflict', item=loaded)
    org_id = int(loaded.project.org_id)
    team_qs = RoutingTeam.objects
    binding_qs = RoutingTeamBinding.objects
    checkpoint_qs = RoutingAuthorityCheckpoint.objects
    entitlement_qs = ReceiveAssignmentEntitlement.objects
    delegation_qs = CrossOrgAssignmentDelegation.objects
    workflow_qs = Workflow.objects
    if lock:
        team_qs = team_qs.select_for_update()
        binding_qs = binding_qs.select_for_update()
        checkpoint_qs = checkpoint_qs.select_for_update()
        entitlement_qs = entitlement_qs.select_for_update()
        delegation_qs = delegation_qs.select_for_update()
        workflow_qs = workflow_qs.select_for_update()
    team = team_qs.filter(
        organization_id=org_id, key=group, state=RoutingTeam.STATE_ACTIVE).first()
    if not team:
        return _deny('routing_team_missing', item=loaded)
    binding = binding_qs.filter(
        organization_id=org_id, routing_team=team,
        subject__subject_id=subject_id, is_current=True,
    ).select_related('subject').first()
    subject = binding.subject if binding else None
    if not subject or subject.state != RoutingSubject.STATE_ACTIVE:
        return _deny('subject_inactive', item=loaded, subject=subject, team=team)
    if not binding or binding.state != RoutingTeamBinding.STATE_ACTIVE:
        return _deny('binding_inactive', item=loaded, subject=subject, team=team,
                     binding=binding)
    if binding.subject_generation != subject.generation:
        return _deny('subject_generation_stale', item=loaded, subject=subject,
                     team=team, binding=binding)
    checkpoint = checkpoint_qs.filter(
        authority_id='shizuha-id',
        aggregate_key=f'{subject.organization_id}:{subject.subject_id}',
    ).first()
    now = timezone.now()
    if (not checkpoint or checkpoint.subject_generation != subject.generation
            or checkpoint.observed_version != subject.generation
            or checkpoint.digest != subject.digest
            or checkpoint.revocation_high_water != subject.revocation_high_water
            or checkpoint.lease_until <= now
            or checkpoint.lease_until > checkpoint.observed_at + timedelta(
                seconds=MAX_LEASE_SECONDS)
            or checkpoint.observed_at > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
            or not checkpoint.source_event_id
            or not checkpoint.first_received_at
            or checkpoint.lease_until > checkpoint.first_received_at + timedelta(
                seconds=MAX_LEASE_SECONDS)
            or (checkpoint.source_expires_at
                and checkpoint.lease_until > checkpoint.source_expires_at)):
        return _deny('authority_stale', item=loaded, subject=subject, team=team,
                     binding=binding, checkpoint=checkpoint)
    wf = (loaded.workflow_name or '').strip()
    entitlements = entitlement_qs.filter(
        organization_id=org_id, subject=subject, routing_team=team,
        workflow_name=wf, action=action, is_current=True,
    )
    cross_org = subject.organization_id != org_id
    delegation = None
    if cross_org:
        entitlements = entitlements.filter(project=loaded.project)
    elif loaded.project.access_level in {'limited', 'private'}:
        entitlements = entitlements.filter(project=loaded.project)
    else:
        entitlements = entitlements.filter(Q(project=loaded.project) | Q(project__isnull=True))
    entitlement = entitlements.order_by('-project_id').first()
    if not entitlement or entitlement.state != ReceiveAssignmentEntitlement.STATE_ACTIVE:
        return _deny('entitlement_missing', item=loaded, subject=subject, team=team,
                     binding=binding, entitlement=entitlement, checkpoint=checkpoint)
    if entitlement.subject_generation != subject.generation:
        return _deny('entitlement_subject_stale', item=loaded, subject=subject,
                     team=team, binding=binding, entitlement=entitlement,
                     checkpoint=checkpoint)
    if entitlement.expires_at and entitlement.expires_at <= now:
        return _deny('entitlement_expired', item=loaded, subject=subject, team=team,
                     binding=binding, entitlement=entitlement, checkpoint=checkpoint)
    if not entitlement.grant_source or not entitlement.grant_event_id:
        return _deny('entitlement_provenance_missing', item=loaded, subject=subject,
                     team=team, binding=binding, entitlement=entitlement,
                     checkpoint=checkpoint)
    if cross_org and not entitlement.expires_at:
        return _deny('cross_org_expiry_missing', item=loaded, subject=subject,
                     team=team, binding=binding, entitlement=entitlement,
                     checkpoint=checkpoint)
    if cross_org:
        delegation = delegation_qs.filter(
            source_organization_id=subject.organization_id,
            target_organization_id=org_id,
            subject=subject,
            routing_team=team,
            project=loaded.project,
            action=action,
            is_current=True,
        ).first()
        if not delegation or delegation.state != CrossOrgAssignmentDelegation.STATE_ACTIVE:
            return _deny(
                'cross_org_delegation_missing', item=loaded, subject=subject,
                team=team, binding=binding, entitlement=entitlement,
                checkpoint=checkpoint, delegation=delegation,
            )
        if (delegation.source_organization_id != subject.organization_id
                or delegation.target_organization_id != org_id
                or delegation.project_id != loaded.project_id
                or delegation.action != action):
            return _deny(
                'cross_org_delegation_scope_mismatch', item=loaded, subject=subject,
                team=team, binding=binding, entitlement=entitlement,
                checkpoint=checkpoint, delegation=delegation,
            )
        if delegation.subject_generation != subject.generation:
            return _deny(
                'cross_org_delegation_subject_stale', item=loaded, subject=subject,
                team=team, binding=binding, entitlement=entitlement,
                checkpoint=checkpoint, delegation=delegation,
            )
        if delegation.expires_at <= now:
            return _deny(
                'cross_org_delegation_expired', item=loaded, subject=subject,
                team=team, binding=binding, entitlement=entitlement,
                checkpoint=checkpoint, delegation=delegation,
            )
    from .workflow_service import WorkflowService
    required = WorkflowService._task_required_capabilities(loaded)
    # HIVE-1153 (Security P1): pass the item so the authoritative owner-write
    # commit fence authorizes runtime-substrate caps from verified signed Hive
    # evidence under `enforce` — never the legacy team/plain-JSON fallback that
    # `_seat_satisfies_required_capability` returns when `item is None`.
    if not WorkflowService._seat_satisfies_required_capability(
            subject.canonical_email, required, item=loaded):
        return _deny('destination_capability_missing', item=loaded, subject=subject,
                     team=team, binding=binding, entitlement=entitlement,
                     checkpoint=checkpoint)
    workflow = workflow_qs.filter(slug=wf).first()
    vector, vector_digest = _freshness_vector(
        item=loaded,
        project=loaded.project,
        workflow=workflow,
        team=team,
        binding=binding,
        subject=subject,
        entitlement=entitlement,
        checkpoint=checkpoint,
        delegation=delegation,
    )
    return AuthorizationDecision(
        allowed=True,
        reason='authorized',
        subject_id=subject.subject_id,
        item_id=loaded.id,
        organization_id=org_id,
        project_id=loaded.project_id,
        routing_team_id=str(team.id),
        assignment_group=group,
        workflow_name=wf,
        binding_version=binding.version,
        entitlement_version=entitlement.version,
        subject_generation=subject.generation,
        revocation_high_water=max(subject.revocation_high_water,
                                  entitlement.revocation_high_water,
                                  delegation.revocation_generation if delegation else 0),
        grant_source=entitlement.grant_source,
        grant_event_id=str(entitlement.grant_event_id),
        freshness_generation=checkpoint.checkpoint_generation,
        delegation_version=delegation.version if delegation else None,
        freshness_vector_digest=vector_digest,
        policy_manifest_digest=RECEIVE_ASSIGNMENT_POLICY_DIGEST,
    )


def apply_consumer_event(*, consumer: str, authority_kind: str, aggregate_key: str,
                         version: int, digest: str, tombstone=False) -> str:
    """Atomic cursor semantics used by relay consumers and adversarial tests."""
    raw_event = {
        'consumer': consumer,
        'authority_kind': authority_kind,
        'aggregate_key': aggregate_key,
        'version': version,
        'digest': digest,
        'tombstone': bool(tombstone),
    }
    try:
        version = int(version)
    except (TypeError, ValueError):
        return _quarantine_poison(raw_event, 'version_invalid')
    if (not consumer or not authority_kind or not aggregate_key or version < 1
            or len(str(digest)) != 64
            or any(ch not in '0123456789abcdef' for ch in str(digest).lower())):
        return _quarantine_poison(raw_event, 'schema_invalid')
    with transaction.atomic():
        cursor, _ = RoutingAuthorityConsumerState.objects.select_for_update().get_or_create(
            consumer=consumer, authority_kind=authority_kind, aggregate_key=aggregate_key)
        if version <= cursor.applied_version:
            receipt = RoutingAuthorityConsumerReceipt.objects.filter(
                consumer=consumer, authority_kind=authority_kind,
                aggregate_key=aggregate_key, version=version,
            ).first()
            if receipt and receipt.digest == digest:
                return 'duplicate'
            cursor.hold_reason = 'conflicting_digest'
            cursor.save(update_fields=['hold_reason', 'updated_at'])
            return 'integrity_hold'
        if version > cursor.applied_version + 1:
            cursor.pending_gap_version = cursor.applied_version + 1
            cursor.hold_reason = 'version_gap'
            cursor.save(update_fields=['pending_gap_version', 'hold_reason', 'updated_at'])
            return 'gap_hold'
        if tombstone:
            cursor.tombstone_high_water = max(cursor.tombstone_high_water, version)
        elif version <= cursor.tombstone_high_water:
            cursor.hold_reason = 'stale_active_after_tombstone'
            cursor.save(update_fields=['hold_reason', 'updated_at'])
            return 'integrity_hold'
        RoutingAuthorityConsumerReceipt.objects.create(
            consumer=consumer,
            authority_kind=authority_kind,
            aggregate_key=aggregate_key,
            version=version,
            digest=digest,
            tombstone=bool(tombstone),
        )
        cursor.applied_version = version
        cursor.applied_digest = digest
        cursor.pending_gap_version = None
        poison_open = RoutingAuthorityQuarantine.objects.filter(
            consumer=consumer,
            authority_kind=authority_kind,
            aggregate_key=aggregate_key,
            recovered_at__isnull=True,
        ).exists()
        cursor.hold_reason = 'poison_quarantined' if poison_open else ''
        cursor.save(update_fields=[
            'applied_version', 'applied_digest', 'tombstone_high_water',
            'pending_gap_version', 'hold_reason', 'updated_at',
        ])
        return 'applied'


def _quarantine_poison(raw_event, reason):
    """Persist poison evidence and a durable HOLD before returning."""
    consumer = str(raw_event.get('consumer') or '__invalid__')[:100]
    authority_kind = str(raw_event.get('authority_kind') or '__invalid__')[:64]
    aggregate_key = str(raw_event.get('aggregate_key') or '__poison__')[:255]
    canonical = {
        key: (str(value) if isinstance(value, uuid.UUID) else value)
        for key, value in raw_event.items()
    }
    dedupe = _digest({'event': canonical, 'reason': reason})
    with transaction.atomic():
        RoutingAuthorityQuarantine.objects.get_or_create(
            dedupe_key=dedupe,
            defaults={
                'consumer': consumer,
                'authority_kind': authority_kind,
                'aggregate_key': aggregate_key,
                'raw_event': canonical,
                'reason': reason,
            },
        )
        cursor, _ = RoutingAuthorityConsumerState.objects.select_for_update().get_or_create(
            consumer=consumer,
            authority_kind=authority_kind,
            aggregate_key=aggregate_key,
        )
        cursor.hold_reason = 'poison_quarantined'
        cursor.save(update_fields=['hold_reason', 'updated_at'])
        _record_scope_hold(
            authority_kind,
            aggregate_key,
            0,
            'poison_quarantined',
        )
    return 'poison_quarantined'
