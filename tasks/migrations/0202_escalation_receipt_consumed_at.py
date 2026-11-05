from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0201_plat10787_attestation_provider_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='escalationroutingreceipt',
            name='consumed_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='escalationroutingreceipt',
            name='consumed_by_transition_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='escalationroutingreceipt',
            name='dormancy_flagged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
