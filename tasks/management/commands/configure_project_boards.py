"""
Django management command: configure_project_boards

PLS-59 (B1 E2): Create Board shells and wire BoardColumn status mappings for
all software projects. Covers autonomous-dev and autonomous-bug workflows.

Idempotent — safe to re-run. Uses get_or_create throughout.

Usage:
    python manage.py configure_project_boards
    python manage.py configure_project_boards --project-ids 336 332
    python manage.py configure_project_boards --dry-run
"""

from django.core.management.base import BaseCommand

from tasks.models import Board, BoardColumn, Project


# Project keys that get board + column configuration.
# Excludes business/accounting projects (BKSG, BKS, EVOL) which don't use
# the autonomous-dev/bug workflows.
SOFTWARE_PROJECT_KEYS = {"CON", "HIVE", "PLS", "CTX", "HKR", "KURA", "SCS"}

# Columns in left-to-right display order.
# status_mapping: list of workflow status slugs that appear in this column.
# Covers both autonomous-dev and autonomous-bug status slugs as a superset so
# a single project-level column config works for all boards on that project.
BOARD_COLUMNS = [
    {
        "name": "Open",
        "status_mapping": ["open", "needs_triage"],
        "color": "#6b7280",
    },
    {
        "name": "In Progress",
        "status_mapping": ["in_progress", "blocked"],
        "color": "#3b82f6",
    },
    {
        "name": "In Review",
        "status_mapping": ["in_review"],
        "color": "#8b5cf6",
    },
    {
        "name": "Awaiting Merge",
        "status_mapping": ["awaiting_merge"],
        "color": "#f59e0b",
    },
    # Deploy + verification stages removed 2026-06-27 (operator directive): CI auto-
    # deploys on merge (the only path to the cluster), so the per-task flow ends at
    # Merge -> Done. Live verification is decoupled to QA's daily recurring sweep over
    # recently-merged work, not a per-task hand-off column.
    {
        "name": "Done",
        "status_mapping": ["done", "completed"],
        "color": "#10b981",
    },
]

# Board definitions per project — workflow-specific kanban views.
# filter_jql="" means all items; non-empty JQL scopes to that workflow.
BOARD_DEFINITIONS = [
    {
        "name": "{project_key} — Dev Board",
        "board_type": "kanban",
        "filter_jql": 'workflow = "autonomous-dev"',
        "is_default": True,
        "position": 0,
    },
    {
        "name": "{project_key} — Bug Board",
        "board_type": "kanban",
        "filter_jql": 'workflow = "autonomous-bug"',
        "is_default": False,
        "position": 1,
    },
]


class Command(BaseCommand):
    help = (
        "PLS-59: Create Board shells and wire BoardColumn status mappings "
        "for all software projects. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-ids",
            nargs="+",
            type=int,
            help="Limit to specific project IDs (default: all software projects)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be created without writing to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        project_id_filter = options.get("project_ids")

        qs = Project.objects.all()
        if project_id_filter:
            qs = qs.filter(id__in=project_id_filter)
        else:
            qs = qs.filter(key__in=SOFTWARE_PROJECT_KEYS)

        projects = list(qs.order_by("key"))
        if not projects:
            self.stdout.write(self.style.WARNING("No matching projects found."))
            return

        boards_created = boards_existing = 0
        columns_created = columns_existing = 0

        for project in projects:
            self.stdout.write(f"\n[{project.key}] {project.name} (id={project.id})")

            # --- Boards ---
            for board_def in BOARD_DEFINITIONS:
                name = board_def["name"].format(project_key=project.key)
                if dry_run:
                    existing = Board.objects.filter(
                        project=project, name=name
                    ).exists()
                    status = "exists" if existing else "would create"
                    self.stdout.write(f"  Board '{name}' — {status}")
                    if not existing:
                        boards_created += 1
                    else:
                        boards_existing += 1
                    continue

                board, created = Board.objects.get_or_create(
                    project=project,
                    name=name,
                    defaults={
                        "board_type": board_def["board_type"],
                        "filter_jql": board_def["filter_jql"],
                        "is_default": board_def["is_default"],
                        "position": board_def["position"],
                    },
                )
                if created:
                    self.stdout.write(
                        self.style.SUCCESS(f"  ✓ Created board: '{name}'")
                    )
                    boards_created += 1
                else:
                    self.stdout.write(f"  · Board exists: '{name}'")
                    boards_existing += 1

            # --- BoardColumns ---
            for position, col_def in enumerate(BOARD_COLUMNS):
                col_name = col_def["name"]
                if dry_run:
                    existing = BoardColumn.objects.filter(
                        project=project, name=col_name
                    ).exists()
                    status = "exists" if existing else "would create"
                    self.stdout.write(
                        f"  Column[{position}] '{col_name}' "
                        f"→ {col_def['status_mapping']} — {status}"
                    )
                    if not existing:
                        columns_created += 1
                    else:
                        columns_existing += 1
                    continue

                col, created = BoardColumn.objects.get_or_create(
                    project=project,
                    name=col_name,
                    defaults={
                        "status_mapping": col_def["status_mapping"],
                        "position": position,
                        "color": col_def["color"],
                    },
                )
                if created:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  ✓ Created column[{position}]: '{col_name}' "
                            f"→ {col_def['status_mapping']}"
                        )
                    )
                    columns_created += 1
                else:
                    self.stdout.write(
                        f"  · Column exists: '{col_name}' (position={col.position})"
                    )
                    columns_existing += 1

        prefix = "[DRY RUN] Would create" if dry_run else "Created"
        self.stdout.write(
            f"\n{prefix}: {boards_created} board(s), {columns_created} column(s). "
            f"Already existed: {boards_existing} board(s), {columns_existing} column(s)."
        )
