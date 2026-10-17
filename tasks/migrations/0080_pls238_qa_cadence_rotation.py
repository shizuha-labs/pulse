from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0079_id39_user_lifecycle_event_receipt'),
    ]

    operations = [
        migrations.CreateModel(
            name='QaCadenceRotation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('project_key', models.CharField(db_index=True, max_length=50)),
                ('tier', models.CharField(max_length=3)),
                ('cursor', models.PositiveIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'unique_together': {('project_key', 'tier')},
            },
        ),
    ]
