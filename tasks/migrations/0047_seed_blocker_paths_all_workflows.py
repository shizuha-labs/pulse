"""Seed linked-blocker workflow paths across active workflows."""
from django.db import migrations


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

    transition.name = name
    update_fields = ['name']
    for field, value in defaults.items():
        setattr(transition, field, value)
        update_fields.append(field)
    transition.save(update_fields=update_fields)


def apply_paths(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    blocked_status, _ = Status.objects.update_or_create(
        slug='blocked',
        defaults={
            'name': 'Blocked',
            'category': 'in_progress',
            'color': '#f59e0b',
            'position': 30,
            'is_system': True,
            'description': 'Work cannot progress until one or more linked blockers are resolved.',
        },
    )

    for workflow in Workflow.objects.all():
        active_statuses = list(
            workflow.workflow_statuses
            .exclude(status__category__in=['done', 'scheduled'])
            .order_by('position')
            .values_list('status__slug', 'position')
        )
        active_sources = [slug for slug, _ in active_statuses if slug != 'blocked']
        if not active_sources:
            continue

        if not WorkflowStatus.objects.filter(workflow=workflow, status=blocked_status).exists():
            max_position = max([position for _, position in active_statuses] or [0])
            WorkflowStatus.objects.create(
                workflow=workflow,
                status=blocked_status,
                position=max_position + 1,
            )

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


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0046_autonomous_agent_block_cancel_paths'),
    ]

    operations = [
        migrations.RunPython(apply_paths, noop),
    ]
