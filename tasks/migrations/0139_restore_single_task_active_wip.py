from django.db import migrations, models


def restore_single_task_active_wip(apps, schema_editor):
    """Re-arm PLS-509 for rosters that still use the unlimited sentinel.

    Hive's live parallel-execution contract reports effective_max_tasks=1 for
    the fleet.  Keep explicit non-zero operator overrides intact and only
    repair the historical zero written by migration 0110.
    """
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        is_active=True,
        wip_enforced=True,
        active_wip_cap=0,
    ).update(active_wip_cap=1)


def restore_unlimited_active_wip(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        is_active=True,
        wip_enforced=True,
        active_wip_cap=1,
    ).update(active_wip_cap=0)


class Migration(migrations.Migration):
    dependencies = [('tasks', '0138_alter_comment_origin_kind')]

    operations = [
        migrations.AlterField(
            model_name='teamroster',
            name='active_wip_cap',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunSQL(
            sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap SET DEFAULT 1;'
            ),
            reverse_sql=(
                'ALTER TABLE tasks_team_roster '
                'ALTER COLUMN active_wip_cap SET DEFAULT 0;'
            ),
        ),
        migrations.RunPython(
            restore_single_task_active_wip,
            restore_unlimited_active_wip,
        ),
    ]
