# Generated manually for PLS-183 because the local heartbeat sandbox lacks the full runtime dependency set.
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0072_seed_feature_request_rfc_workflow'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeploymentEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('idempotency_key', models.CharField(db_index=True, max_length=512, unique=True)),
                ('repo', models.CharField(db_index=True, max_length=255)),
                ('branch', models.CharField(blank=True, default='', max_length=255)),
                ('commit_sha', models.CharField(db_index=True, max_length=64)),
                ('service', models.CharField(db_index=True, max_length=160)),
                ('component', models.CharField(blank=True, default='', max_length=160)),
                ('environment', models.CharField(db_index=True, default='live', max_length=80)),
                ('cluster', models.CharField(db_index=True, default='shizuha-k3s', max_length=120)),
                ('namespace', models.CharField(blank=True, default='', max_length=160)),
                ('deployment_target', models.CharField(blank=True, default='', max_length=255)),
                ('image', models.CharField(blank=True, default='', max_length=512)),
                ('image_tag', models.CharField(blank=True, default='', max_length=255)),
                ('image_digest', models.CharField(blank=True, default='', max_length=255)),
                ('manifest_ref', models.CharField(blank=True, default='', max_length=512)),
                ('pipeline_system', models.CharField(default='origin-forgejo', max_length=120)),
                ('pipeline_run_id', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('pipeline_url', models.URLField(blank=True, default='', max_length=2048)),
                ('logs_url', models.URLField(blank=True, default='', max_length=2048)),
                ('state', models.CharField(choices=[('created', 'Created'), ('build_started', 'Build Started'), ('build_failed', 'Build Failed'), ('manifest_failed', 'Manifest Failed'), ('artifact_published', 'Artifact Published'), ('rollout_started', 'Rollout Started'), ('rollout_stalled', 'Rollout Stalled'), ('smoke_pending', 'Smoke Pending'), ('smoke_passed', 'Smoke Passed'), ('smoke_failed', 'Smoke Failed'), ('rollback_started', 'Rollback Started'), ('rolled_back', 'Rolled Back'), ('superseded', 'Superseded'), ('qa_task_created', 'QA Task Created'), ('verified_by_qa', 'Verified By QA')], db_index=True, default='created', max_length=40)),
                ('smoke_result', models.CharField(choices=[('unknown', 'Unknown'), ('pending', 'Pending'), ('passed', 'Passed'), ('failed', 'Failed'), ('skipped', 'Skipped')], db_index=True, default='unknown', max_length=20)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('rollout_started_at', models.DateTimeField(blank=True, null=True)),
                ('rolled_out_at', models.DateTimeField(blank=True, null=True)),
                ('smoke_started_at', models.DateTimeField(blank=True, null=True)),
                ('smoke_finished_at', models.DateTimeField(blank=True, null=True)),
                ('pr_numbers', models.JSONField(blank=True, default=list)),
                ('source_task_keys', models.JSONField(blank=True, default=list)),
                ('links', models.JSONField(blank=True, default=dict)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('failure_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deployment_failure_events', to='tasks.item')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deployment_events', to='tasks.project')),
                ('qa_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deployment_qa_events', to='tasks.item')),
                ('rollback_of', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rollback_events', to='tasks.deploymentevent')),
                ('superseded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='superseded_events', to='tasks.deploymentevent')),
            ],
            options={
                'db_table': 'tasks_deployment_event',
                'ordering': ['-updated_at', '-created_at'],
                'indexes': [
                    models.Index(fields=['repo', 'service', 'environment', 'cluster'], name='tasks_deplo_repo_7e1d7a_idx'),
                    models.Index(fields=['commit_sha', 'image_digest'], name='tasks_deplo_commit_1f8f2c_idx'),
                    models.Index(fields=['state', 'smoke_result'], name='tasks_deplo_state_0e1fd5_idx'),
                    models.Index(fields=['updated_at'], name='tasks_deplo_updated_18bb51_idx'),
                ],
            },
        ),
    ]
