"""PLS-1185: wire the focused-QA disposition into transition execution.

The seeded FAIL (``Confirm Product Failure``) and SUBSUMED (``Close Duplicate /
Subsumed``) transitions promised their side effects in the transition articles
(immutable receipt + owning-project bug + links; duplicate link + receipt) but
their ``post_functions`` only carried ``set_resolution`` + ``send_notification``
— and no post-function executor type dispatched the disposition service at all.
Live canaries PLAT-7637/7639/7640 caught the resulting silent no-ops.

This migration appends the ``focused_qa_disposition`` post-function (executed by
``WorkflowService.execute_post_functions`` inside the transition's atomic block)
to both seeded transitions. Idempotent upsert; reverse removes the entry.
"""
from django.db import migrations
import json


WORKFLOW_SLUG = 'focused-qa'
TRANSITION_NAMES = ('Confirm Product Failure', 'Close Duplicate / Subsumed')
PF = {'type': 'focused_qa_disposition'}


def _normalized_pfs(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [pf for pf in raw if isinstance(pf, dict)]


def forwards(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    wf = Workflow.objects.filter(slug=WORKFLOW_SLUG).first()
    if wf is None:
        return
    for name in TRANSITION_NAMES:
        transition = WorkflowTransition.objects.filter(workflow=wf, name=name).first()
        if transition is None:
            continue
        pfs = _normalized_pfs(transition.post_functions)
        if any(pf.get('type') == PF['type'] for pf in pfs):
            continue
        # Append last so the receipt commits after the ordinary state
        # mutations (set_resolution) in the same atomic batch.
        pfs.append(dict(PF))
        transition.post_functions = pfs
        transition.save(update_fields=['post_functions'])


def backwards(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    wf = Workflow.objects.filter(slug=WORKFLOW_SLUG).first()
    if wf is None:
        return
    for name in TRANSITION_NAMES:
        transition = WorkflowTransition.objects.filter(workflow=wf, name=name).first()
        if transition is None:
            continue
        pfs = _normalized_pfs(transition.post_functions)
        filtered = [pf for pf in pfs if pf.get('type') != PF['type']]
        if len(filtered) != len(pfs):
            transition.post_functions = filtered
            transition.save(update_fields=['post_functions'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0172_item_current_state_item_current_state_updated_at'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
