"""Seed a Feature Request / RFC workflow for cross-system product work."""
from django.db import migrations


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


def seed_feature_request_rfc(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowScheme = apps.get_model('tasks', 'WorkflowScheme')
    WorkflowSchemeMapping = apps.get_model('tasks', 'WorkflowSchemeMapping')

    statuses = {
        'rfc_intake': _status(
            Status,
            'rfc_intake',
            name='Intake',
            category='todo',
            color='#6b7280',
            position=101,
            is_system=True,
            description='Feature request captured; problem statement and affected surface are being confirmed.',
        ),
        'rfc_triage': _status(
            Status,
            'rfc_triage',
            name='Triage',
            category='in_progress',
            color='#f97316',
            position=102,
            is_system=True,
            description='Feature request is being prioritized and routed to the owning team.',
        ),
        'rfc_design': _status(
            Status,
            'rfc_design',
            name='Design / RFC',
            category='in_progress',
            color='#14b8a6',
            position=103,
            is_system=True,
            description='Architecture/product design is being written or reviewed.',
        ),
        'rfc_approved': _status(
            Status,
            'rfc_approved',
            name='Approved for Implementation',
            category='in_progress',
            color='#3b82f6',
            position=104,
            is_system=True,
            description='Design is approved; linked reviewed implementation tasks should be created.',
        ),
        'rfc_implementation_linked': _status(
            Status,
            'rfc_implementation_linked',
            name='Implementation Linked',
            category='in_progress',
            color='#8b5cf6',
            position=105,
            is_system=True,
            description='Implementation work is tracked in linked child tasks or related tasks.',
        ),
        'rfc_validation': _status(
            Status,
            'rfc_validation',
            name='Validation',
            category='in_progress',
            color='#ec4899',
            position=106,
            is_system=True,
            description='Integrated behavior is being validated against acceptance criteria.',
        ),
        'rfc_needs_info': _status(
            Status,
            'rfc_needs_info',
            name='Needs Info',
            category='in_progress',
            color='#f59e0b',
            position=107,
            is_system=True,
            description='The request needs missing context before triage/design can continue.',
        ),
        'blocked': _status(
            Status,
            'blocked',
            name='Blocked',
            category='in_progress',
            color='#ef4444',
            position=13,
            is_system=False,
            description='Work is blocked by an external dependency',
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
        'deferred': _status(
            Status,
            'deferred',
            name='Deferred',
            category='done',
            color='#64748b',
            position=24,
            is_system=False,
            description='Valid work intentionally deferred by a human/operator decision',
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

    for slug, name, position, description in [
        ('done', 'Done', 5, 'Work on this issue is complete'),
        ('deferred', 'Deferred', 8, 'Valid work intentionally deferred by a human/operator decision'),
        ('cancelled', 'Cancelled', 9, 'Work was cancelled or discarded without completion'),
        ('wont_do', "Won't Do", 6, 'This issue will not be actioned'),
    ]:
        Resolution.objects.update_or_create(
            slug=slug,
            defaults={
                'name': name,
                'position': position,
                'is_system': slug in {'done', 'wont_do'},
                'description': description,
            },
        )

    workflow, _ = Workflow.objects.update_or_create(
        slug='feature-request-rfc',
        defaults={
            'name': 'Feature Request / RFC',
            'description': (
                'Product and cross-system feature intake workflow. Use it for '
                'work that needs problem framing, design/RFC approval, linked '
                'reviewed implementation tasks, and validation before Done.'
            ),
            'is_system': True,
            'is_active': True,
            'initial_status': statuses['rfc_intake'],
            'knowledge_base': [
                {
                    'type': 'inline',
                    'title': 'When to use Feature Request / RFC',
                    'content': (
                        'Use this for user-visible or cross-system changes, '
                        'especially work spanning Hive, Connect, Pulse, runtime, '
                        'identity, credentials, or tenant isolation. Do not use '
                        'it for one-file fixes or routine operational chores.'
                    ),
                },
                {
                    'type': 'inline',
                    'title': 'Required request shape',
                    'content': (
                        'Include Problem, Affected apps, Desired behavior, '
                        'Non-goals, Acceptance criteria, and Linked '
                        'implementation tasks. Parent RFC tasks route by team; '
                        'implementation children should use Reviewed Task.'
                    ),
                },
            ],
        },
    )

    ordered_statuses = [
        'rfc_intake',
        'rfc_triage',
        'rfc_design',
        'rfc_approved',
        'rfc_implementation_linked',
        'rfc_validation',
        'rfc_needs_info',
        'blocked',
        'done',
        'deferred',
        'cancelled',
    ]
    for position, slug in enumerate(ordered_statuses):
        WorkflowStatus.objects.update_or_create(
            workflow=workflow,
            status=statuses[slug],
            defaults={'position': position},
        )

    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Start Triage',
        'rfc_intake',
        'rfc_triage',
        description='Begin product/architecture triage.',
        position=0,
        button_style='primary',
        post_functions=[{'type': 'assign_team', 'value': 'architecture'}],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Begin Design / RFC',
        'rfc_triage',
        'rfc_design',
        description='Move to architecture/product design.',
        position=10,
        button_style='primary',
        post_functions=[{'type': 'assign_team', 'value': 'architecture'}],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Approve for Implementation',
        'rfc_design',
        'rfc_approved',
        description='Design is approved; create linked reviewed implementation tasks.',
        position=20,
        button_style='success',
        screen_fields=[
            {'field': 'comment', 'label': 'Approval note / implementation split', 'required': True},
        ],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Implementation Tasks Linked',
        'rfc_approved',
        'rfc_implementation_linked',
        description='Linked implementation tasks exist and are routed to owning teams.',
        position=30,
        button_style='primary',
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Start Validation',
        'rfc_implementation_linked',
        'rfc_validation',
        description='Implementation has landed; validate integrated behavior.',
        position=40,
        button_style='primary',
        post_functions=[{'type': 'assign_team', 'value': 'qa'}],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Validated',
        'rfc_validation',
        'done',
        description='Acceptance criteria passed; complete the RFC.',
        position=50,
        button_style='success',
        post_functions=[{'type': 'set_resolution', 'resolution': 'done'}],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Request More Info',
        'rfc_intake',
        'rfc_needs_info',
        description='Request missing information before triage can continue.',
        position=60,
        button_style='warning',
        screen_fields=[
            {'field': 'comment', 'label': 'What information is missing?', 'required': True},
        ],
        post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
    )
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Info Provided',
        'rfc_needs_info',
        'rfc_triage',
        description='Requested information was provided; resume triage.',
        position=70,
        button_style='primary',
        post_functions=[{'type': 'assign_team', 'value': 'architecture'}],
    )

    for source in ['rfc_triage', 'rfc_design', 'rfc_approved', 'rfc_implementation_linked', 'rfc_validation']:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Needs Info',
            source,
            'rfc_needs_info',
            description='Pause the RFC until missing context is provided.',
            position=80,
            button_style='warning',
            screen_fields=[
                {'field': 'comment', 'label': 'What information is missing?', 'required': True},
            ],
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Blocked by Linked Issue',
            source,
            'blocked',
            description='Use only after linking the active blocker task.',
            position=90,
            button_style='warning',
        )

    for target in ['rfc_triage', 'rfc_design', 'rfc_approved', 'rfc_implementation_linked', 'rfc_validation']:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            f'Blocker Resolved - Resume {target.replace("rfc_", "").replace("_", " ").title()}',
            'blocked',
            target,
            description='Resume the RFC after linked blockers are terminal.',
            position=100,
            button_style='primary',
        )

    for source in [
        'rfc_intake',
        'rfc_triage',
        'rfc_design',
        'rfc_approved',
        'rfc_implementation_linked',
        'rfc_validation',
        'rfc_needs_info',
        'blocked',
    ]:
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Defer',
            source,
            'deferred',
            description='Park valid work by explicit operator/product decision.',
            position=800,
            button_style='default',
            screen_fields=[
                {'field': 'comment', 'label': 'Why is this deferred?', 'required': True},
            ],
            post_functions=[{'type': 'set_resolution', 'resolution': 'deferred'}],
        )
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Cancel',
            source,
            'cancelled',
            description='Cancel invalid, duplicate, or no-longer-needed feature work.',
            position=900,
            button_style='danger',
            screen_fields=[
                {'field': 'comment', 'label': 'Why is this cancelled?', 'required': True},
            ],
            post_functions=[{'type': 'set_resolution', 'resolution': 'cancelled'}],
        )

    for scheme in WorkflowScheme.objects.filter(is_active=True):
        for item_type in ['task.feature', 'feature.request', 'task.rfc']:
            WorkflowSchemeMapping.objects.update_or_create(
                scheme=scheme,
                item_type=item_type,
                defaults={'workflow': workflow},
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0071_seed_reviewed_task_workflow'),
    ]

    operations = [
        migrations.RunPython(seed_feature_request_rfc, noop),
    ]
