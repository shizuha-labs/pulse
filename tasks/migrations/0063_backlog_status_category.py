"""Make 'backlog' a non-active status category (owned-but-not-active roadmap).

The Backlog status existed but was miscategorized as 'todo', so it read as
active/ready work — every backlog item pressured the owner's heartbeat to
"advance it now" (root of the aoi 26-in_progress false-drain, 2026-06-23).
This recategorizes the Backlog status to the new 'backlog' category so it is
excluded from is_active/is_actionable queues and pulse_get_my_tasks. Pull a
backlog item into To Do / In Progress to make it active. Idempotent.
"""
from django.db import migrations


def set_backlog_category(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Status.objects.filter(slug='backlog').update(category='backlog', is_system=True)


def revert_backlog_category(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Status.objects.filter(slug='backlog').update(category='todo')


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0062_seed_human_ops_team_roster'),
    ]

    operations = [
        migrations.RunPython(set_backlog_category, revert_backlog_category),
    ]
