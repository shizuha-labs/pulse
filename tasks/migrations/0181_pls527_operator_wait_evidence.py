from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0180_pls529_operator_wait_markers'),
    ]

    operations = [
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='evidence_fingerprint',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='evidence_source',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='evidence_checklist',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='evidence_recorded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='evidence_unchanged_probes',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='operatorwaitmarker',
            name='verification_queued_generation',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
