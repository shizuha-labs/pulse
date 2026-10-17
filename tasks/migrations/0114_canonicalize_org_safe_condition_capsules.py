"""Canonicalize duplicate organization-local dependency condition capsules.

0106 looked only for its exact generated heading.  A richer, hand-minted local
capsule with a different heading was therefore treated as absent and received a
second generated capsule.  Preserve the usable local capsule, remove only the
conflicting generated duplicate, and archive every repair for safe reversal.
"""

import re

from django.db import migrations


ARCHIVE_KIND = 'condition_capsule_canonicalization'
GENERATED_HEADING = '## Org-safe condition capsule (migration)'
UNKNOWN_TAG = 'migration-condition-unknown'
CAPSULE_HEADING_RE = re.compile(
    r'(?mi)^##[ \t]+org-safe condition capsule[^\n]*$',
)
H2_HEADING_RE = re.compile(r'(?m)^##[ \t]+[^\n]+$')


def _capsule_sections(description):
    text = str(description or '')
    h2_starts = [match.start() for match in H2_HEADING_RE.finditer(text)]
    sections = []
    for match in CAPSULE_HEADING_RE.finditer(text):
        end = next(
            (start for start in h2_starts if start > match.start()),
            len(text),
        )
        heading = match.group(0).strip()
        body = text[match.start():end].rstrip()
        lowered = body.lower()
        sections.append({
            'start': match.start(),
            'end': end,
            'heading': heading,
            'body': body,
            'generated': heading.lower() == GENERATED_HEADING.lower(),
            'usable': (
                'condition summary' in lowered
                and 'verification criteria' in lowered
                and 'migration stamp' in lowered
            ),
        })
    return sections


def _canonicalize_description(description):
    """Return (description, repair metadata) for a duplicate local capsule."""
    text = str(description or '')
    sections = _capsule_sections(text)
    if len(sections) <= 1:
        return text, None

    # A locally authored capsule is the tenant's own decision record.  Prefer
    # the first usable local section over generated snapshots, even when the
    # generated snapshot is also extractable, because the local section can
    # contain a later and more specific owner/evidence handoff.
    canonical = next(
        (
            section for section in sections
            if not section['generated'] and section['usable']
        ),
        None,
    )
    if canonical is None:
        return text, None

    removals = [
        (section['start'], section['end'])
        for section in sections
        if section is not canonical
    ]
    chunks = []
    cursor = 0
    for start, end in removals:
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    repaired = ''.join(chunks)
    repaired = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', repaired).strip()
    return repaired, {
        'version': 2,
        'canonical_source': 'existing_local_capsule',
        'canonical_heading': canonical['heading'],
        'canonicalization_migration': (
            '0114_canonicalize_org_safe_condition_capsules'
        ),
        'superseded_capsule_count': len(removals),
        'superseded_generated_capsule_count': sum(
            1
            for section in sections
            if section is not canonical and section['generated']
        ),
    }


def _without_unknown_tag(tags):
    values = [
        value.strip()
        for value in str(tags or '').split(',')
        if value.strip() and value.strip() != UNKNOWN_TAG
    ]
    return ','.join(values) if values else None


def canonicalize_condition_capsules(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    Archive = apps.get_model('tasks', 'OrganizationIsolationArchive')
    alias = schema_editor.connection.alias

    candidates = (
        Item.objects.using(alias)
        .filter(description__icontains='## Org-safe condition capsule')
        .order_by('id')
    )
    for item in candidates.iterator():
        repaired_description, canonical_metadata = _canonicalize_description(
            item.description,
        )
        if canonical_metadata is None:
            continue

        old_state = {
            'description': item.description,
            'metadata': item.metadata,
            'tags': item.tags,
        }
        metadata = dict(item.metadata or {})
        metadata['org_safe_condition_capsule'] = canonical_metadata
        new_state = {
            'description': repaired_description,
            'metadata': metadata,
            'tags': _without_unknown_tag(item.tags),
        }
        Archive.objects.using(alias).create(
            kind=ARCHIVE_KIND,
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
        .filter(kind=ARCHIVE_KIND)
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
                'Refusing to reverse condition-capsule canonicalization after '
                f'replacement task {item.id} changed.'
            )
        Item.objects.using(alias).filter(pk=item.pk).update(
            **(payload.get('old_state') or {})
        )
    rows.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0113_pls539_direct_handoff_receipt'),
    ]

    operations = [
        migrations.RunPython(
            canonicalize_condition_capsules,
            reverse_code=restore_condition_capsules,
        ),
    ]
