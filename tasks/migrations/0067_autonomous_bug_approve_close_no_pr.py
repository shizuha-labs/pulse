"""PLS-159: give autonomous-bug a clean no-PR close path (stops the done<->review loop).

The autonomous-bug workflow had NO "Approve & Close (no PR / docs)" transition, so a
no-PR bug task could only leave In Review via "Review Passed" -> Awaiting Merge (the
operator-merge path). At Awaiting Merge the route_merge guard tells it to "use Approve
& Close (no PR / docs)" -- a transition that did not exist -- leaving the task with no
clean terminal exit, so it cycled (PLAT-972, PLAT-973, 4+ times each). autonomous-dev
already has this transition; this seeds the equivalent into autonomous-bug from BOTH
In Review (the correct forward path for a no-PR fix) and Awaiting Merge (rescue for
tasks already stranded on the merge path). Idempotent + reversible.
"""
from django.db import migrations

_KA = [{
    'type': 'inline',
    'title': 'Use for docs / config / no-PR bug fixes',
    'content': (
        "Approve + CLOSE directly. Use this for a bug whose fix has NO shizuha-labs code "
        "PR to merge - config/docs/data fixes, not-reproducible-after-change, or "
        "verified-resolved-without-a-patch. For a bug WITH a code PR, use Review Passed -> "
        "Awaiting Merge -> Merge & Close instead. Before closing, confirm the fix is "
        "verified on the running system (or state why no code change was needed)."
    ),
}]
_PF = [{'type': 'send_notification', 'template': 'status_changed', 'recipients': ['reporter', 'watchers']}]
_NAME = 'Approve & Close (no PR / docs)'


def add_approve_close(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    wf = Workflow.objects.filter(slug='autonomous-bug').first()
    if not wf:
        return
    status_slugs = set(wf.workflow_statuses.values_list('status__slug', flat=True))
    if 'done' not in status_slugs:
        return
    for frm, pos in (('in_review', 33), ('awaiting_merge', 34)):
        if frm not in status_slugs:
            continue
        WorkflowTransition.objects.get_or_create(
            workflow=wf,
            name=_NAME,
            from_status=frm,
            to_status='done',
            defaults={
                'description': (
                    'Approve and close a bug that has no code PR to merge (config/docs/data '
                    'fix or verified-resolved-without-a-patch). Use instead of the '
                    'operator-merge path for no-PR fixes.'
                ),
                'position': pos,
                'button_style': 'primary',
                'post_functions': _PF,
                'knowledge_articles': _KA,
            },
        )


def remove_approve_close(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    Workflow = apps.get_model('tasks', 'Workflow')
    wf = Workflow.objects.filter(slug='autonomous-bug').first()
    if wf:
        WorkflowTransition.objects.filter(
            workflow=wf, name=_NAME, from_status__in=['in_review', 'awaiting_merge'],
            to_status='done',
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0066_design_review_thread_post_function'),
    ]
    operations = [
        migrations.RunPython(add_approve_close, remove_approve_close),
    ]
