from django.db import migrations

TERMINAL_STATUSES = {
    'completed', 'cancelled', 'closed', 'done', 'resolved', 'rejected',
    'duplicate', 'wont_fix', 'merged', 'failed', 'expired', 'deferred',
}


def _norm(value):
    return (value or '').strip().lower()


def repair_identity_drift(apps, schema_editor):
    """PLAT-192 deploy-time backfill for assignee/created_by drift.

    Rules match the operator directive and repair command:
    - both id+email but mismatch => trust id and re-derive email;
    - id only => fill email;
    - email only => fill id;
    - unresolvable rows are skipped rather than guessed.
    """
    Item = apps.get_model('tasks', 'Item')
    try:
        from tasks.user_service import user_service
    except Exception:  # pragma: no cover - migration must be best-effort safe
        return

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

    qs = Item.objects.all().only(
        'id', 'status',
        'assignee_id', 'assignee_email',
        'created_by_id', 'created_by_email',
    )
    # Repair terminal rows too: drift is an identity-integrity problem, and the
    # migration is idempotent. The management command defaults to active-only for
    # operator safety, but deploy-time backfill should leave the table coherent.
    for item in qs.iterator():
        updates = {}
        for field in ('assignee', 'created_by'):
            uid_attr = f'{field}_id'
            email_attr = f'{field}_email'
            uid = getattr(item, uid_attr)
            email = _norm(getattr(item, email_attr))

            if uid and email:
                canon_email = canonical_email(uid)
                if canon_email and canon_email != email:
                    updates[email_attr] = canon_email
                continue

            if uid and not email:
                canon_email = canonical_email(uid)
                if canon_email:
                    updates[email_attr] = canon_email
                continue

            if email and not uid:
                canon_id = canonical_id(email)
                if canon_id:
                    updates[uid_attr] = canon_id

        if updates:
            Item.objects.filter(pk=item.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0060_merge_20260615_0000'),
    ]

    operations = [
        migrations.RunPython(repair_identity_drift, migrations.RunPython.noop),
    ]
