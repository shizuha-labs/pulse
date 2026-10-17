"""Backfill Scheduled gap state for workflows added after migration 0036.

PLS-221: reviewed-task was seeded in 0071, after 0036/0037 attached the
canonical Scheduled status and Run Now transition to then-existing workflows.
Recurring reviewed tasks therefore could not remain in Scheduled after cycle
completion; save-time coercion remapped them into active work and rebalance
churned the task. This migration is idempotent and covers all workflows.
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


def backfill(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    scheduled, _ = Status.objects.update_or_create(
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

    for wf in Workflow.objects.select_related('initial_status').all():
        WorkflowStatus.objects.get_or_create(
            workflow=wf,
            status=scheduled,
            defaults={'position': 99},
        )
        if not wf.initial_status_id:
            continue
        WorkflowTransition.objects.get_or_create(
            workflow=wf,
            from_status='scheduled',
            to_status=wf.initial_status.slug,
            defaults={
                'name': 'Run Now',
                'description': 'Manually start the next recurring cycle now instead of waiting for the timer.',
                'button_style': 'primary',
                'position': 0,
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0076_alter_status_category'),
        ('tasks', '0075_merge_0074_leaves'),
        ('tasks', '0075_pls214_recurring_cycle_stale_activity'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
