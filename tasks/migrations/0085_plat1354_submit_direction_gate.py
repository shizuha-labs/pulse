from django.db import migrations


VALIDATOR = {
    'type': 'submit_requires_current_direction',
    'message': (
        'Submit-for-review direction gate (PLAT-1354): newer reviewer/architect/lead '
        'direction exists after the linked PR head timestamp. Read/act on the newer '
        'comment(s), push a newer head, or post an explicit author ack/rebuttal '
        '(`ack:` or `PLAT-202 rebuttal:`) before submitting again.'
    ),
}


def _is_review_status(value):
    return 'review' in (value or '').strip().lower().replace('-', '_')


def add_submit_direction_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in WorkflowTransition.objects.all():
        if not _is_review_status(transition.to_status) or _is_review_status(transition.from_status):
            continue
        validators = list(transition.validators or [])
        if any(isinstance(v, dict) and v.get('type') == VALIDATOR['type'] for v in validators):
            continue
        validators.append(dict(VALIDATOR))
        transition.validators = validators
        transition.save(update_fields=['validators'])


def remove_submit_direction_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in WorkflowTransition.objects.all():
        validators = [
            v for v in list(transition.validators or [])
            if not (isinstance(v, dict) and v.get('type') == VALIDATOR['type'])
        ]
        if validators != list(transition.validators or []):
            transition.validators = validators
            transition.save(update_fields=['validators'])


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0084_pls304_external_mcp_gateway'),
    ]

    operations = [
        migrations.RunPython(add_submit_direction_validator, remove_submit_direction_validator),
    ]
