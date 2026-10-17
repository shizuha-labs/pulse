"""
Seed a canonical `scheduled → <initial_status>` transition on every workflow.

Without this, recurring tasks parked in Scheduled have no outgoing
transition in the UI, which reads as "No transitions available from
Scheduled" — confusing, and leaves the dedicated Run Now button (banner
only) as the sole way out.

Idempotent: if the transition already exists, skip.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    Status = apps.get_model('tasks', 'Status')

    scheduled = Status.objects.filter(slug='scheduled').first()
    if scheduled is None:
        # 0036 should have created this; bail gracefully if someone squashed migrations
        return

    created_count = 0
    for wf in Workflow.objects.all():
        if not wf.initial_status_id:
            continue
        initial = Status.objects.filter(id=wf.initial_status_id).first()
        if initial is None:
            continue

        # Make sure Scheduled is attached to this workflow (0036 did this
        # for all existing workflows; this covers any that were created
        # between 0036 and now).
        WorkflowStatus.objects.get_or_create(
            workflow=wf,
            status=scheduled,
            defaults={'position': 99},
        )

        # scheduled -> initial transition (named "Run Now" for all workflows)
        _tr, created = WorkflowTransition.objects.get_or_create(
            workflow=wf,
            from_status='scheduled',
            to_status=initial.slug,
            defaults={
                'name': 'Run Now',
                'description': 'Manually start the next recurring cycle now instead of waiting for the timer.',
                'button_style': 'primary',
                'position': 0,
            },
        )
        if created:
            created_count += 1


def unseed(apps, schema_editor):
    # Leave transitions alone on reverse — removing them could break live
    # tasks currently relying on this path.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0036_seed_scheduled_status'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
