"""
Heal tasks whose `status` isn't a valid slug in their assigned workflow.

A recurring bug class: items created by legacy callers (importers, older
MCP tools, bulk inserts) end up with status='pending' / 'completed' /
etc. against a workflow that doesn't actually contain that slug. The UI
strands them with "No transitions available from X".

This migration applies the same coercion logic as
Item._coerce_invalid_status() to every task once, so AT-79, AT-37, and
any other stranded items get repaired at deploy time. Idempotent:
already-valid statuses are skipped.
"""
import logging
from django.db import migrations


logger = logging.getLogger(__name__)


LEGACY_ALIASES = {
    'pending':   ['todo', 'open', 'pending'],
    'todo':      ['todo', 'open'],
    'open':      ['open', 'todo'],
    'completed': ['done', 'completed', 'closed'],
    'closed':    ['closed', 'done'],
    'cancelled': ['cancelled', 'canceled'],
    'canceled':  ['cancelled', 'canceled'],
    'in_progress': ['in_progress'],
    'blocked':   ['blocked'],
}


def _resolve_workflow(Workflow, workflow_name):
    wf = Workflow.objects.filter(name__iexact=workflow_name).first()
    if wf:
        return wf
    base = workflow_name.lower()
    for s in {
        base,
        base.replace('-', '_'),
        base.replace('_', '-'),
        '-'.join(base.split()),
        '_'.join(base.split()),
    }:
        wf = Workflow.objects.filter(slug__iexact=s).first()
        if wf:
            return wf
    return None


def _heal_status(wf, valid_slugs, current_slug, Status, WorkflowStatus):
    """Return the healed slug or None if no target applies."""
    # 1) Legacy aliases
    for candidate in LEGACY_ALIASES.get(current_slug, []):
        if candidate in valid_slugs:
            return candidate
    # 2) Category match
    from_status = Status.objects.filter(slug=current_slug).first()
    if from_status:
        ws_match = (
            WorkflowStatus.objects
            .filter(workflow=wf, status__category=from_status.category)
            .order_by('position')
            .select_related('status')
            .first()
        )
        if ws_match:
            return ws_match.status.slug
    # 3) Initial
    if wf.initial_status_id:
        initial = Status.objects.filter(id=wf.initial_status_id).first()
        if initial:
            return initial.slug
    return None


def heal_existing(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    Status = apps.get_model('tasks', 'Status')

    healed_count = 0
    unresolved = []

    for it in Item.objects.filter(mode='task').exclude(workflow_name=''):
        if not it.status:
            continue
        wf = _resolve_workflow(Workflow, it.workflow_name)
        if not wf:
            continue
        valid_slugs = set(
            WorkflowStatus.objects
            .filter(workflow=wf)
            .values_list('status__slug', flat=True)
        )
        if it.status in valid_slugs:
            continue  # already valid

        target = _heal_status(wf, valid_slugs, it.status, Status, WorkflowStatus)
        if not target or target == it.status:
            unresolved.append((it.item_key or f'#{it.pk}', it.status, wf.slug))
            continue

        # Resolve the display name for the healed slug.
        target_status = Status.objects.filter(slug=target).first()
        new_workflow_status = target_status.name if target_status else target

        old = it.status
        # .update() keeps the migration idempotent (no signals, no
        # cascading auto-transitions during deploy).
        Item.objects.filter(pk=it.pk).update(
            status=target,
            workflow_status=new_workflow_status,
        )
        healed_count += 1
        print(f"  [heal] {it.item_key or it.pk}: {old!r} -> {target!r} (wf={wf.slug})")

    if unresolved:
        print(f"  [heal] {len(unresolved)} items could not be healed:")
        for row in unresolved:
            print(f"    {row}")
    print(f"  [heal] total repaired: {healed_count}")


def noop_reverse(apps, schema_editor):
    # Non-destructive: we can't know the original (pre-invalid) status,
    # so there's nothing to unwind.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0038_webhookkey_project_group'),
    ]

    operations = [
        migrations.RunPython(heal_existing, noop_reverse),
    ]
