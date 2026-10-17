# Generated for PLS-307: citation-backed AI recap generation records.
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0086_merge_20260705_1059'),
    ]

    operations = [
        migrations.CreateModel(
            name='AiGenerationRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('org_id', models.IntegerField(db_index=True, help_text='Organization ID from shizuha-admin')),
                ('type', models.CharField(choices=[('sprint', 'Sprint recap'), ('project', 'Project recap'), ('daily', 'Daily standup'), ('risk', 'Risk digest')], db_index=True, default='project', max_length=20)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], db_index=True, default='queued', max_length=20)),
                ('model', models.CharField(default='deterministic-pulse-recap-v1', max_length=100)),
                ('prompt_template_version', models.CharField(default='pulse-recap-v1', max_length=100)),
                ('source_window_start', models.DateTimeField(db_index=True)),
                ('source_window_end', models.DateTimeField(db_index=True)),
                ('source_snapshot_hash', models.CharField(blank=True, max_length=64)),
                ('source_counts', models.JSONField(blank=True, default=dict)),
                ('output', models.JSONField(blank=True, default=dict)),
                ('citations', models.JSONField(blank=True, default=list)),
                ('feedback', models.JSONField(blank=True, default=list)),
                ('created_by_id', models.IntegerField(blank=True, null=True)),
                ('created_by_email', models.EmailField(blank=True, max_length=254, null=True)),
                ('client_id', models.CharField(blank=True, default='pulse-api', max_length=100)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('project', models.ForeignKey(help_text='Project whose readable Pulse evidence was summarized.', on_delete=django.db.models.deletion.CASCADE, related_name='ai_generation_runs', to='tasks.project')),
                ('sprint', models.ForeignKey(blank=True, help_text='Optional sprint scope for sprint/daily recaps.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ai_generation_runs', to='tasks.sprint')),
            ],
            options={
                'db_table': 'tasks_ai_generation_run',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org_id', 'project', 'created_at'], name='tasks_ai_ge_org_id_c2798f_idx'),
                    models.Index(fields=['project', 'type', 'status'], name='tasks_ai_ge_project_b4b49a_idx'),
                    models.Index(fields=['created_by_email', 'created_at'], name='tasks_ai_ge_created_733192_idx'),
                ],
            },
        ),
    ]
