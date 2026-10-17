"""
Repair PLAT-192 assignee/created_by identity drift using the operator directive.

Rules
-----
* If both id and email exist but disagree, trust the id and re-derive email.
* If only id exists, fill email from the id.
* If only email exists, resolve and fill id from the email.

Dry-run by default; pass --commit to write.  The command intentionally skips
unresolvable rows rather than guessing.
"""
import json

from django.core.management.base import BaseCommand

from tasks.models import Item
from tasks.user_service import user_service


TERMINAL_STATUSES = {
    'completed', 'cancelled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def _norm(value):
    return (value or '').strip().lower()


class Command(BaseCommand):
    help = (
        "Repair assignee/created_by identity drift (PLAT-192). Mismatch rows "
        "trust the numeric id and re-derive email; half-resolved rows fill the "
        "missing side. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true',
                            help='Actually write repairs to the DB (default: dry-run only).')
        parser.add_argument('--field', choices=['assignee', 'created_by', 'both'],
                            default='assignee',
                            help='Which identity pair(s) to repair (default: assignee).')
        parser.add_argument('--include-terminal', action='store_true',
                            help='Also repair terminal items (default: skip them).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after scanning N items (0 = no limit).')
        parser.add_argument('--json', action='store_true', dest='as_json',
                            help='Emit machine-readable JSON.')

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

        email_by_id = {}
        id_by_email = {}

        def canonical_email(uid):
            if not uid:
                return None
            uid = int(uid)
            if uid not in email_by_id:
                try:
                    info = user_service.get_user_by_id(uid)
                    email_by_id[uid] = _norm(info.get('email')) if info else None
                except Exception:
                    email_by_id[uid] = None
            return email_by_id[uid]

        def canonical_id(email):
            key = _norm(email)
            if not key:
                return None
            if key not in id_by_email:
                try:
                    info = user_service.get_user_by_email(key)
                    raw = info.get('id') if info else None
                    id_by_email[key] = int(raw) if raw is not None and str(raw).isdigit() else None
                except Exception:
                    id_by_email[key] = None
            return id_by_email[key]

        scanned = 0
        written = 0
        repairs = []
        skipped = []

        for item in qs.iterator():
            scanned += 1
            if limit and scanned > limit:
                scanned -= 1
                break

            update_fields = []
            item_changes = []

            for field in fields:
                uid_attr = f'{field}_id'
                email_attr = f'{field}_email'
                uid = getattr(item, uid_attr)
                email = _norm(getattr(item, email_attr))

                if uid and email:
                    canon_email = canonical_email(uid)
                    if canon_email is None:
                        skipped.append({
                            'item_key': item.item_key,
                            'field': field,
                            'reason': f'cannot resolve {uid_attr}={uid}',
                        })
                        continue
                    if canon_email != email:
                        setattr(item, email_attr, canon_email)
                        update_fields.append(email_attr)
                        item_changes.append({
                            'field': field,
                            'kind': 'email_id_mismatch',
                            'trusted': 'id',
                            'old_email': email,
                            'new_email': canon_email,
                            'id': uid,
                        })
                    continue

                if uid and not email:
                    canon_email = canonical_email(uid)
                    if canon_email is None:
                        skipped.append({
                            'item_key': item.item_key,
                            'field': field,
                            'reason': f'cannot resolve {uid_attr}={uid}',
                        })
                        continue
                    setattr(item, email_attr, canon_email)
                    update_fields.append(email_attr)
                    item_changes.append({
                        'field': field,
                        'kind': 'half_resolved_id_only',
                        'id': uid,
                        'new_email': canon_email,
                    })
                    continue

                if email and not uid:
                    canon_id = canonical_id(email)
                    if canon_id is None:
                        skipped.append({
                            'item_key': item.item_key,
                            'field': field,
                            'reason': f'cannot resolve {email_attr}={email}',
                        })
                        continue
                    setattr(item, uid_attr, canon_id)
                    update_fields.append(uid_attr)
                    item_changes.append({
                        'field': field,
                        'kind': 'half_resolved_email_only',
                        'email': email,
                        'new_id': canon_id,
                    })

            if update_fields:
                repairs.append({
                    'item_key': item.item_key,
                    'status': item.status,
                    'changes': item_changes,
                })
                if commit:
                    item.save(update_fields=sorted(set(update_fields)))
                    written += 1

        if as_json:
            self.stdout.write(json.dumps({
                'scanned': scanned,
                'repair_items': len(repairs),
                'written': written,
                'dry_run': not commit,
                'repairs': repairs,
                'skipped': skipped,
            }, indent=2))
            return

        verb = 'Repaired' if commit else 'Would repair'
        self.stdout.write(
            f'Scanned {scanned} item(s); {len(repairs)} item(s) need repair; '
            f'{verb} {written if commit else len(repairs)} item(s); '
            f'{len(skipped)} skipped.'
        )
        for repair in repairs:
            for change in repair['changes']:
                self.stdout.write(
                    f"  {repair['item_key'] or '<no-key>':<12} [{repair['status']}] "
                    f"{change['field']}: {change['kind']} {change}"
                )
        if skipped:
            self.stdout.write('Skipped unresolved rows:')
            for row in skipped:
                self.stdout.write(f"  {row['item_key'] or '<no-key>':<12} {row['field']}: {row['reason']}")
        if repairs and not commit:
            self.stdout.write('Dry run — pass --commit to write these repairs.')
