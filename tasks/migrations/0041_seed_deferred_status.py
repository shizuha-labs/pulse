"""
Seed a canonical Deferred terminal status and a global Defer transition.

Deferred means "valid work, intentionally parked by a human/operator decision".
It is a done-category status so default active queues hide it, but explicit
status=deferred queries can find the historical decision and comments. The
global transition assigns the terminal item back to the reporter so the creator
has the durable record and agents do not keep treating it as blocked work.

Idempotent: re-running is safe.
"""
from django.db import migrations


DEFERRED_STATUS = {
    'slug': 'deferred',
    'name': 'Deferred',
    'category': 'done',
    'color': '#64748b',
    'position': 24,
    'is_system': False,
    'description': (
        'Item is intentionally parked by a human/operator decision; do not '
        'recreate without explicit direction'
    ),
}


DEFERRED_RESOLUTION = {
    'slug': 'deferred',
    'name': 'Deferred',
    'position': 8,
    'is_system': False,
    'description': 'Valid work intentionally deferred by a human/operator decision',
}


DEFER_TRANSITION = {
    'name': 'Defer',
    'description': (
        'Terminally park valid work because the human/operator chose not to act '
        'now. Use this instead of leaving operator-only work blocked forever.'
    ),
    'from_status': '*',
    'to_status': 'deferred',
    'is_global': True,
    'button_style': 'warning',
    'position': 950,
    'screen_fields': [
        {
            'field': 'comment',
            'label': 'Why is this deferred?',
            'required': True,
        },
    ],
    'knowledge_articles': [
        {
            'type': 'inline',
            'title': 'Deferred Means Do Not Recreate',
            'content': (
                'Use Defer when the work is valid but intentionally parked by a '
                'human/operator decision. This is terminal: agents should not '
                'reopen it or create another task for the same ask. Before '
                'filing related work, search Pulse for existing active/deferred '
                'tasks and read the deferred task plus comments.'
            ),
        },
    ],
    'post_functions': [
        {'type': 'assign_to', 'target': 'reporter'},
        {'type': 'set_resolution', 'resolution': 'deferred'},
        {
            'type': 'send_notification',
            'template': 'status_changed',
            'recipients': ['reporter', 'watchers'],
        },
        {
            'type': 'add_comment',
            'body': (
                'Deferred by operator decision. This is a terminal parked state: '
                'do not create duplicate tasks for the same ask. Read this task '
                'and its comments before filing related work.'
            ),
        },
    ],
}


def seed(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    status, _ = Status.objects.update_or_create(
        slug=DEFERRED_STATUS['slug'],
        defaults={
            'name': DEFERRED_STATUS['name'],
            'category': DEFERRED_STATUS['category'],
            'color': DEFERRED_STATUS['color'],
            'position': DEFERRED_STATUS['position'],
            'is_system': DEFERRED_STATUS['is_system'],
            'description': DEFERRED_STATUS['description'],
        },
    )
    Resolution.objects.update_or_create(
        slug=DEFERRED_RESOLUTION['slug'],
        defaults={
            'name': DEFERRED_RESOLUTION['name'],
            'position': DEFERRED_RESOLUTION['position'],
            'is_system': DEFERRED_RESOLUTION['is_system'],
            'description': DEFERRED_RESOLUTION['description'],
        },
    )

    for wf in Workflow.objects.all():
        WorkflowStatus.objects.get_or_create(
            workflow=wf,
            status=status,
            defaults={'position': 98},
        )

        transition = WorkflowTransition.objects.filter(
            workflow=wf,
            from_status=DEFER_TRANSITION['from_status'],
            to_status=DEFER_TRANSITION['to_status'],
            name=DEFER_TRANSITION['name'],
        ).first()
        if transition is None:
            WorkflowTransition.objects.create(
                workflow=wf,
                **DEFER_TRANSITION,
            )
        else:
            for field, value in DEFER_TRANSITION.items():
                setattr(transition, field, value)
            transition.save()


def unseed(apps, schema_editor):
    # Non-destructive: leave data in place. Removing would orphan any Item
    # currently in this status and remove the decision record agents rely on.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0040_add_pr_activity_actions'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
