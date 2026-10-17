"""
Repair drifted assignee_id values by deriving the canonical shizuha-id from
assignee_email via the shizuha-id user-service (PLS-23).

Background
----------
Before the FederatedUser auth fix, tasks could be written with assignee_id set
to a Django local auto-increment pk instead of the shizuha-id numeric ID. Those
two ID spaces collide (e.g. Django pk=3 is admin@example.com; shizuha-id=3 is
operator@example.test), so WIP-cap accounting and assigned_to_me queries could
return tasks for the wrong person.

This command is the one-time repair: for every non-terminal Item where
assignee_email is set, look up the canonical shizuha-id for that email. If the
stored assignee_id does NOT match, update it.  created_by_id is repaired in the
same pass when --field=both is passed.

Dry-run by default (--commit to write).

Usage
-----
    python manage.py backfill_assignee_ids                  # dry-run, assignee only
    python manage.py backfill_assignee_ids --field both     # assignee + created_by
    python manage.py backfill_assignee_ids --commit         # write to DB
    python manage.py backfill_assignee_ids --include-terminal --commit
    python manage.py backfill_assignee_ids --limit 500 --commit
    python manage.py backfill_assignee_ids --json           # machine-readable report
"""
import json

from django.core.management.base import BaseCommand

from tasks.models import Item
from tasks.user_service import user_service


TERMINAL_STATUSES = {
    'completed', 'cancelled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def _norm(v):
    return (v or '').strip().lower()


class Command(BaseCommand):
    help = (
        "Repair drifted assignee_id (and optionally created_by_id) values by "
        "re-deriving them from the canonical email via shizuha-id (PLS-23). "
        "Dry-run by default — pass --commit to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Actually write repairs to the DB (default: dry-run only).',
        )
        parser.add_argument(
            '--field', choices=['assignee', 'created_by', 'both'], default='assignee',
            help='Which identity pair(s) to repair (default: assignee).',
        )
        parser.add_argument(
            '--include-terminal', action='store_true',
            help='Also repair items in terminal statuses (default: skip them).',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Stop after scanning N items (0 = no limit).',
        )
        parser.add_argument(
            '--json', action='store_true', dest='as_json',
            help='Emit machine-readable JSON instead of a human report.',
        )

    def handle(self, *args, **opts):
        fields = ['assignee', 'created_by'] if opts['field'] == 'both' else [opts['field']]
        commit = opts['commit']
        as_json = opts['as_json']
        limit = opts['limit']

        qs = Item.objects.all().only(
            'id', 'item_key', 'status',
            'assignee_id', 'assignee_email',
            'created_by_id', 'created_by_email',
        )
        if not opts['include_terminal']:
            qs = qs.exclude(status__in=TERMINAL_STATUSES)

        # Cache shizuha-id lookups to avoid hammering the user-service for
        # the same email repeated many times.
        id_cache = {}  # email → shizuha-id int | None

        def canonical_id(email):
            key = _norm(email)
            if not key:
                return None
            if key not in id_cache:
                try:
                    info = user_service.get_user_by_email(key)
                    raw = info.get('id') if info else None
                    id_cache[key] = int(raw) if raw is not None and str(raw).isdigit() else None
                except Exception:
                    id_cache[key] = None
            return id_cache[key]

        repairs = []   # list of dicts describing each repair
        scanned = 0
        written = 0

        for item in qs.iterator():
            scanned += 1
            if limit and scanned > limit:
                scanned -= 1
                break

            update_fields = []
            item_repairs = []

            for field in fields:
                email = _norm(getattr(item, f'{field}_email'))
                if not email:
                    continue  # nothing to derive from
                stored_id = getattr(item, f'{field}_id')
                canon = canonical_id(email)
                if canon is None:
                    continue  # user-service miss — skip, don't corrupt
                if stored_id == canon:
                    continue  # already correct

                item_repairs.append({
                    'field': field,
                    'email': email,
                    'old_id': stored_id,
                    'new_id': canon,
                })
                setattr(item, f'{field}_id', canon)
                update_fields.append(f'{field}_id')

            if update_fields:
                repairs.append({
                    'item_key': item.item_key,
                    'status': item.status,
                    'changes': item_repairs,
                })
                if commit:
                    item.save(update_fields=update_fields)
                    written += 1

        if as_json:
            self.stdout.write(json.dumps({
                'scanned': scanned,
                'drifted': len(repairs),
                'written': written,
                'dry_run': not commit,
                'repairs': repairs,
            }, indent=2))
            return

        verb = 'Repaired' if commit else 'Would repair'
        self.stdout.write(
            f'Scanned {scanned} item(s); {len(repairs)} drifted. '
            f'{verb} {written if commit else len(repairs)} item(s).'
        )
        for r in repairs:
            for c in r['changes']:
                self.stdout.write(
                    f"  {r['item_key']:<12} [{r['status']}] {c['field']}: "
                    f"id {c['old_id']!r} → {c['new_id']} (email={c['email']})"
                )
        if repairs and not commit:
            self.stdout.write('')
            self.stdout.write("Dry run — pass --commit to write these repairs.")
