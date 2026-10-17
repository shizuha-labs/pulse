from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0135_pls705_trigram_search_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='teamroster',
            name='version',
            field=models.PositiveBigIntegerField(default=0),
        ),
        # Historical migration-replay tests execute earlier TeamRoster model
        # states against the current schema.  Those states cannot name this new
        # column, so retain a DB default instead of letting an omitted value
        # become NULL.  Live model writes still advance it in TeamRoster.save().
        migrations.RunSQL(
            sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN version SET DEFAULT 0'
            ),
            reverse_sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN version DROP DEFAULT'
            ),
        ),
        migrations.CreateModel(
            name='TeamRosterCommandReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('command_id', models.UUIDField(db_index=True, unique=True)),
                ('payload_hash', models.CharField(max_length=64)),
                ('organization_id', models.PositiveIntegerField()),
                ('organization_slug', models.CharField(max_length=64)),
                ('team_name', models.CharField(max_length=64)),
                ('email', models.EmailField(blank=True, default='', max_length=254)),
                ('action', models.CharField(max_length=16)),
                ('expected_version', models.PositiveBigIntegerField()),
                ('previous_version', models.PositiveBigIntegerField()),
                ('result_version', models.PositiveBigIntegerField()),
                ('result', models.JSONField(default=dict)),
                ('actor_email', models.EmailField(blank=True, default='', max_length=254)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'tasks_team_roster_command_receipt',
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
