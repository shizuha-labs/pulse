import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """PLS-521: durable claim state for concurrent HOLD notification relays."""

    dependencies = [
        ('tasks', '0153_pls521_merge_attestation'),
    ]

    operations = [
        migrations.AddField(
            model_name='deploymentevidencegateoutbox',
            name='attempts',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='deploymentevidencegateoutbox',
            name='in_flight',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='deploymentevidencegateoutbox',
            name='in_flight_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='deploymentevidencegateoutbox',
            name='last_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='deploymentevidencegateoutbox',
            name='next_attempt_at',
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
            ),
        ),
    ]
