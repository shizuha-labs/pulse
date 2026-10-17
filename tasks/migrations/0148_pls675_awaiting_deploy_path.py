"""PLS-675 / PLAT-1351 v4: seed a valid non-terminal deploy path for governed workflows.

Rei P1-1: ``merge_event_target_status`` hard-coded ``awaiting_deploy`` but the
governed workflows (autonomous-bug, autonomous-dev, reviewed-task) had no such
status — a merged deployed/live task would strand at an invalid slug that
``Item._coerce_invalid_status`` heals away (or ``execute_transition`` rejects).

This migration:
  1. Seeds the global ``awaiting_deploy`` Status (category in_progress — never
     terminal, so a merge event can never terminalize a governed task).
  2. Adds ``awaiting_deploy`` to every governed workflow that already has
     ``awaiting_merge`` (autonomous-bug, autonomous-dev, reviewed-task).
  3. Adds the transitions ``awaiting_merge -> awaiting_deploy`` (Merge → Awaiting
     Deploy) and ``awaiting_deploy -> done`` (Deploy Verified → Done, evidence-
     gated by the deploy-verify close gate) so the post-merge path is complete
     and non-terminal.

Idempotent + reversible (mirrors the 0046 ``_upsert_transition`` pattern).
"""
from django.db import migrations

# Workflows that carry the merge lane and are governed by the deploy-verify gate.
GOVERNED_WORKFLOWS = ('autonomous-bug', 'autonomous-dev', 'reviewed-task')

MERGE_ARTICLE = {
    'type': 'inline',
    'title': 'Merge is not deployment verification (PLS-675)',
    'content': (
        'The linked PR is merged, but for deployed/live acceptance this is NOT '
        'deployment verification. The task moves to Awaiting Deploy (non-terminal). '
        'It may only reach a terminal state once a correlated production '
        'DeploymentEvent (task + repo + merged head SHA + env + image/digest + '
        'replicas Ready) exists — live also needs a typed QA/live-probe verdict — '
        'or a role-gated durable waiver is recorded.'
    ),
}

VERIFY_ARTICLE = {
    'type': 'inline',
    'title': 'Deploy Verified — evidence-gated close',
    'content': (
        'Close only after the deploy-verify close gate passes: a correlated '
        'production DeploymentEvent with all replicas Ready (live also needs a '
        'typed QA/live-probe verdict), or a role-gated durable waiver. Free-form '
        'comments are never authority. Merge completion alone is insufficient.'
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
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    awaiting_deploy, _ = Status.objects.update_or_create(
        slug='awaiting_deploy',
        defaults={
            'name': 'Awaiting Deploy',
            'category': 'in_progress',
            'color': '#0ea5e9',
            'position': 16,
            'is_system': False,
            'description': (
                'PR merged but deployment verification is pending (PLS-675). '
                'Non-terminal: deployed/live acceptance cannot close until a '
                'correlated production DeploymentEvent or role-gated waiver.'
            ),
        },
    )

    for workflow in Workflow.objects.filter(slug__in=GOVERNED_WORKFLOWS):
        status_slugs = set(
            workflow.workflow_statuses.values_list('status__slug', flat=True)
        )
        if 'awaiting_merge' not in status_slugs:
            # Not a merge-lane workflow — nothing to wire.
            continue

        # Add the awaiting_deploy status to the workflow (idempotent).
        WorkflowStatus.objects.get_or_create(
            workflow=workflow,
            status=awaiting_deploy,
            defaults={'position': 17},
        )

        # Merge → Awaiting Deploy (non-terminal; the merge-event auto-transition
        # target for deployed/live acceptance).
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Merge — Awaiting Deploy',
            'awaiting_merge',
            'awaiting_deploy',
            description=(
                'PR merged; deployed/live acceptance now requires deployment '
                'verification before terminal close (PLS-675).'
            ),
            position=35,
            button_style='primary',
            knowledge_articles=[MERGE_ARTICLE],
            post_functions=[
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'watchers'],
                },
            ],
        )

        # Awaiting Deploy → Done (evidence-gated by the deploy-verify close gate).
        _upsert_transition(
            WorkflowTransition,
            workflow,
            'Deploy Verified — Close',
            'awaiting_deploy',
            'done',
            description=(
                'Close after the deploy-verify close gate passes: correlated '
                'production DeploymentEvent (live also needs a typed QA/live-probe '
                'verdict) or a role-gated durable waiver.'
            ),
            position=36,
            button_style='success',
            knowledge_articles=[VERIFY_ARTICLE],
            post_functions=[
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'reporter', 'watchers'],
                },
            ],
        )


def remove_paths(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for workflow in Workflow.objects.filter(slug__in=GOVERNED_WORKFLOWS):
        WorkflowTransition.objects.filter(
            workflow=workflow,
            name__in=['Merge — Awaiting Deploy', 'Deploy Verified — Close'],
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0147_pls675_acceptance_mode'),
    ]

    operations = [
        migrations.RunPython(apply_paths, remove_paths),
    ]
