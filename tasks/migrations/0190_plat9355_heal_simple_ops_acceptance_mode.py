"""PLAT-9355: heal system simple-workflow (ops/no-PR) items' acceptance_mode to code.

The system ``simple`` workflow has no PR/merge lane — its lifecycle is
operator/agent work recorded as evidence, not code merged through a repo. At
create time before this fix, Item.save defaulted simple-workflow ops tasks to
``live`` whenever they carried a service-affecting / user-facing / infra-
control-plane risk label describing the surface the ops work touches — so the
deploy-verify gate demanded a merged-head DeploymentEvent the workflow class
can never produce, stranding every operational fix at the done transition
(HIVE-2648 was blocked 2x for exactly this reason).

The exemption is structural (the system workflow slug), not label-based: it
can never over-exempt deploy-governed work on a PR-driven workflow. This data
migration heals existing rows to ``code`` (not deploy-governed). The
resolution default is corrected in WorkflowService._resolve_acceptance_mode
(arm placed before the raw-field short-circuit so persisted-live rows are
reachable) and the gate exempts simple-ops items in
_collect_deploy_verify_missing as a durable safety net. Idempotent — rows
already at code (or any valid non-live value) are left alone; only live
simple-workflow rows are healed.
"""
from django.db import migrations


def _norm(value):
    """Mirror Workflow.resolve's space/hyphen/underscore tolerance."""
    return str(value or '').strip().lower()


def _simple_workflow_identifiers(apps):
    """Normalized name/slug spellings that resolve to the system simple wf."""
    Workflow = apps.get_model('tasks', 'Workflow')
    identifiers = set()
    for wf in Workflow.objects.filter(slug='simple', is_system=True):
        name = _norm(wf.name)
        slug = _norm(wf.slug)
        identifiers.add(slug)
        identifiers.add(name)
        identifiers.add(name.replace('-', '_'))
        identifiers.add(name.replace('_', '-'))
        identifiers.add('-'.join(name.split()))
        identifiers.add('_'.join(name.split()))
    identifiers.discard('')
    return identifiers


def heal_simple_ops_to_code(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    identifiers = _simple_workflow_identifiers(apps)
    if not identifiers:
        return
    updated = 0
    for item in Item.objects.filter(
        mode='task',
        acceptance_mode='live',
    ).iterator(chunk_size=500):
        if _norm(getattr(item, 'workflow_name', None)) not in identifiers:
            continue
        item.acceptance_mode = 'code'
        item.save(update_fields=['acceptance_mode', 'updated_at'])
        updated += 1
    if updated:
        print(f'PLAT-9355: healed {updated} simple-workflow ops item(s) to acceptance_mode=code')


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0189_plat10083_provider_wait_marker"),
    ]

    operations = [
        migrations.RunPython(heal_simple_ops_to_code, migrations.RunPython.noop),
    ]
