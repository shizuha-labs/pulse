from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0183_pls458_security_gate_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='wipslotreservation',
            name='scope_assertions',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
