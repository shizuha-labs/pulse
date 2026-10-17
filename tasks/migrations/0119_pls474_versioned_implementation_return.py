"""Normalize implementation-return post-functions for every review bounce.

PLS-474 replaces the legacy unversioned ``metadata.implementer`` snapshot with
the workflow engine's versioned implementation-return context.  The engine can
only take its row-locked single-save path when the transition advertises the
``assign_implementer`` post-function, so keep all affected workflow rows on the
same contract instead of relying on their historical display names/config.
"""

from django.db import migrations


WORKFLOWS = {'reviewed-task', 'autonomous-bug', 'autonomous-dev'}
RETURN_NAMES = {'Request Changes', 'Return for Changes', 'QA Failed'}
IMPLEMENTATION_SOURCES = {'in_progress', 'reopened', 'applying'}
REVIEW_TARGETS = {'in_review', 'in_qa', 'awaiting_merge'}
CONFLICTING_RETURN_ASSIGNMENTS = {'assign_team', 'assign_to', 'assign', 'route_merge'}


def normalize_implementation_return_edges(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    transitions = WorkflowTransition.objects.filter(workflow__slug__in=WORKFLOWS)
    for transition in transitions:
        post_functions = [
            dict(post_function)
            for post_function in (transition.post_functions or [])
            if isinstance(post_function, dict)
        ]
        changed = False

        if (
            transition.from_status in IMPLEMENTATION_SOURCES
            and transition.to_status in REVIEW_TARGETS
        ):
            post_functions = [
                post_function for post_function in post_functions
                if post_function.get('type') != 'stash_implementer'
            ]
            post_functions.insert(0, {'type': 'stash_implementer'})
            changed = post_functions != (transition.post_functions or [])

        if (
            transition.name in RETURN_NAMES
            and transition.to_status in IMPLEMENTATION_SOURCES
        ):
            # Return ownership comes exclusively from the recorded context.
            # A legacy assign_team/assign_to after assign_implementer could
            # silently overwrite the final team/owner tuple.
            filtered = [
                post_function for post_function in post_functions
                if post_function.get('type') not in CONFLICTING_RETURN_ASSIGNMENTS
                and post_function.get('type') != 'assign_implementer'
            ]
            insert_at = next(
                (
                    index for index, post_function in enumerate(filtered)
                    if post_function.get('type') == 'send_notification'
                ),
                len(filtered),
            )
            filtered.insert(insert_at, {'type': 'assign_implementer'})
            changed = changed or filtered != (transition.post_functions or [])
            post_functions = filtered

        if changed:
            transition.post_functions = post_functions
            transition.save(update_fields=['post_functions'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0118_repair_org_local_focused_qa_review_routes'),
    ]

    operations = [
        migrations.RunPython(normalize_implementation_return_edges, noop),
    ]
