from django.db import migrations


WORKFLOW_SLUG = 'autonomous-dev'
TO_STATUS = 'done'
GATE_TYPE = 'qnt_data_artifact_gate'
GATE = {'type': GATE_TYPE, 'project_keys': ['QNT']}


def _target_transitions(apps):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflows = list(Workflow.objects.filter(slug=WORKFLOW_SLUG).only('pk'))
    # A brand-new/bootstrap database has no runtime workflow rows yet; fixture
    # provisioning creates them after schema migration. That empty-database
    # case is not a malformed live target and must remain installable.
    if not workflows:
        return WorkflowTransition, []
    if len(workflows) != 1:
        raise RuntimeError(
            f'PLAT-4182 expected exactly one {WORKFLOW_SLUG!r} workflow; '
            f'found {len(workflows)}'
        )
    transitions = list(WorkflowTransition.objects.filter(
        workflow_id=workflows[0].pk,
        to_status=TO_STATUS,
    ).only('pk', 'validators'))
    if not transitions:
        raise RuntimeError(
            'PLAT-4182 expected at least one autonomous-dev transition '
            f'into {TO_STATUS!r}; found none'
        )
    return WorkflowTransition, transitions


def _validated_validators(transition):
    validators = transition.validators
    if not isinstance(validators, list):
        raise RuntimeError(
            f'PLAT-4182 target transition {transition.pk} validators must be a list'
        )
    malformed = [
        index for index, validator in enumerate(validators)
        if not isinstance(validator, dict)
        or not isinstance(validator.get('type'), str)
        or not validator.get('type').strip()
    ]
    if malformed:
        raise RuntimeError(
            f'PLAT-4182 target transition {transition.pk} has malformed '
            f'validator entries at indexes {malformed}'
        )
    return validators


def activate_qnt_gate(apps, schema_editor):
    WorkflowTransition, transitions = _target_transitions(apps)
    for transition in transitions:
        validators = _validated_validators(transition)
        gate_count = sum(
            validator.get('type') == GATE_TYPE for validator in validators
        )
        if gate_count:
            raise RuntimeError(
                f'PLAT-4182 target transition {transition.pk} already contains '
                f'{gate_count} qnt_data_artifact_gate(s)'
            )
        WorkflowTransition.objects.filter(pk=transition.pk).update(
            validators=[*validators, GATE],
        )


def remove_qnt_gate(apps, schema_editor):
    WorkflowTransition, transitions = _target_transitions(apps)
    for transition in transitions:
        validators = _validated_validators(transition)
        gate_indexes = [
            index for index, validator in enumerate(validators)
            if validator.get('type') == GATE_TYPE
        ]
        if len(gate_indexes) != 1:
            raise RuntimeError(
                f'PLAT-4182 reverse expected exactly one qnt_data_artifact_gate '
                f'on transition {transition.pk}; found {len(gate_indexes)}'
            )
        gate_index = gate_indexes[0]
        WorkflowTransition.objects.filter(pk=transition.pk).update(
            validators=[
                validator for index, validator in enumerate(validators)
                if index != gate_index
            ],
        )


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0168_pls1047_package_publish_acceptance_mode'),
    ]

    operations = [
        migrations.RunPython(
            activate_qnt_gate,
            remove_qnt_gate,
        ),
    ]
