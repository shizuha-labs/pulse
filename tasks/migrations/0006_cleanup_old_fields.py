# Cleanup legacy fields that are no longer in the model

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0005_add_missing_fields'),
    ]

    operations = [
        # SQLite cannot DROP a column that still has an index; remove the index first.
        migrations.RemoveIndex(
            model_name='item',
            name='tasks_task_task_ty_45f0a7_idx',
        ),
        # Remove old task_type field (replaced by mode + item_type)
        migrations.RemoveField(
            model_name='item',
            name='task_type',
        ),
    ]
