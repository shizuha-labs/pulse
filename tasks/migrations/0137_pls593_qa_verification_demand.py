# Generated for PLS-593: DB-backed latest-wins QA verification demand.
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0136_pls745_team_roster_command_receipts'),
    ]

    operations = [
        migrations.CreateModel(
            name='QaVerificationDemand',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_id', models.IntegerField(db_index=True)),
                ('demand_kind', models.CharField(choices=[('regression_watchlist', 'Regression watchlist'), ('focused_qa', 'Focused QA')], max_length=40)),
                ('surface_key', models.CharField(max_length=1024)),
                ('environment', models.CharField(default='live', max_length=80)),
                ('primary_risk', models.CharField(blank=True, default='', max_length=120)),
                ('current_generation', models.PositiveBigIntegerField(default=0)),
                ('verified_generation', models.PositiveBigIntegerField(blank=True, null=True)),
                ('version', models.PositiveBigIntegerField(default=0)),
                ('last_event_at', models.DateTimeField(blank=True, null=True)),
                ('settle_after', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('active_item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='active_qa_verification_demands', to='tasks.item')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='qa_verification_demands', to='tasks.project')),
            ],
            options={'db_table': 'tasks_qa_verification_demand'},
        ),
        migrations.CreateModel(
            name='QaVerificationGeneration',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('generation', models.PositiveBigIntegerField()),
                ('commit_sha', models.CharField(max_length=64)),
                ('artifact_identity', models.CharField(blank=True, default='', max_length=1024)),
                ('event_fingerprint', models.CharField(blank=True, default='', max_length=64)),
                ('surface_sequence', models.DateTimeField()),
                ('state', models.CharField(choices=[('accepted', 'Accepted'), ('current', 'Current'), ('superseded', 'Superseded'), ('failed', 'Failed'), ('verified', 'Verified')], default='accepted', max_length=24)),
                ('accepted_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('demand', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='generations', to='tasks.qaverificationdemand')),
                ('deployment_event', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='qa_demand_generations', to='tasks.deploymentevent')),
            ],
            options={'db_table': 'tasks_qa_verification_generation'},
        ),
        migrations.CreateModel(
            name='QaVerificationOutbox',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('generation', models.PositiveBigIntegerField()),
                ('version', models.PositiveBigIntegerField()),
                ('available_at', models.DateTimeField(db_index=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('demand', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='outbox', to='tasks.qaverificationdemand')),
            ],
            options={'db_table': 'tasks_qa_verification_outbox'},
        ),
        migrations.CreateModel(
            name='QaVerificationAudit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('from_generation', models.PositiveBigIntegerField(default=0)),
                ('to_generation', models.PositiveBigIntegerField()),
                ('effect_kind', models.CharField(max_length=64)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('demand', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='audit_events', to='tasks.qaverificationdemand')),
                ('deployment_event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='qa_demand_audit_events', to='tasks.deploymentevent')),
                ('item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='qa_demand_audit_events', to='tasks.item')),
            ],
            options={'db_table': 'tasks_qa_verification_audit'},
        ),
        migrations.AddConstraint(
            model_name='qaverificationdemand',
            constraint=models.UniqueConstraint(fields=('organization_id', 'project', 'demand_kind', 'surface_key', 'environment', 'primary_risk'), name='uniq_qa_demand_identity'),
        ),
        migrations.AddIndex(
            model_name='qaverificationdemand',
            index=models.Index(fields=['demand_kind', 'settle_after'], name='qa_demand_settle_idx'),
        ),
        migrations.AddConstraint(
            model_name='qaverificationgeneration',
            constraint=models.UniqueConstraint(fields=('demand', 'generation'), name='uniq_qa_demand_generation'),
        ),
        migrations.AddConstraint(
            model_name='qaverificationgeneration',
            constraint=models.UniqueConstraint(fields=('demand', 'deployment_event'), name='uniq_qa_demand_event'),
        ),
        migrations.AddIndex(
            model_name='qaverificationgeneration',
            index=models.Index(fields=['demand', 'state'], name='qa_generation_state_idx'),
        ),
        migrations.AddConstraint(
            model_name='qaverificationoutbox',
            constraint=models.UniqueConstraint(fields=('demand', 'version'), name='uniq_qa_demand_outbox_version'),
        ),
        migrations.AddIndex(
            model_name='qaverificationoutbox',
            index=models.Index(fields=['delivered_at', 'available_at'], name='qa_outbox_ready_idx'),
        ),
        migrations.AddConstraint(
            model_name='qaverificationaudit',
            constraint=models.UniqueConstraint(fields=('demand', 'from_generation', 'to_generation', 'effect_kind'), name='uniq_qa_demand_audit_effect'),
        ),
        migrations.AddIndex(
            model_name='qaverificationaudit',
            index=models.Index(fields=['demand', 'created_at'], name='qa_audit_demand_idx'),
        ),
    ]
