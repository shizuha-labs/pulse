# Generated for PLS-304 external MCP gateway MVP.
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0083_pls240_reopen_terminal_review'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalMcpClient',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('org_id', models.IntegerField(db_index=True)),
                ('name', models.CharField(max_length=255)),
                ('owner_user_id', models.IntegerField(blank=True, null=True)),
                ('owner_email', models.EmailField(blank=True, max_length=254)),
                ('allowed_project_ids', models.JSONField(blank=True, default=list)),
                ('scopes', models.JSONField(blank=True, default=list)),
                ('rate_limit_policy', models.JSONField(blank=True, default=dict)),
                ('disabled_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'tasks_external_mcp_client',
            },
        ),
        migrations.CreateModel(
            name='ExternalMcpToken',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token_hash', models.CharField(db_index=True, max_length=64, unique=True)),
                ('scopes', models.JSONField(blank=True, default=list)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('created_by_id', models.IntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tokens', to='tasks.externalmcpclient')),
            ],
            options={
                'db_table': 'tasks_external_mcp_token',
            },
        ),
        migrations.CreateModel(
            name='ExternalMcpCallAudit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('org_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('actor_user_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('tool_name', models.CharField(db_index=True, max_length=100)),
                ('task_id', models.CharField(blank=True, max_length=100)),
                ('idempotency_key', models.CharField(blank=True, max_length=255)),
                ('request_hash', models.CharField(blank=True, max_length=64)),
                ('result_status', models.CharField(choices=[('success', 'Success'), ('denied', 'Denied'), ('error', 'Error')], max_length=20)),
                ('error_code', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('client', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_entries', to='tasks.externalmcpclient')),
            ],
            options={
                'ordering': ['-created_at'],
                'db_table': 'tasks_external_mcp_call_audit',
            },
        ),
        migrations.AddIndex(
            model_name='externalmcpclient',
            index=models.Index(fields=['org_id', 'disabled_at'], name='tasks_exter_org_id_5c5d9d_idx'),
        ),
        migrations.AddIndex(
            model_name='externalmcptoken',
            index=models.Index(fields=['client', 'revoked_at'], name='tasks_exter_client__6ca132_idx'),
        ),
        migrations.AddIndex(
            model_name='externalmcptoken',
            index=models.Index(fields=['expires_at'], name='tasks_exter_expires_7cc747_idx'),
        ),
        migrations.AddIndex(
            model_name='externalmcpcallaudit',
            index=models.Index(fields=['client', 'created_at'], name='tasks_exter_client__352bb2_idx'),
        ),
        migrations.AddIndex(
            model_name='externalmcpcallaudit',
            index=models.Index(fields=['org_id', 'tool_name'], name='tasks_exter_org_id_caf465_idx'),
        ),
    ]
