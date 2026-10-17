"""Default board provisioning for Pulse projects (PLS-58).

This module keeps Project post_save signals and backfill commands on the same
idempotent path so existing projects and newly-created projects get identical
board configuration.
"""

import logging

from django.db import transaction
from django.db.models import Max

from .models import Board, Project

logger = logging.getLogger(__name__)

DEFAULT_BOARD_SUFFIX = "Kanban"
DEFAULT_BOARD_TYPE = "kanban"
DEFAULT_SOFTWARE_PROJECT_TYPE = "software"


def default_board_name(project):
    """Return the canonical default board name for a project."""
    return f"{project.name} — {DEFAULT_BOARD_SUFFIX}"


def should_create_default_board(project):
    """Default boards are currently scoped to active software projects."""
    return (
        project.project_type == DEFAULT_SOFTWARE_PROJECT_TYPE
        and project.is_active
        and not project.is_archived
    )


def ensure_default_board_for_project(project, *, dry_run=False):
    """Ensure a project has exactly one canonical default Kanban board.

    Returns ``(board, created)``. ``board`` is ``None`` when the project is out of
    scope or ``dry_run=True`` and a board would have been created.

    Idempotency rules:
    - A default board already attached to the project wins.
    - A board with the canonical ``<Project> — Kanban`` name is promoted to
      default rather than duplicated.
    - Otherwise a new default Kanban board is created at the next position.
    """
    if not should_create_default_board(project):
        return None, False

    canonical_name = default_board_name(project)

    with transaction.atomic():
        existing_default = (
            Board.objects.select_for_update()
            .filter(project=project, is_default=True)
            .order_by("position", "created_at")
            .first()
        )
        if existing_default:
            updates = []
            if existing_default.board_type != DEFAULT_BOARD_TYPE:
                existing_default.board_type = DEFAULT_BOARD_TYPE
                updates.append("board_type")
            if not existing_default.name:
                existing_default.name = canonical_name
                updates.append("name")
            if updates and not dry_run:
                updates.append("updated_at")
                existing_default.save(update_fields=updates)
            return existing_default, False

        canonical_board = (
            Board.objects.select_for_update()
            .filter(project=project, name=canonical_name)
            .order_by("position", "created_at")
            .first()
        )
        if canonical_board:
            if not dry_run:
                canonical_board.is_default = True
                canonical_board.board_type = DEFAULT_BOARD_TYPE
                canonical_board.save(update_fields=["is_default", "board_type", "updated_at"])
            return canonical_board, False

        if dry_run:
            return None, True

        next_position = (Board.objects.filter(project=project).aggregate(Max("position"))["position__max"] or 0)
        if Board.objects.filter(project=project).exists():
            next_position += 1

        return Board.objects.create(
            project=project,
            name=canonical_name,
            description="Default Kanban board for this project.",
            board_type=DEFAULT_BOARD_TYPE,
            filter_jql="",
            swimlane_by="none",
            position=next_position,
            is_default=True,
            created_by_id=project.created_by_id,
        ), True


def ensure_default_board_for_new_project(project):
    """Signal-safe wrapper for best-effort default board creation."""
    try:
        ensure_default_board_for_project(project)
    except Exception:
        logger.exception("Failed to create default board for project %s", project.pk)
