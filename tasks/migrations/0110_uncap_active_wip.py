from django.db import migrations, models


def uncap_active_wip(apps, schema_editor):
    """Remove the one-active-item bottleneck without disabling ready limits."""
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.all().update(active_wip_cap=0)


def restore_enforced_active_wip_cap(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(wip_enforced=True).update(active_wip_cap=1)


class Migration(migrations.Migration):
    dependencies = [('tasks', '0109_canonicalize_platform_ops_alias')]

    operations = [
        migrations.AlterField(
            model_name='teamroster',
            name='active_wip_cap',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        # Historical migration-state models can omit this column on INSERT.
        # Retain an explicit PostgreSQL default, matching migration 0107.
        migrations.RunSQL(
            sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap SET DEFAULT 0;'
            ),
            reverse_sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap SET DEFAULT 1;'
            ),
        ),
        migrations.RunPython(
            uncap_active_wip,
            restore_enforced_active_wip_cap,
        ),
    ]
