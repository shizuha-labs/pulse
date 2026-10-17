"""PLAT-10330: require disposition on Review Passed after an unchanged bounce.

Idempotent. Appends the reviewer-side validator to transitions named
Review Passed. Runtime still fail-closes only when bounce metadata is present.
"""

from django.db import migrations

VALIDATOR = {'type': 'reviewer_pass_requires_disposition'}


def seed(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for row in WorkflowTransition.objects.filter(name='Review Passed').iterator():
        validators = list(row.validators or [])
        if any(
            isinstance(v, dict) and v.get('type') == VALIDATOR['type']
            for v in validators
        ):
            continue
        WorkflowTransition.objects.filter(pk=row.pk).update(
            validators=validators + [dict(VALIDATOR)],
        )


def unseed(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for row in WorkflowTransition.objects.filter(name='Review Passed').iterator():
        validators = [
            v for v in list(row.validators or [])
            if not (isinstance(v, dict) and v.get('type') == VALIDATOR['type'])
        ]
        WorkflowTransition.objects.filter(pk=row.pk).update(validators=validators)


class Migration(migrations.Migration):
    dependencies = [
        # PLAT-10330: re-parent onto master's merge leaf — collapses the
        # two-leaf graph (the merge leaf transitively carries 0189_…marker).
        ('tasks', '0190_merge_pls1306_idx_plat10083_provider_wait'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
