"""
Reconcile every Project.next_item_number to max(existing <KEY>-<n>) + 1 (PULSE-1).

The item-key counter can desync below the highest existing key when items are
created off the normal serializer path (manage.py shell / bulk backfill) without
advancing the counter — which makes the next create mint a duplicate item_key and
500 with an IntegrityError. get_next_item_key() now self-heals on the next create,
but this command lets ops proactively sweep + repair all projects (idempotent).

Usage:
    python manage.py reconcile_item_numbers            # repair drift
    python manage.py reconcile_item_numbers --dry-run  # report only
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max, IntegerField
from django.db.models.functions import Cast, Substr

from tasks.models import Project, Item


def _max_existing_number(project):
    """Highest numeric suffix among this project's existing item_keys, or 0."""
    prefix = f"{project.key}-"
    return (
        Item.objects
        .filter(item_key__regex=rf'^{re.escape(project.key)}-\d+$')
        .annotate(_num=Cast(Substr('item_key', len(prefix) + 1), IntegerField()))
        .aggregate(m=Max('_num'))['m']
    ) or 0


class Command(BaseCommand):
    help = ("Reconcile each Project.next_item_number to max(existing <KEY>-<n>)+1 "
            "(PULSE-1). Idempotent — re-running with no drift is a no-op.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Report drift without writing.",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        drifted = 0
        for project in Project.objects.all().only('id', 'key', 'next_item_number'):
            max_existing = _max_existing_number(project)
            target = max_existing + 1
            if project.next_item_number >= target:
                continue
            drifted += 1
            self.stdout.write(
                f"{project.key}: next_item_number {project.next_item_number} "
                f"-> {target} (max existing {max_existing})"
            )
            if not dry_run:
                # Re-check under a row lock to avoid clobbering a concurrent mint.
                with transaction.atomic():
                    locked = Project.objects.select_for_update().get(pk=project.pk)
                    fresh_target = _max_existing_number(locked) + 1
                    if locked.next_item_number < fresh_target:
                        locked.next_item_number = fresh_target
                        locked.save(update_fields=['next_item_number', 'updated_at'])
        verb = "Would repair" if dry_run else "Repaired"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {drifted} project(s) with item-key counter drift."
        ))
