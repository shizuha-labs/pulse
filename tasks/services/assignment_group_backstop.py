"""PLAT-9100: create-time ``assignment_group`` backstop (PLAT-9096 prevention).

Retro finding (PLAT-9096): 84 urgent/high items were filed with NO
``assignment_group`` — invisible to every team drain, because the create-time
team resolution in ``ItemViewSet.perform_create`` only fires for mode=task
creates that carry no direct assignee. Direct-assignee creates, alert-mode
creates and any other shape that skipped that branch landed teamless.

This backstop runs at the serializer choke point (``ItemSerializer.create``),
so EVERY create that crosses the API/MCP boundary is covered. It COMPOSES with
the PLAT-9076 filing backstop (pulse#557, validate()-time, urgent/high
task-mode) and with the PLS-278 transition-time team resolver — the backstop
guarantees a group exists at create; the resolver routes within it.

Resolution order (kai spec 2026-09-19):
  1. explicit ``assignment_group``            → canonicalized
  2. workflow/project/tags derivation         → PLS-933 ``_derive_owner_team``
     (the workflow-default signal)
  3. reporter's team                          → ``_team_of_email(created_by_email)``
  4. direct assignee's team                   → ``_team_of_email(assignee_email)``

Outcome contract:
  - resolved  → group stamped AND a provenance ledger row written to
    ``metadata['assignment_group_backstop'] = 'auto-resolved:<source>'`` so
    misrouting is correctable and the auto-resolver's accuracy is measurable
    (kai spec §3).
  - unresolvable + mode=task + no assignee → **loud-fail** (HTTP 400, logged).
    A teamless ownerless task is exactly the PLAT-9096 failure mode; filing
    must name a team. (kai spec §2: never silently teamless.)
  - unresolvable otherwise (alert/action mode, or a concrete direct assignee
    is present) → **ledger**: ERROR log + ``metadata`` flag
    (``unresolved`` / ``owner-only-unresolved``). Alert ingestion must never
    400; a direct-assignee item is not invisible — it has an owner — and the
    HIVE-1223 fresh-org first-task path must keep working.
  - derivation infra error → fail open (ledgered), never blocks creation.

Fail-open: an unexpected exception in the backstop logs and passes through —
it must never block legitimate creation (same contract as the PLAT-524
classifier guard).
"""
from __future__ import annotations

import logging

from rest_framework import serializers

logger = logging.getLogger(__name__)

_METADATA_FLAG = 'assignment_group_backstop'
_FLAG_UNRESOLVED = 'unresolved'
_FLAG_OWNER_ONLY = 'owner-only-unresolved'


def _team_of_email(email: str) -> str | None:
    """Reverse roster lookup via the shared WorkflowService helper."""
    if not email:
        return None
    try:
        from .workflow_service import WorkflowService

        return WorkflowService._team_of_email(email)
    except Exception:
        logger.warning("PLAT-9100 backstop: roster team lookup failed", exc_info=True)
        return None


def _ledger(validated_data: dict, value: str) -> None:
    """Write the provenance/ledger flag into item metadata (kai spec §3)."""
    metadata = dict(validated_data.get('metadata') or {})
    metadata[_METADATA_FLAG] = value
    validated_data['metadata'] = metadata


def apply_create_time_backstop(validated_data: dict) -> None:
    """Require or auto-resolve ``assignment_group`` on an Item create.

    Mutates ``validated_data`` in place. Raises DRF ``ValidationError`` only
    for the loud-fail case (mode=task, no assignee, unresolvable team).
    """
    try:
        from .workflow_service import WorkflowService

        team = (validated_data.get('assignment_group') or '').strip()
        if team:
            canonical = WorkflowService._canonical_team_name(team)
            if canonical:
                validated_data['assignment_group'] = canonical
            return

        # Auto-resolve, strongest signal first. Track the winning source so
        # every resolution is ledgered with its provenance.
        resolved = None
        source = None

        # (2) workflow default / project / tags — PLS-933 true-owner derivation.
        derivation_errored = False
        try:
            probe = WorkflowService.create_permission_probe_item(validated_data)
            derived = WorkflowService._derive_owner_team(probe)
            if derived:
                resolved, source = derived, 'derived'
        except Exception:
            derivation_errored = True
            logger.warning(
                "PLAT-9100 backstop: owner-team derivation failed — continuing",
                exc_info=True,
            )

        # (3) reporter's team — "where the task came from".
        if not resolved:
            reporter_team = _team_of_email(
                str(validated_data.get('created_by_email') or '')
            )
            if reporter_team:
                resolved, source = reporter_team, 'reporter-team'

        # (4) direct assignee's team.
        assignee_email = (
            validated_data.get('assignee_email') or ''
        ).strip().lower()
        if not resolved and assignee_email:
            assignee_team = _team_of_email(assignee_email)
            if assignee_team:
                resolved, source = assignee_team, 'assignee-team'

        if resolved:
            validated_data['assignment_group'] = resolved
            _ledger(validated_data, f'auto-resolved:{source}')
            return

        mode = (validated_data.get('mode') or 'task').strip().lower()
        if assignee_email:
            # A concrete owner exists — the item is not invisible to all
            # drains. Ledger the missing team instead of blocking (keeps the
            # HIVE-1223 fresh-org first-task path and operator self-assign
            # working).
            _ledger(validated_data, _FLAG_OWNER_ONLY)
            logger.error(
                "PLAT-9100 backstop: teamless create with direct assignee "
                "(ledgered, not blocked): mode=%s assignee=%s title=%r "
                "project=%s workflow=%s",
                mode, assignee_email,
                str(validated_data.get('title') or '')[:120],
                getattr(validated_data.get('project'), 'pk', validated_data.get('project')),
                validated_data.get('workflow_name'),
            )
            return

        if mode == 'task' and not derivation_errored:
            # Loud-fail only when derivation ran cleanly and resolved nothing.
            # An errored derivation is an infra failure, not a filing mistake —
            # fail open so the backstop never blocks legitimate creation.
            logger.warning(
                "PLAT-9100 backstop: LOUD-FAIL refusing teamless ownerless "
                "task create: title=%r project=%s workflow=%s reporter=%s",
                str(validated_data.get('title') or '')[:120],
                getattr(validated_data.get('project'), 'pk', validated_data.get('project')),
                validated_data.get('workflow_name'),
                validated_data.get('created_by_email'),
            )
            raise serializers.ValidationError({
                'assignment_group': (
                    'PLAT-9100 create-time backstop: no assignment_group and '
                    'none resolvable from workflow/project/tags or the '
                    'reporter/assignee roster. Pass assignment_group '
                    'explicitly — teamless ownerless tasks are invisible to '
                    'every team drain (PLAT-9096).'
                ),
            })

        # Alert/action mode: availability-sensitive — never block ingestion.
        # Ledger loudly so the gap is queryable and alertable.
        _ledger(validated_data, _FLAG_UNRESOLVED)
        logger.error(
            "PLAT-9100 backstop: teamless %s-mode create (ledgered): "
            "title=%r project=%s workflow=%s source=%s",
            mode,
            str(validated_data.get('title') or '')[:120],
            getattr(validated_data.get('project'), 'pk', validated_data.get('project')),
            validated_data.get('workflow_name'),
            validated_data.get('source'),
        )
    except serializers.ValidationError:
        raise
    except Exception:
        # Fail-open: the backstop must never block legitimate creation.
        logger.exception(
            "PLAT-9100 backstop raised unexpectedly — failing open (create proceeds)"
        )
