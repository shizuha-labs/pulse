from django.core.management.base import BaseCommand
from django.utils import timezone

from tasks.models import Item
from tasks.tasks import _recurring_cycle_state, _recurring_cycle_activity, _workflow_initial_status_slug, _sync_item_workflow_status


DEFAULT_KEYS = ['SM-1', 'PLAT-250', 'PLAT-251', 'EVOL-8', 'PLAT-304', 'CON-21', 'PLAT-540']


class Command(BaseCommand):
    help = 'PLS-214: initialize recurring_cycle metadata for known stale recurring charters and lift backlog ones.'

    def add_arguments(self, parser):
        parser.add_argument('--keys', nargs='*', default=DEFAULT_KEYS)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        keys = options['keys'] or DEFAULT_KEYS
        dry_run = options['dry_run']
        now = timezone.now()
        touched = lifted = missing = skipped = 0
        for key in keys:
            item = Item.objects.filter(item_key=key, mode='task').first()
            if not item:
                missing += 1
                self.stdout.write(f'{key}: missing')
                continue
            if not item.is_recurring or not item.frequency or item.frequency == 'one_time':
                skipped += 1
                self.stdout.write(f'{key}: not recurring')
                continue
            metadata, cycle = _recurring_cycle_state(item)
            changed = False
            if int(cycle.get('consecutive_skips') or 0) < 2:
                cycle['consecutive_skips'] = 2
                cycle['last_skip_at'] = now.isoformat()
                cycle.setdefault('last_terminal_completion_at', (item.completed_at or item.created_at or now).isoformat())
                cycle.setdefault('escalated_at_skips', 0)
                metadata['recurring_cycle'] = cycle
                item.metadata = metadata
                changed = True
            update_fields = ['metadata'] if changed else []
            if (item.status or '').lower() == 'backlog':
                old_status = item.status
                item.status = _workflow_initial_status_slug(item, default='open')
                _sync_item_workflow_status(item, item.status)
                update_fields += ['status', 'workflow_status']
                lifted += 1
                changed = True
                if not dry_run:
                    _recurring_cycle_activity(item, 'recurring_cycle_fired', old_status, item.status, 'status')
            if changed:
                touched += 1
                if not dry_run:
                    item.save(update_fields=list(dict.fromkeys(update_fields + ['updated_at'])))
            self.stdout.write(f'{key}: touched={changed} status={item.status} skips={cycle.get("consecutive_skips")}')
        self.stdout.write(self.style.SUCCESS(
            f'PLS-214 backfill complete: touched={touched} lifted={lifted} missing={missing} skipped={skipped} dry_run={dry_run}'
        ))
