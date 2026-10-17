"""PLAT-6192: reject-as-fixed/closure on security-finding requires live-boundary evidence.

Retro 14 Aug 2026: DOJO-40 was marked rejected (already-fixed) on helper/unit
proof (sanitizer denylist tests fed crafted marker strings) while the real
create-session caller still leaked paraphrased internals in production
(DOJO-54 P1 live). This adds the `live_boundary_evidence` validator to the
security-finding Reject transition so a reject-as-fixed verdict is denied unless
the item carries a `## Live-boundary evidence` comment section proving the fix
through the real caller sequence with the persisted layer asserted safe.
"""

from django.db import migrations


def add_live_boundary_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    updated = 0
    for t in WorkflowTransition.objects.filter(name__iexact='Reject'):
        validators = list(t.validators or [])
        if any(v.get('type') == 'live_boundary_evidence' for v in validators):
            continue
        validators.append({
            'type': 'live_boundary_evidence',
            'message': (
                'Reject-as-fixed denied (PLAT-6192): post a `## Live-boundary evidence` '
                'comment showing the fix through the REAL caller sequence (API/CLI/browser '
                'command + URL) AND asserting the persisted layer (DB row / session history / '
                'API re-read) is safe and equal to the rendered output. Helper/unit-level '
                'proof alone is denied.'
            ),
        })
        t.validators = validators
        t.save(update_fields=['validators'])
        updated += 1
    if updated == 0:
        # Fallback: match the known security-finding Reject transition by UUID.
        t = WorkflowTransition.objects.filter(
            id='c102e019-abf9-44d8-b4fb-b202b32ef223'
        ).first()
        if t:
            validators = list(t.validators or [])
            if not any(v.get('type') == 'live_boundary_evidence' for v in validators):
                validators.append({
                    'type': 'live_boundary_evidence',
                    'message': (
                        'Reject-as-fixed denied (PLAT-6192): post a `## Live-boundary evidence` '
                        'comment showing the fix through the REAL caller sequence (API/CLI/browser '
                        'command + URL) AND asserting the persisted layer (DB row / session history / '
                        'API re-read) is safe and equal to the rendered output. Helper/unit-level '
                        'proof alone is denied.'
                    ),
                })
                t.validators = validators
                t.save(update_fields=['validators'])
                updated += 1


def remove_live_boundary_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for t in WorkflowTransition.objects.filter(name__iexact='Reject'):
        validators = [v for v in (t.validators or [])
                      if v.get('type') != 'live_boundary_evidence']
        t.validators = validators
        t.save(update_fields=['validators'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0162_pls712_item_labels_gin'),
    ]

    operations = [
        migrations.RunPython(add_live_boundary_validator, remove_live_boundary_validator),
    ]
