"""Seed a simple task workflow with an explicit review gate."""
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

CANCEL_ARTICLE = {
    'type': 'inline',
    'title': 'Cancel Is Terminal',
    'content': (
        'Use Cancel only for invalid artifacts, test artifacts, duplicates, or '
        'work that should not exist. Use Defer for valid work that a human or '
        'operator intentionally parked. Always include a short comment.'
    ),
}

HUMAN_OPS_ARTICLE = {
    'type': 'inline',
    'title': 'Escalate to Human Ops - last-resort physical/human-only',
    'content': (
        'Use only when the next step is genuinely impossible for any agent and '
        'needs a human acting physically or in person. For decisions, billing, '
        'policy, sign-offs, or coordination use Raise to Admin Ops instead.'
    ),
}

DEVOPS_ARTICLE = {
    'type': 'inline',
    'title': 'Escalate to DevOps - internal infra/provisioning only',
    'content': (
        'Use when progress is blocked by internal infrastructure, deployment, '
        'runtime, credential-broker, registry, kubeconfig, repo-write identity, '
        'or production-access provisioning that DevOps owns and can self-serve.'
    ),
}

ADMIN_OPS_ARTICLE = {
    'type': 'inline',
    'title': 'Raise to Admin Ops',
    'content': (
        'Use for coordination, policy, approval, routing, or operator-facing '
        'questions that are not direct implementation, review, DevOps, or '
        'physical Human Ops work.'
    ),
}


def _status(Status, slug, **defaults):
    status, _ = Status.objects.update_or_create(slug=slug, defaults=defaults)
    return status


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
    changed = []
    for field, value in defaults.items():
        setattr(transition, field, value)
        changed.append(field)
    if changed:
        transition.save(update_fields=changed)


def seed_reviewed_task(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    statuses = {
        'todo': _status(
            Status,
            'todo',
            name='To Do',
            category='todo',
            color='#6b7280',
            position=1,
            is_system=True,
            description='Item is ready to be worked on',
        ),
        'in_progress': _status(
            Status,
            'in_progress',
            name='In Progress',
            category='in_progress',
            color='#3b82f6',
            position=10,
            is_system=True,
            description='Work is actively being done',
        ),
        'in_review': _status(
            Status,
            'in_review',
            name='In Review',
            category='in_progress',
            color='#8b5cf6',
            position=11,
            is_system=False,
            description='Work is complete and awaiting review',
        ),
        'blocked': _status(
            Status,
            'blocked',
            name='Blocked',
            category='in_progress',
            color='#f59e0b',
            position=30,
            is_system=True,
            description='Work cannot progress until one or more linked blockers are resolved.',
        ),
        'done': _status(
            Status,
            'done',
            name='Done',
            category='done',
            color='#22c55e',
            position=20,
            is_system=True,
            description='Work is complete',
        ),
        'cancelled': _status(
            Status,
            'cancelled',
            name='Cancelled',
            category='done',
            color='#9ca3af',
            position=22,
            is_system=False,
            description='Item was cancelled without completion',
        ),
    }
    Resolution.objects.update_or_create(
        slug='done',
        defaults={
            'name': 'Done',
            'position': 5,
            'is_system': True,
            'description': 'Work on this issue is complete',
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

    workflow, _ = Workflow.objects.update_or_create(
        slug='reviewed-task',
        defaults={
            'name': 'Reviewed Task',
            'description': (
                'Simple task workflow with a mandatory Review-team approval '
                'step before completion.'
            ),
            'is_system': True,
            'is_active': True,
            'initial_status': statuses['todo'],
            'knowledge_base': [
                {
                    'type': 'inline',
                    'title': 'Reviewed Task Workflow',
                    'content': (
                        'Use this for ordinary tasks that need independent '
                        'review. Implementers submit work to Review; reviewers '
                        'approve to Done or request changes back to the original '
                        'implementer.'
                    ),
                },
            ],
        },
    )

    for position, slug in enumerate(['todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']):
        WorkflowStatus.objects.update_or_create(
            workflow=workflow,
            status=statuses[slug],
            defaults={'position': position},
        )

    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Start Progress',
        'todo',
        'in_progress',
        description='Begin implementation.',
        position=0,
        button_style='primary',
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Submit for Review',
        'in_progress',
        'in_review',
        description='Implementation complete; send to the Review team.',
        position=10,
        button_style='primary',
        post_functions=[
            {'type': 'stash_implementer'},
            {'type': 'assign_team', 'value': 'review'},
            {'type': 'stamp_review_started'},
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Approve',
        'in_review',
        'done',
        description='Review passed; complete the task.',
        position=20,
        button_style='success',
        post_functions=[
            {'type': 'set_resolution', 'resolution': 'done'},
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'reporter', 'watchers'],
            },
        ],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Request Changes',
        'in_review',
        'in_progress',
        description='Review found issues; return to the original implementer.',
        position=30,
        button_style='warning',
        screen_fields=[
            {'field': 'comment', 'label': 'What needs to change?', 'required': True},
        ],
        post_functions=[
            {'type': 'assign_implementer'},
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Reopen',
        'done',
        'todo',
        description='Reopen completed work.',
        position=35,
        button_style='default',
        post_functions=[{'type': 'clear_resolution'}],
    )

    active_sources = ['todo', 'in_progress', 'in_review']
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
        _upsert_transition(
            WorkflowTransition,
            workflow,
            f'Blocker Resolved - Resume {target.replace("_", " ").title()}',
            'blocked',
            target,
            description=(
                'Recovery path after all linked blockers are terminal. Pulse '
                'usually executes this automatically and restores the pre-block owner.'
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

    for source in active_sources + ['blocked']:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Cancel',
            source,
            'cancelled',
            description='Terminally cancel invalid, duplicate, or no-longer-needed work.',
            position=900,
            button_style='danger',
            screen_fields=[
                {'field': 'comment', 'label': 'Why is this being cancelled?', 'required': True},
            ],
            knowledge_articles=[CANCEL_ARTICLE],
            post_functions=[
                {'type': 'set_resolution', 'resolution': 'cancelled'},
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'reporter', 'watchers'],
                },
            ],
        )

    for source in active_sources:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Escalate to Human Ops',
            source,
            'blocked',
            description='Last-resort route for physical or human-only work.',
            position=60,
            button_style='warning',
            knowledge_articles=[HUMAN_OPS_ARTICLE],
            post_functions=[
                {'type': 'assign_team', 'value': 'human-ops'},
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'watchers'],
                },
            ],
        )
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Raise to Admin Ops',
            source,
            'blocked',
            description='Route coordination, policy, or operator-facing questions to Admin Ops.',
            position=65,
            button_style='warning',
            knowledge_articles=[ADMIN_OPS_ARTICLE],
            post_functions=[
                {'type': 'assign_team', 'value': 'admin-ops'},
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'watchers'],
                },
            ],
        )

    for source in ['in_progress', 'in_review']:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Escalate to DevOps',
            source,
            'todo',
            description='Route internal infra/provisioning/runtime/credential gaps to DevOps.',
            position=55,
            button_style='warning',
            knowledge_articles=[DEVOPS_ARTICLE],
            post_functions=[
                {'type': 'assign_team', 'value': 'devops'},
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
        ('tasks', '0070_pls171_task_event'),
    ]

    operations = [
        migrations.RunPython(seed_reviewed_task, noop),
    ]
