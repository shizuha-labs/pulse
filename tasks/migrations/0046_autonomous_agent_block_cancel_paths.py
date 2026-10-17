"""Make autonomous agent workflows exhaustive for blockers and discards."""
from django.db import migrations


AUTONOMOUS_WORKFLOWS = ('autonomous-dev', 'autonomous-bug')

BLOCK_ARTICLE = {
    'type': 'inline',
    'title': 'Linked Blocker Discipline',
    'content': (
        'Use this when the current task cannot progress because another Pulse '
        'task is an active blocker. Create or link the blocker first: '
        'BLOCKER --blocks--> THIS task. Do not leave a task open/in_progress '
        'while it has active blocker links. Pulse auto-resumes the blocked task '
        'when all linked blockers reach terminal states.'
    ),
}

CANCEL_ARTICLE = {
    'type': 'inline',
    'title': 'Discard Is Terminal',
    'content': (
        'Use Cancel / Discard only for invalid artifacts, test artifacts, '
        'duplicates, or work that should not exist. Use Defer for valid work '
        'that an operator intentionally parked. Always include a short comment.'
    ),
}

RESUME_ARTICLE = {
    'type': 'inline',
    'title': 'Resume After Blocker Resolution',
    'content': (
        'Use only when linked blockers have actually reached terminal states. '
        'The automatic unblock path normally restores the pre-block status and '
        'owner; manual use is a recovery path.'
    ),
}


def _upsert_transition(WorkflowTransition, workflow, name, from_status, to_status, **defaults):
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
        name=name,
        from_status=from_status,
        to_status=to_status,
    ).first()
    if transition is None:
        WorkflowTransition.objects.create(
            workflow=workflow,
            name=name,
            from_status=from_status,
            to_status=to_status,
            **defaults,
        )
        return
    for field, value in defaults.items():
        setattr(transition, field, value)
    transition.save(update_fields=list(defaults.keys()))


def apply_paths(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    cancelled_status, _ = Status.objects.update_or_create(
        slug='cancelled',
        defaults={
            'name': 'Cancelled',
            'category': 'done',
            'color': '#9ca3af',
            'position': 22,
            'is_system': False,
            'description': 'Item was cancelled or discarded without completion',
        },
    )
    Resolution.objects.update_or_create(
        slug='cancelled',
        defaults={
            'name': 'Cancelled',
            'position': 9,
            'is_system': False,
            'description': 'Work was cancelled or discarded without completion',
        },
    )

    for workflow in Workflow.objects.filter(slug__in=AUTONOMOUS_WORKFLOWS):
        WorkflowStatus.objects.get_or_create(
            workflow=workflow,
            status=cancelled_status,
            defaults={'position': 99},
        )

        status_slugs = list(
            workflow.workflow_statuses
            .exclude(status__category='done')
            .values_list('status__slug', flat=True)
            .order_by('position')
        )
        active_sources = [
            slug for slug in status_slugs
            if slug not in ('blocked', 'scheduled')
        ]

        if 'blocked' in status_slugs:
            for source in active_sources:
                _upsert_transition(
                    WorkflowTransition,
                    workflow,
                    'Blocked by Linked Issue',
                    source,
                    'blocked',
                    description=(
                        'Use when this task has an active linked blocker and no '
                        'meaningful work can continue until that blocker resolves.'
                    ),
                    position=40,
                    button_style='warning',
                    knowledge_articles=[BLOCK_ARTICLE],
                    post_functions=[
                        {
                            'type': 'send_notification',
                            'template': 'status_changed',
                            'recipients': ['assignee', 'watchers'],
                        },
                    ],
                )

            for target in active_sources:
                if WorkflowTransition.objects.filter(
                    workflow=workflow,
                    from_status='blocked',
                    to_status=target,
                ).exists():
                    continue
                _upsert_transition(
                    WorkflowTransition,
                    workflow,
                    f'Blocker Resolved - Resume {target.replace("_", " ").title()}',
                    'blocked',
                    target,
                    description=(
                        'Recovery path after all linked blockers are terminal. '
                        'Pulse usually executes this automatically and restores '
                        'the pre-block owner.'
                    ),
                    position=51,
                    button_style='primary',
                    knowledge_articles=[RESUME_ARTICLE],
                    post_functions=[
                        {
                            'type': 'send_notification',
                            'template': 'status_changed',
                            'recipients': ['assignee', 'watchers'],
                        },
                    ],
                )

        for source in active_sources + (['blocked'] if 'blocked' in status_slugs else []):
            _upsert_transition(
                WorkflowTransition,
                workflow,
                'Cancel / Discard',
                source,
                'cancelled',
                description=(
                    'Terminally discard invalid, duplicate, test-only, or '
                    'no-longer-needed work. This removes it from active queues.'
                ),
                position=900,
                button_style='danger',
                screen_fields=[
                    {
                        'field': 'comment',
                        'label': 'Why is this being cancelled/discarded?',
                        'required': True,
                    },
                ],
                knowledge_articles=[CANCEL_ARTICLE],
                post_functions=[
                    {'type': 'set_resolution', 'resolution': 'cancelled'},
                    {
                        'type': 'send_notification',
                        'template': 'status_changed',
                        'recipients': ['assignee', 'reporter', 'watchers'],
                    },
                    {
                        'type': 'add_comment',
                        'body': (
                            'Cancelled/discarded through workflow. This is a '
                            'terminal state and should not remain in active agent queues.'
                        ),
                    },
                ],
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0045_blocked_back_to_merge_transition'),
    ]

    operations = [
        migrations.RunPython(apply_paths, noop),
    ]
