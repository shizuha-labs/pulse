from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0188_retire_waiting_external_entry'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProviderWaitMarker',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('signature_class', models.CharField(max_length=40)),
                ('stop_message_id', models.CharField(blank=True, default='', max_length=64)),
                ('state', models.CharField(choices=[('active', 'Active'), ('inactive', 'Inactive')], default='active', max_length=16)),
                ('generation', models.PositiveIntegerField(default=1)),
                ('activated_at', models.DateTimeField(auto_now_add=True)),
                ('last_set_at', models.DateTimeField(auto_now=True)),
                ('auto_cleared_at', models.DateTimeField(blank=True, null=True)),
                ('manager_cleared_at', models.DateTimeField(blank=True, null=True)),
                ('manager_cleared_by', models.CharField(blank=True, default='', max_length=254)),
                ('manager_cleared_action', models.CharField(blank=True, choices=[('take_over', 'Take over'), ('redispatch', 'Redispatch'), ('acknowledge', 'Acknowledge')], default='', max_length=20)),
                ('sweep_seen_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('item', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='provider_wait_marker', to='tasks.item')),
            ],
            options={
                'db_table': 'tasks_provider_wait_marker',
                'indexes': [models.Index(fields=['state', 'signature_class'], name='tasks_provw_state_9d1c44_idx')],
            },
        ),
    ]
