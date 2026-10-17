from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tasks', '0051_agent_project_membership')]
    operations = [
        migrations.CreateModel(
            name='ProjectTeamLead',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('org_id', models.IntegerField(db_index=True, default=0)),
                ('project_key', models.CharField(db_index=True, max_length=16)),
                ('team_name', models.CharField(max_length=64)),
                ('lead_email', models.EmailField(max_length=254)),
                ('lead_id', models.IntegerField(blank=True, null=True)),
                ('elected_at', models.DateTimeField(auto_now_add=True)),
                ('is_active', models.BooleanField(default=True)),
                ('deactivated_reason', models.CharField(blank=True, default='', max_length=128)),
            ],
        ),
        migrations.AddConstraint(
            model_name='projectteamlead',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_active', True)),
                fields=('org_id', 'project_key', 'team_name'),
                name='uniq_active_project_team_lead',
            ),
        ),
    ]
