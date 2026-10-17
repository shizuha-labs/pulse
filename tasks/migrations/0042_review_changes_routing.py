"""Wire the review-rejection bounce: capture the implementer on "Submit for
Review" and auto-route the task back to them on "Request Changes" / "QA Failed".

Migration 0023 seeded these transitions with no post_functions, so a rejection
just flipped in_review -> in_progress and left the task on the reviewer (or
unassigned). This patches every existing transition row in place.
"""
from django.db import migrations


def add_review_routing(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    def ensure_pf(name_filter, pf, prepend=False):
        for t in WorkflowTransition.objects.filter(**name_filter):
            pfs = list(t.post_functions or [])
            if any(p.get('type') == pf['type'] for p in pfs):
                continue
            if prepend:
                pfs.insert(0, pf)
            else:
                pfs.append(pf)
            t.post_functions = pfs
            t.save(update_fields=['post_functions'])

    # Capture who implemented it, BEFORE any reviewer reassignment (prepend).
    ensure_pf({'name': 'Submit for Review'}, {'type': 'stash_implementer'}, prepend=True)
    # Bounce back to the implementer on rejection.
    ensure_pf({'name': 'Request Changes'}, {'type': 'assign_implementer'})
    ensure_pf({'name': 'QA Failed'}, {'type': 'assign_implementer'})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0041_seed_deferred_status'),
    ]

    operations = [
        migrations.RunPython(add_review_routing, noop),
    ]
