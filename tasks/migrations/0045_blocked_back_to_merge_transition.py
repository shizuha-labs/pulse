"""Allow resolved merge blockers to return to the merge lane."""
from django.db import migrations


def add_blocked_back_to_merge_transition(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.all():
        status_slugs = set(
            workflow.workflow_statuses.values_list('status__slug', flat=True)
        )
        if not {'blocked', 'awaiting_merge'} <= status_slugs:
            continue

        WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            name='Blocker Resolved - Back to Merge',
            from_status='blocked',
            to_status='awaiting_merge',
            defaults={
                'description': (
                    'Use when an Admin Ops/repo/access blocker has been resolved '
                    'and an already-approved PR should return to merge-team landing.'
                ),
                'position': 52,
                'button_style': 'primary',
                'post_functions': [
                    {'type': 'route_merge'},
                    {
                        'type': 'send_notification',
                        'template': 'status_changed',
                        'recipients': ['assignee'],
                    },
                    {
                        'type': 'add_comment',
                        'body': (
                            'Resolved blocker - returned to merge-team landing. '
                            'Merge commit only; never squash/rebase/force-push.'
                        ),
                    },
                ],
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0044_team_assignment_invariants'),
    ]

    operations = [
        migrations.RunPython(add_blocked_back_to_merge_transition, noop),
    ]
