"""PLAT-4213: TeamRoster.organization_slug — single-team/single-org invariant."""
from django.db import migrations, models

ORG_SCOPED = {
    'digital-accounting': 'shizuha-digital',
    'digital-quant': 'shizuha-digital',
    'personal-accounting': 'personal-3',
    'securities-engineering': 'shizuha-trading',
}


def backfill(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for roster in TeamRoster.objects.all():
        roster.organization_slug = ORG_SCOPED.get(roster.team_name, 'shizuha')
        roster.save(update_fields=['organization_slug'])


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0093_pls425_preserve_owning_team_on_needs_info'),
    ]
    operations = [
        migrations.AddField(
            model_name='teamroster',
            name='organization_slug',
            field=models.CharField(blank=True, db_index=True, default='shizuha', max_length=64),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
