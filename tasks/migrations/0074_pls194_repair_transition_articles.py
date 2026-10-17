from django.db import migrations


TRANSITION_ID = '391e05e4-6a6a-400f-b174-63205c01a69b'

MERGE_TRANSITION_ARTICLES = [
    {
        'type': 'inline',
        'title': 'Merge + close — REVIEW TEAM merges as kai2574',
        'content': (
            'Only the review/merge lane closes this transition. Confirm the linked PR has review approval, '
            'no unresolved blocking findings, and is mergeable; merge as the review/merge identity with the '
            'workflow-approved merge path, not as the PR author.'
        ),
    },
    {
        'type': 'inline',
        'title': 'Wiki-lifecycle close gate',
        'content': (
            'Before closing, ensure durable knowledge from the change is either documented in the canonical '
            'wiki page/runbook/ADR or explicitly marked N/A in the task handoff. Pulse comments hold live '
            'state; reusable design/decision/runbook knowledge belongs in the wiki.'
        ),
    },
    {
        'type': 'inline',
        'title': 'PLS-186: deployment/QA verification is a separate lane',
        'content': (
            'For autonomous-dev, Done means the implementation was reviewed and merged; do not reintroduce '
            'deploy/QA verification as a blocking WIP stage on this workflow. If CI rollout, smoke, or live '
            'QA verification remains, create/link the appropriate deployment-event, DevOps, or QA follow-up '
            'per PLS-183/PLS-186 and let that lane own it.'
        ),
    },
]


def repair_transition_articles(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowTransition.objects.filter(id=TRANSITION_ID).update(
        knowledge_articles=MERGE_TRANSITION_ARTICLES,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0073_pls183_deployment_event'),
    ]

    operations = [
        migrations.RunPython(repair_transition_articles, migrations.RunPython.noop),
    ]
