from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0201_plat10787_attestation_provider_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='SweepWorkLease',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('generation', models.PositiveBigIntegerField()),
                ('holder', models.CharField(max_length=128)),
                ('expires_at', models.DateTimeField()),
                ('expired_loudly', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('item', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sweep_work_lease', to='tasks.item')),
            ],
            options={'db_table': 'tasks_sweep_work_lease'},
        ),
    ]
