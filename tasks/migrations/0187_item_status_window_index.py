"""Bound status-window metrics without indexing unrelated audit activity.

The existing item/time index supports per-item history. Organization window
reads otherwise probe it once per touched item and filter unrelated fields.
Build the selective time index concurrently so normal activity writes remain
available during the governed deployment migration. Reverse drops it
concurrently; no history or metric semantics change.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('tasks', '0186_deferred_workflow_command')]
    operations = [
        AddIndexConcurrently(
            model_name='itemactivity',
            index=models.Index(
                fields=['created_at', 'item', 'id'],
                condition=models.Q(field_name='Status'),
                name='item_status_time_item_idx',
            ),
        ),
    ]
