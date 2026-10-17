from django.db import migrations
from django.db.models import Q


def backfill_completed_at(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    ItemActivity = apps.get_model('tasks', 'ItemActivity')
    Status = apps.get_model('tasks', 'Status')

    done_rows = list(Status.objects.filter(category='done').values_list('slug', 'name'))
    done_slugs = {slug for slug, _ in done_rows}
    done_labels = {
        label
        for slug, name in done_rows
        for label in (slug, name)
        if label
    }
    done_slugs.update({
        'completed', 'done', 'resolved', 'merged', 'closed', 'cancelled',
        'deferred', 'rejected', 'wont_fix', 'duplicate', 'failed', 'expired',
    })
    done_labels.update(done_slugs)

    for item in (
        Item.objects
        .filter(mode='task', completed_at__isnull=True, status__in=done_slugs)
        .only('id', 'status', 'updated_at')
        .iterator()
    ):
        activity = (
            ItemActivity.objects
            .filter(item_id=item.id)
            .filter(
                Q(action='completed') |
                Q(field_name='Status', new_value__in=done_labels)
            )
            .order_by('-created_at')
            .first()
        )
        Item.objects.filter(pk=item.id, completed_at__isnull=True).update(
            completed_at=activity.created_at if activity else item.updated_at,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0073_pls183_deployment_event'),
    ]

    operations = [
        migrations.RunPython(backfill_completed_at, migrations.RunPython.noop),
    ]
