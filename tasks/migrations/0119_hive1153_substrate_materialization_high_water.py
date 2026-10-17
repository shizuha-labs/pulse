# HIVE-1153 P1 #2 (HIVE-1174 Security decision): durable per-exact-seat monotonic
# high-water of the producer's signed materialization, so a superseded materialization
# (a newer iat verified inside the 5s window) HOLDs an older carried receipt at commit.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0119_pls474_versioned_implementation_return'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubstrateMaterializationHighWater',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('producer_generation_id', models.CharField(max_length=128)),
                ('organization_id', models.PositiveIntegerField()),
                ('subject', models.CharField(max_length=254)),
                ('team_slug', models.CharField(blank=True, default='', max_length=100)),
                ('iat', models.PositiveBigIntegerField()),
                ('materialization_generation', models.CharField(max_length=128)),
                ('ambiguous', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'tasks_substrate_materialization_high_water',
            },
        ),
        migrations.AddConstraint(
            model_name='substratematerializationhighwater',
            constraint=models.UniqueConstraint(
                fields=('producer_generation_id', 'organization_id', 'subject', 'team_slug'),
                name='uq_substrate_matgen_seat'),
        ),
    ]
