from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0128_pls597_seed_shizuha_decision_team'),
    ]

    operations = [
        migrations.CreateModel(
            name='ContainmentDecision',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state', models.CharField(choices=[('pending', 'Pending'), ('applied', 'Applied'), ('explicitly_not_required', 'Explicitly not required')], db_index=True, default='pending', max_length=40)),
                ('owner_id', models.IntegerField(blank=True, null=True)),
                ('owner_email', models.EmailField(blank=True, default='', max_length=254)),
                ('owner_group', models.CharField(blank=True, default='', max_length=100)),
                ('risk_markers', models.JSONField(blank=True, default=list)),
                ('requirement_active', models.BooleanField(default=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('evidence', models.JSONField(blank=True, default=dict)),
                ('version', models.PositiveIntegerField(default=1)),
                ('required_at', models.DateTimeField(auto_now_add=True)),
                ('decided_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('item', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='containment_decision', to='tasks.item')),
            ],
            options={
                'db_table': 'tasks_containment_decision',
                'indexes': [
                    models.Index(fields=['state', 'updated_at'], name='containment_state_updated_idx'),
                    models.Index(fields=['owner_group', 'state'], name='containment_group_state_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ContainmentDecisionEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField()),
                ('state', models.CharField(choices=[('pending', 'Pending'), ('applied', 'Applied'), ('explicitly_not_required', 'Explicitly not required')], max_length=40)),
                ('owner_id', models.IntegerField(blank=True, null=True)),
                ('owner_email', models.EmailField(blank=True, default='', max_length=254)),
                ('owner_group', models.CharField(blank=True, default='', max_length=100)),
                ('risk_markers', models.JSONField(blank=True, default=list)),
                ('requirement_active', models.BooleanField(default=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('evidence', models.JSONField(blank=True, default=dict)),
                ('trigger', models.CharField(blank=True, default='', max_length=80)),
                ('actor_id', models.IntegerField(blank=True, null=True)),
                ('actor_email', models.EmailField(blank=True, default='', max_length=254)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('decision', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='tasks.containmentdecision')),
            ],
            options={
                'db_table': 'tasks_containment_decision_event',
                'ordering': ['version'],
                'constraints': [
                    models.UniqueConstraint(fields=('decision', 'version'), name='unique_containment_decision_version'),
                ],
            },
        ),
    ]
