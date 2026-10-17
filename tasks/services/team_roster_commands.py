"""Authoritative, exact-org TeamRoster command boundary (PLS-745)."""

from dataclasses import dataclass
import hashlib
import json
import uuid

from django.db import IntegrityError, connection, transaction

from tasks.models import TeamRoster, TeamRosterCommandReceipt


@dataclass
class TeamRosterCommandError(Exception):
    code: str
    detail: str


def _canonical_team(name):
    normalized = (name or '').strip().lower().replace('_', '-').replace(' ', '-')
    return TeamRoster._CANONICAL_ALIASES.get(normalized, normalized)


def _canonical_payload(*, command_id, action, organization_id,
                       organization_slug, team_name, email, expected_version):
    payload = {
        'command_id': str(command_id),
        'action': action,
        'organization_id': int(organization_id),
        'organization_slug': organization_slug,
        'team_name': team_name,
        'email': email,
        'expected_version': int(expected_version),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return payload, hashlib.sha256(raw).hexdigest()


def _flat_members(roster):
    if roster is None:
        return []
    return sorted(TeamRoster._flat_member_emails(roster.tiers, roster.junior_emails))


def _replay(receipt, payload_hash):
    if receipt.payload_hash != payload_hash:
        raise TeamRosterCommandError(
            'idempotency_payload_conflict',
            'command_id was already committed with a different payload.',
        )
    return {**receipt.result, 'idempotent_replay': True}


def execute_team_roster_command(*, command_id, action, organization_id,
                                organization_slug, team_name, email='',
                                expected_version, actor_email=''):
    """Apply one payload-bound roster command and return its durable receipt.

    ``expected_version=0`` is the create-if-absent compare-and-swap boundary.
    Removing from an absent roster creates an active empty roster, because an
    explicit authoritative-empty answer is materially different from no
    authority row at all.
    """
    try:
        command_id = uuid.UUID(str(command_id))
    except (TypeError, ValueError, AttributeError):
        raise TeamRosterCommandError('command_id_invalid', 'command_id must be a UUID.')
    action = (action or '').strip().lower()
    if action not in {'add', 'remove', 'create'}:
        raise TeamRosterCommandError('action_invalid', 'action must be add, remove, or create.')
    team_name = _canonical_team(team_name)
    organization_slug = (organization_slug or '').strip().lower()
    email = (email or '').strip().lower()
    if not team_name or not organization_slug:
        raise TeamRosterCommandError(
            'identity_invalid', 'organization_slug and team_name are required.',
        )
    if action in {'add', 'remove'} and not TeamRoster._is_email(email):
        raise TeamRosterCommandError('email_invalid', 'A valid email is required for add/remove.')
    if action == 'add':
        # PLS-1164 roster-write boundary: the added email must resolve to a
        # shizuha-id principal. Phantom labels are rejected with the named
        # reason class; resolvable emails are rewritten to the PLAT-4220
        # roster storage form BEFORE the membership check so the stored
        # roster only ever carries resolvable, convention-valid labels.
        # Authority outages fail open (email stored as-is, never phantom).
        from .canonical_identity import (
            REASON_PHANTOM_ROSTER_IDENTITY,
            classify_roster_email,
            roster_label_for_principal,
        )
        _status, _principal_canonical = classify_roster_email(email)
        if _status == 'phantom':
            raise TeamRosterCommandError(
                REASON_PHANTOM_ROSTER_IDENTITY,
                f'{email} resolves to no shizuha-id principal; a roster label '
                'that matches no principal is a silent zero in every routing '
                'consumer (PLS-1154/PLS-1164). Canonicalize or provision it first.',
            )
        if _status in ('canonical', 'canonicalizable'):
            _stored = roster_label_for_principal(_principal_canonical)
            if _stored != email:
                email = _stored
    try:
        expected_version = int(expected_version)
        organization_id = int(organization_id)
    except (TypeError, ValueError):
        raise TeamRosterCommandError(
            'version_or_org_invalid', 'organization_id and expected_version must be integers.',
        )
    if expected_version < 0 or organization_id <= 0:
        raise TeamRosterCommandError(
            'version_or_org_invalid', 'organization_id must be positive and expected_version non-negative.',
        )

    _payload, payload_hash = _canonical_payload(
        command_id=command_id,
        action=action,
        organization_id=organization_id,
        organization_slug=organization_slug,
        team_name=team_name,
        email=email,
        expected_version=expected_version,
    )
    existing = TeamRosterCommandReceipt.objects.filter(command_id=command_id).first()
    if existing is not None:
        return _replay(existing, payload_hash)

    with transaction.atomic():
        # Serialize identical in-flight command IDs before the receipt lookup.
        # Row locking cannot lock an absent receipt, which otherwise lets a
        # concurrent retry wait on the roster and then see a misleading stale-
        # version conflict instead of the first request's committed receipt.
        if connection.vendor == 'postgresql':
            advisory_key = command_id.int & ((1 << 63) - 1)
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [advisory_key])
        existing = TeamRosterCommandReceipt.objects.select_for_update().filter(
            command_id=command_id,
        ).first()
        if existing is not None:
            return _replay(existing, payload_hash)

        # Lock the exact organization, not a first matching team slug.  The
        # model's one-agent-one-team enforcement can edit siblings, so those
        # rows participate in the same transaction and version history.
        rosters = list(
            TeamRoster.objects.select_for_update()
            .filter(organization_id=organization_id)
            .order_by('pk')
        )
        roster = next(
            (
                row for row in rosters
                if row.organization_slug == organization_slug
                and _canonical_team(row.team_name) == team_name
            ),
            None,
        )
        previous_version = int(roster.version or 0) if roster is not None else 0
        if previous_version != expected_version:
            raise TeamRosterCommandError(
                'expected_version_conflict',
                f'Roster version is {previous_version}, not {expected_version}.',
            )

        changed = False
        if roster is None:
            roster = TeamRoster(
                team_name=team_name,
                organization_id=organization_id,
                organization_slug=organization_slug,
                tiers=[],
                junior_emails=[],
                is_active=True,
                updated_by=actor_email,
            )
            roster.save()
            changed = True
        elif not roster.is_active:
            roster.is_active = True
            roster.updated_by = actor_email
            roster.save(update_fields=['is_active', 'updated_by', 'updated_at'])
            changed = True

        if action == 'add':
            present = email in TeamRoster._flat_member_emails(
                roster.tiers, roster.junior_emails,
            )
            if not present:
                tiers = [list(tier or []) for tier in (roster.tiers or [[]])]
                if not tiers:
                    tiers = [[]]
                tiers[0].append(email)
                roster.tiers = tiers
                roster.updated_by = actor_email
                roster.save(update_fields=['tiers', 'updated_by', 'updated_at'])
                changed = True
        elif action == 'remove':
            tiers, juniors, removed = TeamRoster._remove_member_emails(
                roster.tiers, roster.junior_emails, {email},
            )
            if removed:
                roster.tiers = tiers
                roster.junior_emails = juniors
                roster.updated_by = actor_email
                roster.save(update_fields=[
                    'tiers', 'junior_emails', 'updated_by', 'updated_at',
                ])
                changed = True

        members = _flat_members(roster)
        result = {
            'command_id': str(command_id),
            'receipt': str(command_id),
            'action': action,
            'organization_id': organization_id,
            'organization_slug': organization_slug,
            'team_name': team_name,
            'email': email,
            'changed': changed,
            'previous_version': previous_version,
            'version': int(roster.version),
            'tiers': roster.tiers or [],
            'junior_emails': roster.junior_emails or [],
            'members': members,
            'authoritative_empty': not members,
            'idempotent_replay': False,
        }
        try:
            with transaction.atomic():
                TeamRosterCommandReceipt.objects.create(
                    command_id=command_id,
                    payload_hash=payload_hash,
                    organization_id=organization_id,
                    organization_slug=organization_slug,
                    team_name=team_name,
                    email=email,
                    action=action,
                    expected_version=expected_version,
                    previous_version=previous_version,
                    result_version=int(roster.version),
                    result=result,
                    actor_email=actor_email,
                )
        except IntegrityError:
            # A concurrent identical command won the unique-key race.  Re-read
            # its committed receipt; a different payload stays a hard conflict.
            winner = TeamRosterCommandReceipt.objects.get(command_id=command_id)
            return _replay(winner, payload_hash)
        return result
