"""PLS-675 / Rei P1: backfill Item.acceptance_mode for existing task rows.

Migration 0141 added a nullable field. Enforcement requires an explicit value
on every governed task. This data migration:

  1. Copies a valid metadata.acceptance_mode mirror onto the field when present.
  2. Else sets live for service-affecting / user-facing / incident labels or
     incident workflow.
  3. Else sets code.

Idempotent — rows that already have a valid field value are left alone.
"""
from django.db import migrations

VALID = frozenset({'code', 'deployed', 'live'})
SA_LABELS = frozenset({
    'service-affecting', 'service-affecting-bug', 'incident', 'prod',
    'production', 'outage', 'deploy-verification', 'regression',
    'sev1', 'sev2', 'sev-1', 'sev-2', 'p0', 'p1-incident',
    'user-facing', 'user_facing', 'ux', 'frontend-user', 'customer-facing',
})


def _label_tokens(value):
    if not value:
        return set()
    if isinstance(value, dict):
        value = list(value.keys())
    elif isinstance(value, str):
        value = [v.strip() for v in value.split(',') if v.strip()]
    elif not isinstance(value, (list, tuple, set)):
        return set()
    return {str(v).strip().lower() for v in value if str(v).strip()}


def backfill(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    for item in Item.objects.filter(mode='task').iterator(chunk_size=500):
        current = (item.acceptance_mode or '').strip().lower()
        if current in VALID:
            continue
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        meta_mode = str(meta.get('acceptance_mode') or '').strip().lower()
        if meta_mode in VALID:
            item.acceptance_mode = meta_mode
        else:
            toks = _label_tokens(item.labels) | _label_tokens(item.tags)
            wf = (item.workflow_name or '').strip().lower().replace(' ', '-')
            if wf == 'incident' or (toks & SA_LABELS):
                item.acceptance_mode = 'live'
            else:
                item.acceptance_mode = 'code'
        item.save(update_fields=['acceptance_mode'])


def noop_reverse(apps, schema_editor):
    # Leaving values in place is safe; field remains nullable.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0148_pls675_awaiting_deploy_path'),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
