"""Backfill evaluable, redacted condition capsules onto 0105 replacements."""

from django.db import migrations, models

from ._org_safe_condition_capsule_v1 import (
    CAPSULE_HEADING,
    build_capsule,
    merge_unknown_tag,
    upsert_capsule_description,
)


KNOWN_BKSG_28_CONDITION = """Accounting source documents must be present in
the target organization's canonical Drive folders before books-close
dependents proceed. Evidence includes bank statements, Tally backups, invoices,
tax challans and returns, and partner capital records.

## Verification criteria
- Local Accounting confirms the required period artifacts exist.
- The dependent books-close work can proceed from local evidence.
"""


def _migration_stamp(item):
    timestamp = item.created_at.isoformat() if item.created_at else 'unknown-time'
    return f'{timestamp} (0106_backfill_org_safe_condition_capsules)'


def _source_payload(source, former_key):
    if source is None:
        description = (
            KNOWN_BKSG_28_CONDITION
            if former_key == 'BKSG-28'
            else ''
        )
        return {
            'source_key': former_key,
            'source_title': '',
            'source_description': description,
            'source_status': 'unknown',
        }
    description = source.description or ''
    if not description and former_key == 'BKSG-28':
        description = KNOWN_BKSG_28_CONDITION
    return {
        'source_key': former_key or source.item_key or f'#{source.id}',
        'source_title': source.title,
        'source_description': description,
        'source_status': source.status,
    }


def backfill_condition_capsules(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    alias = schema_editor.connection.alias

    replacements = (
        Item.objects.using(alias)
        .filter(
            models.Q(source='org_isolation_migration')
            | models.Q(metadata__org_isolation_replacement=True)
            | models.Q(title__startswith='[Org-local dependency]')
        )
        .order_by('id')
    )
    for item in replacements.iterator():
        metadata = dict(item.metadata or {})
        existing_capsule = metadata.get('org_safe_condition_capsule') or {}
        if (
            CAPSULE_HEADING in str(item.description or '')
            and existing_capsule.get('version') == 1
        ):
            continue

        former_id = metadata.get('former_external_item_id')
        former_key = (
            metadata.get('former_external_item_key')
            or 'unknown'
        )
        source = (
            Item.objects.using(alias).filter(pk=former_id).first()
            if former_id
            else None
        )
        source_payload = _source_payload(source, former_key)
        capsule = build_capsule(
            **source_payload,
            local_owning_team=item.assignment_group or 'admin-ops',
            migration_stamp=_migration_stamp(item),
        )

        new_description = upsert_capsule_description(
            item.description,
            capsule,
        )
        new_metadata = dict(metadata)
        new_metadata['org_safe_condition_capsule'] = capsule
        new_tags = merge_unknown_tag(
            item.tags,
            capsule['extraction_state'] == 'unknown',
        )
        old_state = {
            'description': item.description,
            'metadata': item.metadata,
            'tags': item.tags,
        }
        new_state = {
            'description': new_description,
            'metadata': new_metadata,
            'tags': new_tags,
        }
        Archive.objects.using(alias).create(
            kind='replacement_condition_capsule',
            object_key=str(item.id),
            payload={
                'item_id': item.id,
                'old_state': old_state,
                'new_state': new_state,
            },
        )
        Item.objects.using(alias).filter(pk=item.pk).update(**new_state)


def restore_condition_capsules(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    alias = schema_editor.connection.alias

    rows = (
        Archive.objects.using(alias)
        .filter(kind='replacement_condition_capsule')
        .order_by('-id')
    )
    for row in rows:
        payload = row.payload or {}
        item = (
            Item.objects.using(alias)
            .filter(pk=payload.get('item_id'))
            .first()
        )
        if item is None:
            continue
        current_state = {
            'description': item.description,
            'metadata': item.metadata,
            'tags': item.tags,
        }
        if current_state != (payload.get('new_state') or {}):
            raise RuntimeError(
                'Refusing to reverse the condition-capsule backfill after '
                f'replacement task {item.id} changed.'
            )
        Item.objects.using(alias).filter(pk=item.pk).update(
            **(payload.get('old_state') or {})
        )
    rows.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0105_enforce_item_org_isolation'),
    ]

    operations = [
        migrations.RunPython(
            backfill_condition_capsules,
            reverse_code=restore_condition_capsules,
        ),
    ]
