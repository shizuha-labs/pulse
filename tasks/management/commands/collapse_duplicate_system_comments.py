"""PLS-198 cleanup: collapse duplicate consecutive system@pulse comments.

The pre-fix auto-failover drain posted near-identical system comments in bursts
and re-fired every sweep tick, leaving items like BKST-1 with 2000+ spam
comments that bury the real handoff history. This command keeps the FIRST
comment of every consecutive same-content system@pulse run and deletes the
rest. Non-consecutive repeats are kept — an owner legitimately bouncing back
and forth produces distinct history entries.

Usage:
    manage.py collapse_duplicate_system_comments --item-key BKST-1 --dry-run
    manage.py collapse_duplicate_system_comments --item-key BKST-1
    manage.py collapse_duplicate_system_comments --all
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

SYSTEM_AUTHOR = 'system@pulse'


class Command(BaseCommand):
    help = ('Collapse consecutive duplicate system@pulse comments '
            '(PLS-198 auto-failover comment-spam cleanup)')

    def add_arguments(self, parser):
        parser.add_argument('--item-key', help='Collapse only this item (e.g. BKST-1)')
        parser.add_argument('--all', action='store_true',
                            help='Collapse every item that has system comments')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted without deleting')

    def handle(self, *args, **options):
        from tasks.models import Comment, Item

        item_key = (options.get('item_key') or '').strip()
        if not item_key and not options['all']:
            raise CommandError('Pass --item-key <KEY> or --all')

        if item_key:
            items = Item.objects.filter(item_key__iexact=item_key)
            if not items.exists():
                raise CommandError(f'No item with key {item_key!r}')
        else:
            items = Item.objects.filter(
                comments__author_email=SYSTEM_AUTHOR).distinct()

        total_deleted = 0
        for item in items.iterator():
            doomed = []
            prev_system_content = None
            comments = (Comment.objects.filter(item=item)
                        .order_by('created_at', 'id')
                        .only('id', 'author_email', 'content', 'created_at'))
            for comment in comments.iterator():
                if comment.author_email != SYSTEM_AUTHOR:
                    # A human/agent comment breaks the run: a later identical
                    # system comment is new information again.
                    prev_system_content = None
                    continue
                if comment.content == prev_system_content:
                    doomed.append(comment.id)
                else:
                    prev_system_content = comment.content
            if not doomed:
                continue
            label = item.item_key or f'#{item.pk}'
            if options['dry_run']:
                self.stdout.write(
                    f'{label}: would delete {len(doomed)} duplicate system comment(s)')
            else:
                with transaction.atomic():
                    deleted, _ = Comment.objects.filter(id__in=doomed).delete()
                self.stdout.write(
                    f'{label}: deleted {deleted} duplicate system comment(s)')
            total_deleted += len(doomed)

        verb = 'would delete' if options['dry_run'] else 'deleted'
        self.stdout.write(self.style.SUCCESS(
            f'Done — {verb} {total_deleted} duplicate system comment(s).'))
