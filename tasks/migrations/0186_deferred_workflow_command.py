"""Queue graph mutations before changing the root; keep PLAT-4215 unchanged."""
import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('tasks', '0185_plat9681_fleetwide_terminal_reopen')]
    operations = [
        migrations.CreateModel(
            name='DeferredWorkflowCommand',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_id', models.IntegerField(null=True)),
                ('actor_id', models.IntegerField()),
                ('actor_email', models.EmailField(max_length=254)),
                ('kind', models.CharField(max_length=16)),
                ('payload', models.JSONField(default=dict)),
                ('source_generation', models.PositiveBigIntegerField()),
                ('graph_digest', models.CharField(max_length=64)),
                ('transition_digest', models.CharField(blank=True, max_length=64)),
                ('request_digest', models.CharField(max_length=64)),
                ('state', models.CharField(choices=[('pending', 'Pending'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('stale', 'Stale')], db_index=True, default='pending', max_length=16)),
                ('result', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('external_token', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='tasks.externalmcptoken')),
                ('item', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deferred_workflow_commands', to='tasks.item')),
            ],
        ),
        migrations.AddConstraint(
            model_name='deferredworkflowcommand',
            constraint=models.UniqueConstraint(condition=models.Q(state='pending'), fields=('item', 'request_digest'), name='uniq_pending_workflow_command'),
        ),
    ]
