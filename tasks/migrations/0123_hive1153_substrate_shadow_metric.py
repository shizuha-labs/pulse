from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0122_hive1153_widen_receipt_requirement_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubstrateShadowMetric',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('outcome', models.CharField(max_length=32)),
                ('reason', models.CharField(max_length=100)),
                ('mismatch', models.BooleanField(default=False)),
                ('observations', models.PositiveBigIntegerField(default=0)),
                ('first_seen_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'tasks_substrate_shadow_metric'},
        ),
        migrations.AddConstraint(
            model_name='substrateshadowmetric',
            constraint=models.UniqueConstraint(
                fields=('outcome', 'reason', 'mismatch'),
                name='uq_substrate_shadow_metric_labels'),
        ),
    ]
