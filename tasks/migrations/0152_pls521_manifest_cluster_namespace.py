from django.db import migrations, models


class Migration(migrations.Migration):
    """PLS-521 P1 #3: bind the reviewed cluster + namespace into the verification
    requirement manifest so the deployed-evidence gate can reject evidence that
    lands in a different cluster/namespace even when env/service/workload match."""

    dependencies = [
        ('tasks', '0151_pls521_deployed_evidence_gate'),
    ]

    operations = [
        migrations.AddField(
            model_name='verificationrequirementmanifest',
            name='cluster',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='verificationrequirementmanifest',
            name='namespace',
            field=models.CharField(blank=True, default='', max_length=160),
        ),
    ]
