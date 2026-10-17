"""PLS-902: return a fresh Awaiting Deploy corrective PR to Review.

The deployed/live workflow can legitimately discover that an already-merged
increment needs a corrective PR.  Awaiting Deploy previously had no legal edge
back to Review, leaving that PR outside author -> reviewer -> merger ownership.

This additive edge admits only provider-confirmed open Origin PRs whose exact
head differs from every prior merged linked head, then uses the ordinary Review
team routing chain.  It is idempotent and rollback removes only this edge.
"""

from django.db import migrations


GOVERNED_WORKFLOWS = ('autonomous-bug', 'autonomous-dev', 'reviewed-task')
TRANSITION_NAME = 'Submit Corrective PR for Code Review'

ARTICLE = {
    'type': 'inline',
    'title': 'Corrective PR re-entry from Awaiting Deploy (PLS-902)',
    'content': (
        'Use this only when an already-merged task needs a new corrective PR. '
        'Pulse re-fetches the linked PR at this transition boundary and admits '
        'only an open canonical Origin PR with an exact head different from '
        'every prior merged head. The task then returns to the Review team; '
        'author != reviewer != merger remains mandatory. Deploy Verified — '
        'Close stays unavailable until the corrective head is merged and has '
        'correlated deployment evidence.'
    ),
}


def _upsert_transition(WorkflowTransition, workflow):
    defaults = {
        'description': (
            'Route a newly linked corrective Origin PR from Awaiting Deploy '
            'back through formal Review (PLS-902).'
        ),
        'position': 34,
        'button_style': 'primary',
        'validators': [
            {'type': 'awaiting_deploy_corrective_pr'},
            {'type': 'submit_requires_current_direction'},
        ],
        'knowledge_articles': [ARTICLE],
        'post_functions': [
            {'type': 'stash_implementer'},
            {'type': 'assign_team', 'value': 'review'},
            {'type': 'stamp_review_started'},
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
    }
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
        name=TRANSITION_NAME,
        from_status='awaiting_deploy',
        to_status='in_review',
    ).first()
    if transition is None:
        WorkflowTransition.objects.create(
            workflow=workflow,
            name=TRANSITION_NAME,
            from_status='awaiting_deploy',
            to_status='in_review',
            **defaults,
        )
        return
    for field, value in defaults.items():
        setattr(transition, field, value)
    transition.save(update_fields=list(defaults))


def add_review_reentry(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for workflow in Workflow.objects.filter(slug__in=GOVERNED_WORKFLOWS):
        status_slugs = set(
            workflow.workflow_statuses.values_list('status__slug', flat=True)
        )
        if {'awaiting_deploy', 'in_review'} <= status_slugs:
            _upsert_transition(WorkflowTransition, workflow)


def remove_review_reentry(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowTransition.objects.filter(
        workflow__slug__in=GOVERNED_WORKFLOWS,
        name=TRANSITION_NAME,
        from_status='awaiting_deploy',
        to_status='in_review',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0154_pls521_outbox_delivery_claim'),
    ]

    operations = [
        migrations.RunPython(add_review_reentry, remove_review_reentry),
    ]
