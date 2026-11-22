"""PLAT-10624: scope create idempotency to the creator, not globally.

0190 added a global unique. Two callers reusing a key must not collide.
The partial unique is (created_by_id, client_request_id).
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tasks', '0190_plat10624_item_client_request_id')]
    operations = [
        migrations.AlterField(
            model_name='item',
            name='client_request_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Optional caller idempotency key for task create retries.',
                max_length=64,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name='item',
            constraint=models.UniqueConstraint(
                condition=models.Q(client_request_id__isnull=False) & ~models.Q(client_request_id=''),
                fields=('created_by_id', 'client_request_id'),
                name='tasks_item_creator_request_id_uniq',
            ),
        ),
    ]
