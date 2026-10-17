"""PLS-240: reopen terminal autonomous work back into review.

Prematurely closed autonomous-bug/dev tasks with a still-relevant PR had no path
out of Done, so pulse_reopen_task no-oped and open PRs were orphaned behind a
terminal task. Add an explicit terminal -> in_review transition for the affected
workflows; the existing review-entry gate still requires a linked PR or no-pr
marker.
"""
from django.db import migrations


WORKFLOWS = ('autonomous-bug', 'autonomous-dev', 'bug-workflow')
TERMINALS = ('done', 'completed', 'closed')

KNOWLEDGE = [{
    'type': 'inline',
    'title': 'Reopen terminal work for review',
    'content': (
        'Use when a task was closed prematurely while a real PR or no-PR review '
        'deliverable still needs the review/merge flow. The review-entry gate '
        'still applies: link the real PR with pulse_link_pr, or mark no-pr for '
        'docs/config/no-code work, before reopening.'
    ),
}]

POST_FUNCTIONS = [
    {'type': 'clear_resolution'},
    {'type': 'assign_team', 'value': 'review'},
    {'type': 'send_notification', 'template': 'status_changed', 'recipients': ['assignee', 'watchers']},
]


def add_reopen_for_review(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.filter(slug__in=WORKFLOWS):
        slugs = set(workflow.workflow_statuses.values_list('status__slug', flat=True))
        if 'in_review' not in slugs:
            continue
        for source in TERMINALS:
            if source not in slugs:
                continue
            WorkflowTransition.objects.update_or_create(
                workflow=workflow,
                name='Reopen for Review',
                from_status=source,
                to_status='in_review',
                defaults={
                    'description': (
                        'Reopen terminal work that still has a reviewable PR/no-PR '
                        'deliverable and route it back to the review board.'
                    ),
                    'position': 8,
                    'button_style': 'primary',
                    'validators': [{
                        'type': 'pr_linked_or_flagged',
                        'message': (
                            'Cannot reopen directly for review without a linked PR. '
                            'Call pulse_link_pr(task, pr_url), or add no-pr for a '
                            'docs/config/no-code review item.'
                        ),
                    }],
                    'post_functions': POST_FUNCTIONS,
                    'knowledge_articles': KNOWLEDGE,
                },
            )


def remove_reopen_for_review(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowTransition.objects.filter(
        workflow__in=Workflow.objects.filter(slug__in=WORKFLOWS),
        name='Reopen for Review',
        from_status__in=TERMINALS,
        to_status='in_review',
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0082_merge_pls228_pls263'),
    ]

    operations = [
        migrations.RunPython(add_reopen_for_review, remove_reopen_for_review),
    ]
