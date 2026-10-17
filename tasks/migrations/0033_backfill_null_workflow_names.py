"""Backfill Items with NULL workflow_name.

Older creation paths (pre-workflow-scheme, webhook importers, some direct
ORM saves) could leave `workflow_name` blank on mode='task' items. That
strands them — the transition API has no workflow to look transitions up
in, and the item can never advance. AT-60 is the representative case.

This migration resolves a workflow for every such item using the same
project-scheme cascade the model's save() now uses:
  1. Project → ProjectWorkflow → WorkflowScheme mapping by item_type
  2. Scheme's default_workflow
  3. Global 'simple' system workflow

Alerts (mode != 'task') are exempt; they use a separate state machine.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    Workflow = apps.get_model('tasks', 'Workflow')
    ProjectWorkflow = apps.get_model('tasks', 'ProjectWorkflow')
    WorkflowSchemeMapping = apps.get_model('tasks', 'WorkflowSchemeMapping')

    simple = Workflow.objects.filter(slug='simple', is_system=True).first()
    if simple is None:
        # Nothing we can do — the system workflow seed is missing.
        # This shouldn't happen in normal installs; surface it instead of
        # silently skipping so the operator notices.
        raise RuntimeError(
            "Cannot backfill workflow_name: the system 'simple' workflow is "
            "missing from tasks_workflow. Seed it before re-running."
        )

    def resolve_for(item):
        # Scheme-driven resolution first
        if item.project_id:
            try:
                pw = ProjectWorkflow.objects.filter(project_id=item.project_id).first()
                if pw and pw.workflow_scheme_id:
                    mapping = WorkflowSchemeMapping.objects.filter(
                        scheme_id=pw.workflow_scheme_id,
                        item_type=item.item_type,
                    ).select_related('workflow').first()
                    if mapping and mapping.workflow_id:
                        return Workflow.objects.filter(id=mapping.workflow_id).first()
                    # Fallback to scheme's default_workflow
                    scheme = pw.workflow_scheme
                    if scheme and scheme.default_workflow_id:
                        return Workflow.objects.filter(id=scheme.default_workflow_id).first()
            except Exception:
                pass
        return simple

    touched = 0
    skipped = 0
    for item in Item.objects.filter(mode='task').filter(
        models_q_null_or_empty('workflow_name')
    ):
        wf = resolve_for(item)
        if wf is None:
            skipped += 1
            continue
        item.workflow_name = wf.name
        if not item.workflow_status and wf.initial_status_id:
            # Use the initial status's name (matches the runtime save() path)
            initial = wf.initial_status
            item.workflow_status = initial.name
            # Keep legacy status field in sync if it's still the generic default
            if item.status in ('', 'pending'):
                item.status = f"{initial.name} [{initial.category}]"
        item.save(update_fields=['workflow_name', 'workflow_status', 'status'])
        touched += 1

    print(f"[0033] Backfilled workflow_name on {touched} task item(s). Skipped: {skipped}.")


def models_q_null_or_empty(field):
    # Helper — avoids importing Q at module top in a way that clashes with
    # Django's migration executor on some versions.
    from django.db.models import Q
    return Q(**{f'{field}__isnull': True}) | Q(**{field: ''})


def noop_reverse(apps, schema_editor):
    # No reliable reverse — we don't know which tasks were NULL before.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0032_item_is_archived'),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=noop_reverse),
    ]
