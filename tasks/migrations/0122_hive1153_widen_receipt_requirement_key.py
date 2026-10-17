from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0121_hive1153_substrate_write_receipt_arming'),
    ]

    operations = [
        migrations.AlterField(
            model_name='substratewritereceipt',
            name='requirement_key',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
    ]
