"""0042 only patched transitions literally named "Submit for Review". The
workflows actually in use (Autonomous Dev / Autonomous Bug) submit to review via
per-pod transitions ("Submit for Code Review", "Submit for Engineering Review",
etc.), which were missed. Stash the implementer on EVERY in_progress -> in_review
edge, regardless of name, and PREPEND it so it captures the implementer before
`assign_team` reassigns the task to a reviewer.
"""
from django.db import migrations


def prepend_stash(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for t in WorkflowTransition.objects.filter(from_status='in_progress', to_status='in_review'):
        pfs = list(t.post_functions or [])
        if any(p.get('type') == 'stash_implementer' for p in pfs):
            continue
        pfs.insert(0, {'type': 'stash_implementer'})
        t.post_functions = pfs
        t.save(update_fields=['post_functions'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0042_review_changes_routing'),
    ]

    operations = [
        migrations.RunPython(prepend_stash, noop),
    ]
