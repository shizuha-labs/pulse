"""Seed an 'Escalate to Human Ops' transition on every active status of every
workflow (operator directive 2026-06-13). Gives agents a LAST-RESORT path to
escalate genuinely-impossible-for-an-agent, physical/human-only work (plug/reseat
a cable, power-cycle a bricked node, flash firmware at the console, etc.) to the
human-ops team — which resolves to the operator. Decisions/billing/policy/sign-offs
keep using Raise to Admin Ops / Escalate to Leads; this is for hands-at-hardware
only, after the normal attempts are exhausted.

Idempotent and name-aware: multiple transitions already target `blocked`
(Raise to Admin Ops, Escalate to Leads, Blocked by Linked Issue), so we dedup by
(workflow, from_status, to_status, NAME) to avoid clobbering them.
"""
from django.db import migrations


HUMAN_OPS_ARTICLE = {
    'type': 'inline',
    'title': 'Escalate to Human Ops — LAST RESORT, physical/human-only',
    'content': (
        'Use ONLY when the next step is genuinely impossible for any agent and needs a '
        'human (the operator) acting physically/in person — e.g. plug or reseat a cable/SFP, '
        'power-cycle a bricked node, flash firmware at the console, a physical device action, '
        'or something only a human can do in the real world. '
        'This is a LAST RESORT — first exhaust the normal attempts: retry; fix in code/config; '
        'restart the service; search the wiki runbooks; ask a peer or route to the owning team; '
        'and (DevOps) use your host ssh + passwordless sudo, which is self-serve and NOT an '
        'escalation. Only when NO agent can do it does this apply. '
        'Write the task as a hands-on runbook step: exact node/port/device, what to do, and how '
        'to verify success. Routes to the human-ops team (the operator). '
        'For decisions / billing / policy / sign-offs use Raise to Admin Ops or Escalate to '
        'Leads instead — NOT this.'
    ),
}

ESCALATION_NAME = 'Escalate to Human Ops'


def _upsert_named_transition(WorkflowTransition, workflow, name, from_status, to_status, **defaults):
    """Upsert deduped by (workflow, from_status, to_status, NAME) so sibling
    transitions to the same target (blocked) are preserved."""
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
        from_status=from_status,
        to_status=to_status,
        name=name,
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
    update_fields = []
    for field, value in defaults.items():
        setattr(transition, field, value)
        update_fields.append(field)
    if update_fields:
        transition.save(update_fields=update_fields)


def apply_human_ops(apps, schema_editor):
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
            'description': 'Work cannot progress until a blocker (incl. a human/physical action) is resolved.',
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

        # Ensure 'blocked' is a status in this workflow (target of the escalation).
        if not WorkflowStatus.objects.filter(workflow=workflow, status=blocked_status).exists():
            max_position = max([position for _, position in active_statuses] or [0])
            WorkflowStatus.objects.create(workflow=workflow, status=blocked_status, position=max_position + 1)

        for source in active_sources:
            _upsert_named_transition(
                WorkflowTransition,
                workflow,
                ESCALATION_NAME,
                source,
                'blocked',
                description=(
                    'LAST RESORT: escalate work that is genuinely impossible for any agent and '
                    'needs a human (the operator) to act physically/in person — cables, reseats, '
                    'power-cycle, firmware-at-console, physical device actions. Exhaust retry / '
                    'fix / restart / wiki / peer / host-sudo first. Routes to human-ops (operator).'
                ),
                position=60,
                button_style='warning',
                knowledge_articles=[HUMAN_OPS_ARTICLE],
                post_functions=[
                    {'type': 'assign_team', 'value': 'human-ops'},
                    {'type': 'send_notification', 'template': 'status_changed',
                     'recipients': ['assignee', 'watchers']},
                ],
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0052_projectteamlead'),
    ]

    operations = [
        migrations.RunPython(apply_human_ops, noop),
    ]
