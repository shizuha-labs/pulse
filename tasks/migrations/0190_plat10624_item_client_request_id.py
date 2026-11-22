"""PLAT-10624: optional create idempotency key on Item.

Nullable so existing rows are unchanged. Unique so a retry of the same
client_request_id cannot insert a second task.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tasks', '0189_plat10083_provider_wait_marker')]
    operations = [
        migrations.AddField(
            model_name='item',
            name='client_request_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Optional caller idempotency key for task create retries.',
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
    ]
