"""Index the canonical heartbeat owner lookup (PLS-698).

Agent queues use ``assignee_email`` as the cross-service identity source of
truth, then narrow by mode/archive/status before applying blocker annotations
and computed ordering.  The missing owner index made every synchronized fleet
heartbeat scan the full task table (twice when the permission hint was enabled).

Build concurrently: Pulse remains writable while PostgreSQL indexes the live
table.  ``AddIndexConcurrently`` also records the normal Django model state, so
``makemigrations --check`` and later schema changes see one canonical index.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('tasks', '0133_pls467_item_state_generation'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='item',
            index=models.Index(
                fields=['assignee_email', 'mode', 'is_archived', 'status'],
                name='task_owner_active_queue_idx',
            ),
        ),
    ]
