from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0179_merge_0178_leaves'),
    ]

    operations = [
        migrations.CreateModel(
            name='OperatorWaitDigestState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_comment_order', models.PositiveBigIntegerField(default=0)),
                ('source_revision', models.CharField(blank=True, default='', max_length=64)),
                ('source_message_id', models.CharField(blank=True, default='', max_length=64)),
                ('state', models.CharField(choices=[('active', 'Active'), ('withdrawn', 'Withdrawn')], default='active', max_length=16)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('digest_item', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='operator_wait_digest_state', to='tasks.item')),
                ('source_comment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='tasks.comment')),
            ],
            options={'db_table': 'tasks_operator_wait_digest_state'},
        ),
        migrations.CreateModel(
            name='OperatorWaitMarker',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_revision', models.CharField(max_length=64)),
                ('source_message_id', models.CharField(max_length=64)),
                ('lineage_item_ids', models.JSONField(default=list)),
                ('deadline_at', models.DateTimeField(blank=True, null=True)),
                ('state', models.CharField(choices=[('active', 'Active'), ('inactive', 'Inactive')], default='active', max_length=16)),
                ('generation', models.PositiveIntegerField(default=1)),
                ('activated_at', models.DateTimeField(auto_now_add=True)),
                ('invalidated_at', models.DateTimeField(blank=True, null=True)),
                ('invalidated_reason', models.CharField(blank=True, default='', max_length=80)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('digest_comment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='operator_wait_markers', to='tasks.comment')),
                ('digest_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='digest_operator_wait_markers', to='tasks.item')),
                ('item', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='operator_wait_marker', to='tasks.item')),
                ('surfaced_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='surfaced_operator_wait_markers', to='tasks.item')),
            ],
            options={'db_table': 'tasks_operator_wait_marker'},
        ),
        migrations.AddIndex(
            model_name='operatorwaitmarker',
            index=models.Index(fields=['state', 'deadline_at'], name='tasks_opera_state_9a160c_idx'),
        ),
        migrations.AddIndex(
            model_name='operatorwaitmarker',
            index=models.Index(fields=['digest_item', 'state'], name='tasks_opera_digest__0e7df2_idx'),
        ),
    ]
