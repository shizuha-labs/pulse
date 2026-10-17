"""Seed an 'Escalate to DevOps' route across workflows.

This is for internal infrastructure/provisioning gaps that agents cannot fix from
their normal runtime but DevOps can self-serve: host/cluster access, credential
broker wiring, registry/kubeconfig/runtime deployment, repo write identity, and
similar operational access issues. It intentionally routes to a ready state, not
blocked, so DevOps receives actionable work instead of a parked blocker.
"""
from django.db import migrations


DEVOPS_ARTICLE = {
    'type': 'inline',
    'title': 'Escalate to DevOps — internal infra/provisioning only',
    'content': (
        'Use when progress is blocked by internal infrastructure, deployment, '
        'runtime, credential-broker, registry, kubeconfig, repo-write identity, '
        'or production-access provisioning that DevOps owns and can self-serve. '
        'Do NOT use this for product decisions, billing/legal/operator-only '
        'approval, physical hands-on work, or ordinary implementation uncertainty. '
        'Include exact evidence: failing command/API, host/pod/service/repo, missing '
        'permission or credential, and verification expected after DevOps fixes it.'
    ),
}

ESCALATION_NAME = 'Escalate to DevOps'


def _ready_status_for(workflow):
    statuses = list(
        workflow.workflow_statuses
        .exclude(status__category__in=['done', 'scheduled'])
        .order_by('position')
        .values_list('status__slug', 'status__category')
    )
    for slug, category in statuses:
        if category == 'todo':
            return slug
    for slug, category in statuses:
        if category == 'backlog':
            return slug
    for slug, _category in statuses:
        if slug != 'blocked':
            return slug
    return None


def _upsert_named_transition(WorkflowTransition, workflow, from_status, to_status):
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
        name=ESCALATION_NAME,
        from_status=from_status,
        to_status=to_status,
    ).first()
    defaults = {
        'description': (
            'Route internal infra/provisioning/runtime/credential/repo-write access '
            'gaps to DevOps. Not for operator-only decisions or physical work.'
        ),
        'position': 55,
        'button_style': 'warning',
        'knowledge_articles': [DEVOPS_ARTICLE],
        'post_functions': [
            {'type': 'assign_team', 'value': 'devops'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'watchers']},
            {'type': 'add_comment',
             'body': '⬇️ Escalated to DevOps — internal infra/provisioning gap. '
                     'DevOps owns this self-serve path; use Human Ops/Operator only '
                     'for true human-only action.'},
        ],
    }
    if transition is None:
        WorkflowTransition.objects.create(
            workflow=workflow,
            name=ESCALATION_NAME,
            from_status=from_status,
            to_status=to_status,
            **defaults,
        )
        return
    for field, value in defaults.items():
        setattr(transition, field, value)
    transition.save(update_fields=list(defaults.keys()))


def apply_devops_escalation(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.all():
        ready_status = _ready_status_for(workflow)
        if not ready_status:
            continue
        _upsert_named_transition(
            WorkflowTransition,
            workflow,
            '*',
            ready_status,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0068_raise_admin_ops_routes_team'),
    ]

    operations = [
        migrations.RunPython(apply_devops_escalation, noop),
    ]
