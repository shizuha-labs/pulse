"""
Seed the canonical 'scheduled' Status and attach it to every existing Workflow.

Recurring tasks between cycles live in this status — see
WorkflowService.execute_transition and fire_recurring_due for the runtime
flow. Idempotent: re-running is safe.
"""
from django.db import migrations


SCHEDULED = {
    'slug': 'scheduled',
    'name': 'Scheduled',
    'category': 'scheduled',
    'color': '#a855f7',
    'position': 30,
    'is_system': True,
    'description': 'Waiting for next recurrence — timer-owned, no human action expected',
}


def seed(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')

    status, _ = Status.objects.update_or_create(
        slug=SCHEDULED['slug'],
        defaults={
            'name': SCHEDULED['name'],
            'category': SCHEDULED['category'],
            'color': SCHEDULED['color'],
            'position': SCHEDULED['position'],
            'is_system': SCHEDULED['is_system'],
            'description': SCHEDULED['description'],
        },
    )

    # Attach to every workflow so execute_transition can target it. Use a
    # high position (99) so it doesn't interleave between todo/in_progress/done.
    for wf in Workflow.objects.all():
        WorkflowStatus.objects.get_or_create(
            workflow=wf,
            status=status,
            defaults={'position': 99},
        )


def unseed(apps, schema_editor):
    # Non-destructive: leave data in place. Removing would orphan any Item
    # currently in this status.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0035_alter_status_category'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
