"""PLS-481 / PLS-634: Seed accepted_capacity_held and implementation_active statuses.

Adds the new statuses to the Status table, wires them into the scoped
no-PR Architecture workflows (feature-request-rfc, reviewed-task,
autonomous-dev, autonomous-bug), and adds the Accept — Capacity-Held
transition with Architecture actor authority + required plan screen fields.
"""
from django.db import migrations


ACCEPT_VALIDATOR = {
    'type': 'accept_capacity_held',
    'message': (
        'Accept — Capacity-Held gate (PLS-481): the item does not '
        'satisfy the acceptance criteria. A current PublicationBinding '
        'with Architecture + Security role proofs, digest-verified '
        'plan_document, and capacity_reason is required.'
    ),
}

ACCEPT_SCREEN_FIELDS = [
    {
        'field': 'plan_hash',
        'label': 'Content-addressed plan hash',
        'required': True,
        'help_text': (
            'SHA-256 digest of the immutable held-plan identity '
            '(must equal digest(plan_document)).'
        ),
    },
    {
        'field': 'plan_document',
        'label': 'Immutable plan document (JSON)',
        'required': True,
        'help_text': (
            'Ordered slice inventory: '
            '{"slices":[{"id":"s1","team":"engineering","depends_on":[]}]}'
        ),
    },
    {
        'field': 'capacity_reason',
        'label': 'Capacity hold reason',
        'required': True,
        'help_text': 'Architect-supplied reason implementation capacity is held.',
    },
]

ACCEPT_POST_FUNCTION = {
    'type': 'accept_capacity_held',
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


def _seed_accept_on_workflow(WorkflowStatus, WorkflowTransition, workflow,
                             accepted_capacity_held, implementation_active,
                             accept_from_statuses):
    """Wire statuses + Accept/Activate/Return/Defer/Cancel on one workflow."""
    existing = {
        ws.status.slug
        for ws in WorkflowStatus.objects.filter(workflow=workflow).select_related('status')
    }
    if 'accepted_capacity_held' not in existing:
        WorkflowStatus.objects.update_or_create(
            workflow=workflow,
            status=accepted_capacity_held,
            defaults={'position': 15},
        )
    if 'implementation_active' not in existing:
        WorkflowStatus.objects.update_or_create(
            workflow=workflow,
            status=implementation_active,
            defaults={'position': 16},
        )

    for idx, from_status in enumerate(accept_from_statuses):
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Accept — Capacity-Held',
            from_status,
            'accepted_capacity_held',
            description=(
                'PLS-481: Design is accepted but implementation capacity '
                'is held. Requires a current PublicationBinding with '
                'Architecture + Security role proofs, plan_hash, '
                'capacity_reason, and plan_targets. Architecture actor only.'
            ),
            position=25 + idx,
            button_style='primary',
            allowed_roles=['architecture'],
            validators=[dict(ACCEPT_VALIDATOR)],
            screen_fields=list(ACCEPT_SCREEN_FIELDS),
            post_functions=[dict(ACCEPT_POST_FUNCTION)],
        )

    # S1 deliberately does NOT seed Activate Implementation / Return to Held.
    # Those edges require the S3 event-driven activation transaction (capacity
    # event + WIP reservation + slice materialization). An unfenced manual
    # path would bypass HLD §5 (rei P1).
    #
    # Defer/Cancel remain seeded for operator cleanup, but the runtime
    # terminal-debt gate blocks them while accepted_plan slices are open.
    _upsert_transition(
        WorkflowTransition,
        workflow,
        'Defer',
        'accepted_capacity_held',
        'deferred',
        description=(
            'Park valid accepted work by explicit operator/product decision. '
            'Blocked while accepted plan slices remain open (PLS-481 debt gate).'
        ),
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
        'accepted_capacity_held',
        'cancelled',
        description=(
            'Cancel accepted work that is no longer needed. '
            'Blocked while accepted plan slices remain open (PLS-481 debt gate).'
        ),
        position=900,
        button_style='danger',
        screen_fields=[
            {'field': 'comment', 'label': 'Why is this cancelled?', 'required': True},
        ],
        post_functions=[{'type': 'set_resolution', 'resolution': 'cancelled'}],
    )


