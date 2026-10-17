"""
Tombstone orphaned item-state outbox rows (PLS-1310).

Background
----------
A hard item-delete that bypasses the ORM (the raw/SQL path operators currently
use) leaves the item's ``tasks_item_state_event_outbox`` rows behind as
*orphaned pending mutations for a nonexistent item* (live datum: item 36111,
one of the PLAT-9235/9238/9230 batch hard-deleted 01:05-02:12Z on 09-19). Any
future consumer that drains ``pending`` rows would process mutations for an
item that cannot exist. The ORM delete path is fixed at the source (pre_delete
receiver in ``tasks.signals`` + the SET_NULL FK), but rows orphaned before that
fix — and any the raw path creates going forward — need this pass.

What it does
------------
For every outbox row whose ``item_id`` no longer exists in ``tasks_task``:
set ``disposition='orphaned'`` and ``stale_reason='orphaned-by-hard-delete'``.
The rows are NOT deleted — the outbox is the only audit surface the hard-delete
path leaves, and the tombstone preserves the forensic trail (envelope, event
UUID, timestamps intact). Orphaned rows are inert: consumers drain 'pending'
only, and the disposition is self-describing for forensics.

Rows already tombstoned by the delete-time receiver (``stale_reason=
'item-hard-deleted'``) are left untouched; this command only claims rows the
raw path orphaned while still ``pending``/``applied``/``stale``/``quarantined``.

Dry-run by default (--commit to write).

Usage
-----
    python manage.py tombstone_orphaned_outbox            # dry-run report
    python manage.py tombstone_orphaned_outbox --commit   # write tombstones
    python manage.py tombstone_orphaned_outbox --json     # machine-readable
"""
import json

from django.core.management.base import BaseCommand
from django.db import models

from tasks.models import Item, ItemStateEventOutbox


class Command(BaseCommand):
    help = 'Tombstone outbox rows whose owning item no longer exists (PLS-1310)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Write the tombstones (default is a dry-run report)')
        parser.add_argument(
            '--json', action='store_true',
            help='Machine-readable report on stdout')

    def handle(self, *args, **options):
        commit = options['commit']
        as_json = options['json']

        live_ids = Item.objects.values('pk')
        orphans = ItemStateEventOutbox.objects.exclude(
            item_id__in=live_ids).exclude(disposition='orphaned')

        by_disposition = {}
        sample_ids = []
        for row in orphans.order_by('created_at'):
            by_disposition[row.disposition] = by_disposition.get(row.disposition, 0) + 1
            if len(sample_ids) < 10:
                sample_ids.append(str(row.id))

        total = orphans.count()
        report = {
            'orphaned_rows': total,
            'by_prior_disposition': by_disposition,
            'sample_row_ids': sample_ids,
            'committed': bool(commit),
        }

        if commit and total:
            updated = orphans.update(
                disposition='orphaned',
                stale_reason='orphaned-by-hard-delete',
            )
            report['tombstoned'] = updated

        if as_json:
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(
                'PLS-1310 orphaned-outbox tombstone pass'))
            self.stdout.write(
                f"orphaned rows (item gone, disposition != 'orphaned'): {total}")
            for disp, n in sorted(by_disposition.items()):
                self.stdout.write(f"  prior disposition {disp!r}: {n}")
            if sample_ids:
                self.stdout.write(f"  sample row ids: {', '.join(sample_ids)}")
            if commit:
                self.stdout.write(self.style.SUCCESS(
                    f"tombstoned: {report.get('tombstoned', 0)}"))
            else:
                self.stdout.write(self.style.WARNING(
                    'dry-run — re-run with --commit to write tombstones'))
