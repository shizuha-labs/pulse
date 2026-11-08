"""PLS-1338: backfill is_system classification for historical automation comments.

Automation lifecycle rows (system@pulse / workflow@pulse authors) landed with
the LEGACY_UNTYPED default origin_kind, so the digest partitioning consumed
is_system=false and rendered churn as separate substantive entries — 0/187
flagged on PLAT-528, 0/94 on SCLI-731.

The predicate is author-based (author_email ends '@pulse') and ONLY upgrades
rows still on the LEGACY_UNTYPED default (rows an automation explicitly typed
with another kind are left alone). Idempotent by construction: re-runs match
zero rows once applied. Provenance-immutability does not apply — this is the
audited, exact-predicate QuerySet.update repair the save() docstring allows.
"""
from django.core.management.base import BaseCommand

from tasks.models import Comment


class Command(BaseCommand):
    help = (
        'PLS-1338: classify historical automation comments '
        '(author_email ending @pulse, still LEGACY_UNTYPED) as '
        'WORKFLOW_LIFECYCLE so the digest partitions them as system rows.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report counts without writing.')

    def handle(self, *args, **options):
        qs = Comment.objects.filter(
            origin_kind=Comment.ORIGIN_LEGACY_UNTYPED,
        ).filter(
            author_email__iendswith='@pulse',
        )
        total = qs.count()
        if options['dry_run']:
            self.stdout.write(f'would classify: {total} rows')
            for a in qs.values_list('author_email', flat=True).distinct():
                self.stdout.write(f'  {a}: {qs.filter(author_email=a).count()}')
            return
        updated = qs.update(origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE)
        self.stdout.write(f'classified {updated} of {total} candidate rows')
