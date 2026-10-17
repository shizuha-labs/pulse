"""PLS-200 one-time backfill for delivered-but-open epics.

This is intentionally a management command, not a recurring sweep: the runtime
path is event-driven from terminal child transitions. The command only catches
historical epics that predate the event hook and delegates the close-out decision
to WorkflowService._request_epic_closeout_for_epic so there is one source of
truth for child discovery, live-child filtering, routing, comments, and the
idempotency guard.
"""
from django.core.management.base import BaseCommand

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService


class Command(BaseCommand):
    help = 'PLS-200: one-time backfill close-out requests for open epics whose children are all terminal.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='Maximum candidate epics to evaluate (0 = all).')

    def handle(self, *args, **options):
        limit = max(int(options.get('limit') or 0), 0)
        qs = Item.objects.filter(WorkflowService._umbrella_item_q()).order_by('id')

        evaluated = 0
        requested = 0
        skipped_terminal = 0
        skipped_guarded = 0
        for epic in qs.iterator():
            if limit and evaluated >= limit:
                break
            if WorkflowService._status_is_terminal(epic.status) or epic.resolution_id:
                skipped_terminal += 1
                continue
            if dict(epic.metadata or {}).get('closeout_requested'):
                skipped_guarded += 1
                continue
            evaluated += 1
            if WorkflowService._request_epic_closeout_for_epic(epic):
                requested += 1

        self.stdout.write(self.style.SUCCESS(
            f'PLS-200 epic close-out backfill complete: evaluated={evaluated} '
            f'requested={requested} skipped_terminal={skipped_terminal} '
            f'skipped_guarded={skipped_guarded}'
        ))
