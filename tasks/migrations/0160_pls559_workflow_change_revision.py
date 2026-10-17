"""PLS-559: make failed typed workflow changes recoverable without bypassing seals."""
import json

from django.db import migrations


RECOVERY_NAME = 'Revise After Apply Failure'
SEAL_VALIDATOR = {
    'type': 'typed_spec_matches_trusted_seal',
    'message': (
        "Retry Apply refused because the authoritative typed_change_spec does not "
        "match the trusted pre-approval seal. Use 'Revise After Apply Failure', "
        "then atomically revise, re-seal, and re-approve the request."
    ),
}


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def add_revision_recovery(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug='workflow-change').first()
    if not workflow:
        return
    status_slugs = set(
        workflow.workflow_statuses.values_list('status__slug', flat=True)
    )
    if {'apply_failed', 'proposed'} <= status_slugs:
        transition, _created = WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            name=RECOVERY_NAME,
            from_status='apply_failed',
            to_status='proposed',
            defaults={
                'description': (
                    'Return a failed typed change request to proposal so its authoritative '
                    'payload can be atomically revised, re-sealed, and re-approved.'
                ),
                'position': 12,
                'button_style': 'warning',
                'assignee_only': False,
                'validators': [],
                'post_functions': [
                    {'type': 'assign_to', 'target': 'reporter'},
                    {
                        'type': 'add_comment',
                        'body': (
                            'Apply failure returned to proposal. Revise only through '
                            'pulse_revise_workflow_change; a fresh seal and approval are required.'
                        ),
                    },
                ],
                'knowledge_articles': [{
                    'type': 'inline',
                    'title': 'Typed apply-failure recovery (PLS-559)',
                    'content': (
                        'Use this for a stale or corrected typed payload. It performs no workflow '
                        'mutation. After transition, call pulse_revise_workflow_change with the '
                        'complete corrected envelope; that primitive atomically replaces metadata '
                        'and the rendered JSON projection and submits a fresh pre-approval seal. '
                        'Never edit the description JSON or metadata manually and never bypass §3.7.'
                    ),
                }],
            },
        )
        transition.description = (
            'Return a failed typed change request to proposal so its authoritative '
            'payload can be atomically revised, re-sealed, and re-approved.'
        )
        transition.position = 12
        transition.button_style = 'warning'
        transition.assignee_only = False
        transition.validators = []
        transition.post_functions = [
            {'type': 'assign_to', 'target': 'reporter'},
            {
                'type': 'add_comment',
                'body': (
                    'Apply failure returned to proposal. Revise only through '
                    'pulse_revise_workflow_change; a fresh seal and approval are required.'
                ),
            },
        ]
        transition.knowledge_articles = [{
            'type': 'inline',
            'title': 'Typed apply-failure recovery (PLS-559)',
            'content': (
                'Use this for a stale or corrected typed payload. It performs no workflow '
                'mutation. After transition, call pulse_revise_workflow_change with the complete '
                'corrected envelope; that primitive atomically replaces metadata and the rendered '
                'JSON projection and submits a fresh pre-approval seal. Never edit the description '
                'JSON or metadata manually and never bypass §3.7.'
            ),
        }]
        transition.save(update_fields=[
            'description', 'position', 'button_style', 'assignee_only',
            'validators', 'post_functions', 'knowledge_articles', 'updated_at',
        ])

    for retry in WorkflowTransition.objects.filter(
        workflow=workflow,
        name='Retry Apply',
        from_status='apply_failed',
    ):
        validators = _list(retry.validators)
        validators = [
            validator for validator in validators
            if not (
                isinstance(validator, dict)
                and validator.get('type') == SEAL_VALIDATOR['type']
            )
        ]
        validators.append(dict(SEAL_VALIDATOR))
        retry.validators = validators
        retry.save(update_fields=['validators', 'updated_at'])


def remove_revision_recovery(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug='workflow-change').first()
    if not workflow:
        return
    WorkflowTransition.objects.filter(
        workflow=workflow,
        name=RECOVERY_NAME,
        from_status='apply_failed',
        to_status='proposed',
    ).delete()
    for retry in WorkflowTransition.objects.filter(
        workflow=workflow,
        name='Retry Apply',
        from_status='apply_failed',
    ):
        retry.validators = [
            validator for validator in _list(retry.validators)
            if not (
                isinstance(validator, dict)
                and validator.get('type') == SEAL_VALIDATOR['type']
            )
        ]
        retry.save(update_fields=['validators', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        # PLS-559 was numbered 0159; PLS-792 merged 0159_pls792_run_now_cas_screen_fields
        # to main first, so this migration is 0160 and chains on that leaf to keep the
        # graph strictly linear (two 0159 files was a parallel-PR migration collision).
        ('tasks', '0159_pls792_run_now_cas_screen_fields'),
    ]

    operations = [
        migrations.RunPython(add_revision_recovery, remove_revision_recovery),
    ]
