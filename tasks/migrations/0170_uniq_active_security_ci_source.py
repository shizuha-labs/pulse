"""One active security-ci item per (source, source_id).

Weekly security-ci ledger scans were reminting a new Pulse task whenever
``pulse_find_existing`` AND-combined ``source_id=`` with ``search=`` (ledger
titles do not contain the source_id token). Collapse extras first so the
unique constraint can land on a database that already has duplicates.
"""
from collections import defaultdict

from django.db import migrations, models


NON_ACTIVE = (
    'done', 'completed', 'closed', 'cancelled', 'canceled',
    'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
    'failed', 'deferred', 'expired',
)
STATUS_SCORE = {
    'in_progress': 50,
    'under_review': 45,
    'in_review': 40,
    'accepted': 30,
    'todo': 20,
    'found': 20,
    'open': 20,
    'blocked': 10,
}


def collapse_security_ci_source_id_dupes(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    ItemLink = apps.get_model('tasks', 'ItemLink')
    qs = (
        Item.objects.filter(source='security-ci')
        .exclude(source_id__isnull=True)
        .exclude(source_id='')
        .exclude(status__in=NON_ACTIVE)
    )
    groups = defaultdict(list)
    for item in qs:
        groups[item.source_id].append(item)
    for source_id, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: (-STATUS_SCORE.get(item.status, 0), item.created_at or item.pk))
        keeper = items[0]
        for extra in items[1:]:
            extra.status = 'done' if extra.status in ('in_review', 'under_review') else 'cancelled'
            extra.save(update_fields=['status'])
            ItemLink.objects.filter(
                models.Q(source_item=extra, link_type='is_blocked_by')
                | models.Q(target_item=extra, link_type='blocks')
            ).delete()


def noop_reverse(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0169_pls960_rfc_publication_binding_path'),
    ]

    operations = [
        migrations.RunPython(collapse_security_ci_source_id_dupes, noop_reverse),
        migrations.AddConstraint(
            model_name='item',
            constraint=models.UniqueConstraint(
                fields=('source', 'source_id'),
                condition=(
                    models.Q(source='security-ci')
                    & models.Q(source_id__isnull=False)
                    & ~models.Q(status__in=[
                        'done', 'completed', 'closed', 'cancelled', 'canceled',
                        'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                        'failed', 'deferred', 'expired',
                    ])
                ),
                name='uniq_active_security_ci_source',
            ),
        ),
    ]
