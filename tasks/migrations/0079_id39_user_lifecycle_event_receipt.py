# Generated for ID-39 Pulse user lifecycle event idempotency
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0078_merge_20260703_0550'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserLifecycleEventReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.UUIDField(db_index=True, unique=True)),
                ('event_type', models.CharField(choices=[('user.deactivated', 'User deactivated'), ('user.deleted', 'User deleted'), ('user.reactivated', 'User reactivated')], db_index=True, max_length=40)),
                ('user_id', models.IntegerField(db_index=True)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('items_unassigned', models.PositiveIntegerField(default=0)),
                ('processed_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'ordering': ['-processed_at'],
                'indexes': [models.Index(fields=['event_type', 'user_id'], name='tasks_userl_event_t_9f9209_idx')],
            },
        ),
    ]
