"""PLS-756: seed server-owned reason_category screen-field contract on Cancel.

Discovery (pulse_get_available_transitions + MCP formatter) must report every
Cancel/Discard transition's required reason_category field with the exact enum
(duplicate|invalid|obsolete|test-artifact). WorkflowTransition.screen_fields is
the single source of truth; migration 0046 seeded those transitions before this
contract existed, so append the field here (idempotent — migrations that already
carry the field are left untouched). The runtime backstop
WorkflowService._ensure_cancel_reason_category_field covers any transition that
later diverges.
"""

from django.db import migrations

REASON_CATEGORY_FIELD = {
    'field': 'reason_category',
    'label': 'Reason category (duplicate, invalid, obsolete, or test-artifact)',
    'required': True,
    'enum': ['duplicate', 'invalid', 'obsolete', 'test-artifact'],
}


def seed_cancel_reason_category(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    qs = WorkflowTransition.objects.filter(to_status='cancelled')
    for t in qs.iterator(chunk_size=500):
        fields = list(t.screen_fields or [])
        if any(str(sf.get('field', '')).lower() == 'reason_category' for sf in fields):
            continue
        WorkflowTransition.objects.filter(pk=t.pk).update(
            screen_fields=fields + [dict(REASON_CATEGORY_FIELD)],
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0164_focusedqabugequivalencekey_focusedqageneration_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_cancel_reason_category, noop),
    ]
