"""Backfill default Kanban boards for existing software projects (PLS-58).

Usage:
    python manage.py backfill_default_boards
    python manage.py backfill_default_boards --dry-run
    python manage.py backfill_default_boards --project-key PLS --project-key CON
"""

from django.core.management.base import BaseCommand

from tasks.default_boards import ensure_default_board_for_project
from tasks.models import Project


class Command(BaseCommand):
    help = (
        "Create the canonical '<Project> — Kanban' default board for active "
        "software projects. Idempotent; safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created or reused without writing.',
        )
        parser.add_argument(
            '--project-key', action='append', default=[],
            help='Limit backfill to one project key. May be passed multiple times.',
        )
        parser.add_argument(
            '--include-archived', action='store_true',
            help='Include inactive/archived software projects (default: active only).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        qs = Project.objects.filter(project_type='software').order_by('org_id', 'key')
        if not options['include_archived']:
            qs = qs.filter(is_active=True, is_archived=False)
        if options['project_key']:
            qs = qs.filter(key__in=options['project_key'])

        scanned = created = reused = skipped = 0
        for project in qs.iterator():
            scanned += 1
            board, was_created = ensure_default_board_for_project(project, dry_run=dry_run)
            if was_created:
                created += 1
                action = 'would create' if dry_run else 'created'
                self.stdout.write(f"{project.key}: {action} {project.name} — Kanban")
            elif board:
                reused += 1
                self.stdout.write(f"{project.key}: default board exists ({board.name})")
            else:
                skipped += 1
                self.stdout.write(f"{project.key}: skipped (not an active software project)")

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} project(s); {verb} {created}; "
            f"reused/promoted {reused}; skipped {skipped}."
        ))
