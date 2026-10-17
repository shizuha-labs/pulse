"""PLAT-4213: make TeamRoster names unique inside an organization, not globally."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0096_team_capability_profile'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teamroster',
            name='team_name',
            field=models.CharField(db_index=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name='teamroster',
            constraint=models.UniqueConstraint(
                fields=('organization_slug', 'team_name'),
                name='uniq_team_roster_org_name',
            ),
        ),
    ]