def seed_accepted_capacity_held(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # Not WIP: backlog category so capacity accounting excludes it by default.
    # Runtime also hard-excludes the slug from _assignment_excluded_statuses /
    # _slot_kind_for_status as defense in depth.
    accepted_capacity_held = _status(
        Status,
        'accepted_capacity_held',
        name='Accepted — Capacity Held',
        category='backlog',
        color='#8b5cf6',
        position=108,
        is_system=True,
        description=(
            'PLS-481: Design is accepted and frozen at the referenced '
            'PublicationBinding, but implementation capacity is held. '
            'Not WIP, not done, not deferred.'
        ),
    )
    implementation_active = _status(
        Status,
        'implementation_active',
        name='Implementation Active',
        category='in_progress',
        color='#a78bfa',
        position=109,
        is_system=True,
        description=(
            'PLS-481: At least one plan slice has been activated; '
            'implementation is in progress but the accepted plan is '
            'not fully closed.'
        ),
    )

    workflow_from = {
        'feature-request-rfc': ('rfc_design', 'rfc_approved'),
        'reviewed-task': ('in_review',),
        # no-pr design path of autonomous-dev / autonomous-bug (label-gated
        # at the validator; transition is available from design/in_review).
        'autonomous-dev': ('design', 'in_review'),
        'autonomous-bug': ('design', 'in_review'),
    }
    for slug, from_statuses in workflow_from.items():
        workflow = Workflow.objects.filter(slug=slug).first()
        if not workflow:
            continue
        # Only seed from_statuses that exist on the workflow (or are global).
        present = set(
            WorkflowStatus.objects.filter(workflow=workflow)
            .values_list('status__slug', flat=True)
        )
        # Also allow common design/review slugs even if WorkflowStatus join is
        # incomplete in older fixtures — transition seed is still useful.
        usable = [s for s in from_statuses if s in present or s in {
            'rfc_design', 'rfc_approved', 'in_review', 'design', 'open',
        }]
        if not usable:
            usable = list(from_statuses)
        _seed_accept_on_workflow(
            WorkflowStatus,
            WorkflowTransition,
            workflow,
            accepted_capacity_held,
            implementation_active,
            usable,
        )


def unseed_accepted_capacity_held(apps, schema_editor):
    """Idempotent reverse: remove only rows this migration owns."""
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    owned_slugs = ('feature-request-rfc', 'reviewed-task', 'autonomous-dev', 'autonomous-bug')
    workflows = Workflow.objects.filter(slug__in=owned_slugs)
    # Drop S1 Accept / Defer / Cancel edges and any residual unsafe activation
    # edges this train may have seeded on earlier revisions.
    WorkflowTransition.objects.filter(
        workflow__in=workflows,
        name__in=(
            'Accept — Capacity-Held',
            'Activate Implementation',
            'Return to Held',
            'Defer',
            'Cancel',
        ),
        from_status__in=(
            'in_review', 'approved', 'design', 'accepted_capacity_held',
            'implementation_active',
        ),
    ).filter(
        to_status__in=(
            'accepted_capacity_held', 'implementation_active',
            'deferred', 'cancelled',
        ),
    ).delete()
    # Also catch Accept edges from any from_status on owned workflows.
    WorkflowTransition.objects.filter(
        workflow__in=workflows,
        name='Accept — Capacity-Held',
        to_status='accepted_capacity_held',
    ).delete()
    WorkflowStatus.objects.filter(
        workflow__in=workflows,
        status__slug__in=('accepted_capacity_held', 'implementation_active'),
    ).delete()
    # Only delete statuses if no remaining workflow membership.
    for slug in ('accepted_capacity_held', 'implementation_active'):
        if not WorkflowStatus.objects.filter(status__slug=slug).exists():
            Status.objects.filter(slug=slug).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0143_pls634_fix_heldplantarget_id'),
    ]

    operations = [
        migrations.RunPython(seed_accepted_capacity_held, unseed_accepted_capacity_held),
    ]
