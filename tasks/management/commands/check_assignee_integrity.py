"""
Detect assignee/created_by identity DRIFT on Items (PLAT-192, slice 1 — read-only).

Background: assignee_id and assignee_email are written independently at many sites
(workflow post-functions, the /assign endpoint, _resolve_team_assignee, PATCH), so
the pair can diverge — e.g. assignee_email set to a routed team member while
assignee_id stays None or stale. That divergence is what made PLAT-257's labels-
only PATCH 403 for a task's own assignee: the participant check matched neither the
(stale/None) id nor the email. This command SURFACES that drift so it can be
repaired; it does NOT write anything (the atomic Item.assign() refactor + the
one-time repair migration + a divergence constraint are PLAT-192 slice 2, which is
schema-gated on the arch design and needs the in-container test env).

Two checks:
  * STRUCTURAL (default, no network): an id/email pair where exactly one side is
    set (id without email, or email without id) — a half-resolved assignment.
  * CANONICAL (--resolve, queries shizuha-id): assignee_email does not match the
    canonical email for assignee_id (the two identify different users).

Usage:
    python manage.py check_assignee_integrity                 # structural drift
    python manage.py check_assignee_integrity --resolve       # + canonical-email drift
    python manage.py check_assignee_integrity --field both     # assignee AND created_by
    python manage.py check_assignee_integrity --json           # machine-readable
"""
import json

from django.core.management.base import BaseCommand

from tasks.models import Item


# Terminal statuses are excluded by default — drift on a closed task is harmless
# and just noise. Pass --include-terminal to sweep everything.
TERMINAL_STATUSES = {
    'completed', 'cancelled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def _norm_email(value):
    return (value or '').strip().lower()


class Command(BaseCommand):
    help = (
        "Detect assignee/created_by identity drift on Items (PLAT-192, read-only). "
        "Structural by default; --resolve adds the canonical-email check via shizuha-id."
    )

    def add_arguments(self, parser):
        parser.add_argument('--resolve', action='store_true',
                            help='Also verify <field>_email matches the canonical email for <field>_id (queries shizuha-id).')
        parser.add_argument('--field', choices=['assignee', 'created_by', 'both'], default='assignee',
                            help='Which identity pair(s) to check (default: assignee).')
        parser.add_argument('--include-terminal', action='store_true',
                            help='Also check items in terminal statuses (default: skip them).')
        parser.add_argument('--json', action='store_true', dest='as_json',
                            help='Emit machine-readable JSON instead of a human report.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after scanning N items (0 = no limit).')

    def handle(self, *args, **opts):
        fields = ['assignee', 'created_by'] if opts['field'] == 'both' else [opts['field']]
        resolve = opts['resolve']
        as_json = opts['as_json']
        limit = opts['limit']

        qs = Item.objects.all().only(
            'id', 'item_key', 'status',
            'assignee_id', 'assignee_email', 'created_by_id', 'created_by_email',
        )
        if not opts['include_terminal']:
            qs = qs.exclude(status__in=TERMINAL_STATUSES)

        resolver = None
        email_cache = {}
        if resolve:
            from tasks.user_service import user_service as resolver  # noqa: N816

        def canonical_email(uid):
            if uid in email_cache:
                return email_cache[uid]
            info = None
            try:
                info = resolver.get_user_by_id(int(uid))
            except Exception:  # network/lookup failure → treat as unknown, don't crash the sweep
                info = None
            email = _norm_email(info.get('email')) if info else None
            email_cache[uid] = email
            return email

        findings = []  # list of dicts: {item_key, field, kind, detail}
        scanned = 0
        for item in qs.iterator():
            scanned += 1
            if limit and scanned > limit:
                scanned -= 1
                break
            for field in fields:
                uid = getattr(item, f'{field}_id')
                email = _norm_email(getattr(item, f'{field}_email'))

                # STRUCTURAL: exactly one side populated → half-resolved assignment.
                if bool(uid) != bool(email):
                    findings.append({
                        'item_key': item.item_key, 'status': item.status, 'field': field,
                        'kind': 'half_resolved',
                        'detail': f'{field}_id={uid!r} but {field}_email={email!r}',
                    })
                    continue

                # CANONICAL: both set, but email doesn't match the id's real email.
                if resolve and uid and email:
                    canon = canonical_email(uid)
                    if canon is not None and canon != email:
                        findings.append({
                            'item_key': item.item_key, 'status': item.status, 'field': field,
                            'kind': 'email_id_mismatch',
                            'detail': f'{field}_email={email!r} != canonical({field}_id={uid})={canon!r}',
                        })

        if as_json:
            self.stdout.write(json.dumps({
                'scanned': scanned,
                'drifted': len(findings),
                'resolve': resolve,
                'findings': findings,
            }, indent=2))
            return

        self.stdout.write(f'Scanned {scanned} item(s); {len(findings)} drift finding(s).')
        for f in findings:
            self.stdout.write(f"  {f['item_key']:<12} [{f['status']}] {f['field']}: {f['kind']} — {f['detail']}")
        if findings:
            self.stdout.write('')
            self.stdout.write('These are read-only findings. Repair is PLAT-192 slice 2 '
                              '(atomic Item.assign + one-time migration aligning email→id + a divergence constraint).')
        else:
            self.stdout.write('No assignee/created_by drift detected.')
