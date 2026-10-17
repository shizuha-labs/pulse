"""PLS-1047: heal package-publish artifacts' acceptance_mode to code.

Package-publish tasks (e.g. ORIG-232 @shizuha/ui) deliver a published package
version (npm/Verdaccio), not a k8s DeploymentEvent — they have no repo/PR/merge/
deploy of their own that produces a correlated deployment. At create time before
this fix, Item.save defaulted them to ``live`` whenever they carried a risk
label (user-facing / infra-control-plane / etc.) describing the surface the
package feeds — so the deploy-verify gate demanded a merged-head SHA they can
never have and stranded them in ``in_review``.

The exemption is an explicit task-metadata rule: only tasks carrying the
``package-publish`` / ``package_publish`` label/tag are exempted, so it can
NEVER over-exempt deploy-governed work. This data migration heals existing rows
to ``code`` (not deploy-governed). The create-time default is corrected in
WorkflowService._resolve_acceptance_mode / Item.save and the gate exempts
package-publish artifacts in _collect_deploy_verify_missing as a durable safety
net. Idempotent — rows already at code (or any valid non-live value) are left
alone; only live package-publish artifacts are healed.
"""
from django.db import migrations


def _label_tokens(value):
    if not value:
        return set()
    if isinstance(value, dict):
        tokens = set(str(k).strip().lower() for k in value.keys())
        tokens.update(
            str(v).strip().lower() for v in value.values()
            if isinstance(v, str)
        )
        return tokens
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    return {
        t.strip().lower() for t in str(value).replace(';', ',').split(',')
        if t.strip()
    }


def _is_package_publish_artifact(item):
    tokens = _label_tokens(getattr(item, 'labels', None))
    tokens |= _label_tokens(getattr(item, 'tags', None))
    if not (
        'package-publish' in tokens or 'package_publish' in tokens
    ):
        return False
    # A task that also carries a projectable deployment event stays governed —
    # the marker alone does not exempt deploy-governed work.
    metadata = getattr(item, 'metadata', None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if str(metadata.get('focused_qa_deployment_event_id') or '').strip():
        return False
    return True


def heal_package_publish_to_code(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    updated = 0
    for item in Item.objects.filter(
        mode='task',
        acceptance_mode__in=['live', 'deployed'],
    ).iterator(chunk_size=500):
        if not _is_package_publish_artifact(item):
            continue
        item.acceptance_mode = 'code'
        item.save(update_fields=['acceptance_mode', 'updated_at'])
        updated += 1
    if updated:
        print(f'PLS-1047: healed {updated} package-publish artifact(s) to acceptance_mode=code')


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0167_backfill_focused_qa_generations"),
    ]

    operations = [
        migrations.RunPython(heal_package_publish_to_code, migrations.RunPython.noop),
    ]
