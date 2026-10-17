from django.db import migrations


WORKFLOW_SLUG = 'security-finding'
FROM_STATUS = 'awaiting_merge'
TO_STATUS = 'verification'
GATE_TYPE = 'deployment_evidence_gate'
GATE = {'type': GATE_TYPE}


def _target_transition(apps):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflows = list(Workflow.objects.filter(slug=WORKFLOW_SLUG).only('pk'))
    # A brand-new/bootstrap database has no runtime workflow rows yet; fixture
    # provisioning creates them after schema migration.  That empty-database
    # case is not a malformed live target and must remain installable.  Once
    # the security workflow exists, however, its target edge is mandatory and
    # every zero/multiple state below fails loud.
    if not workflows:
        return WorkflowTransition, None
    if len(workflows) != 1:
        raise RuntimeError(
            f'PLS-682 expected exactly one {WORKFLOW_SLUG!r} workflow; '
            f'found {len(workflows)}'
        )
    transitions = list(WorkflowTransition.objects.filter(
        workflow_id=workflows[0].pk,
        from_status=FROM_STATUS,
        to_status=TO_STATUS,
    ).only('pk', 'validators'))
    if len(transitions) != 1:
        raise RuntimeError(
            'PLS-682 expected exactly one security-finding '
            f'{FROM_STATUS!r} -> {TO_STATUS!r} transition; '
            f'found {len(transitions)}'
        )
    return WorkflowTransition, transitions[0]


def _validated_validators(transition):
    validators = transition.validators
    if not isinstance(validators, list):
        raise RuntimeError(
            f'PLS-682 target transition {transition.pk} validators must be a list'
        )
    malformed = [
        index for index, validator in enumerate(validators)
        if not isinstance(validator, dict)
        or not isinstance(validator.get('type'), str)
        or not validator.get('type').strip()
    ]
    if malformed:
        raise RuntimeError(
            f'PLS-682 target transition {transition.pk} has malformed '
            f'validator entries at indexes {malformed}'
        )
    return validators


def activate_deployment_evidence_gate(apps, schema_editor):
    WorkflowTransition, transition = _target_transition(apps)
    if transition is None:
        return
    validators = _validated_validators(transition)
    gate_count = sum(
        validator.get('type') == GATE_TYPE for validator in validators
    )
    if gate_count:
        raise RuntimeError(
            f'PLS-682 target transition {transition.pk} already contains '
            f'{gate_count} deployment evidence gate(s)'
        )
    WorkflowTransition.objects.filter(pk=transition.pk).update(
        validators=[*validators, GATE],
    )


def remove_deployment_evidence_gate(apps, schema_editor):
    WorkflowTransition, transition = _target_transition(apps)
    if transition is None:
        return
    validators = _validated_validators(transition)
    gate_indexes = [
        index for index, validator in enumerate(validators)
        if validator.get('type') == GATE_TYPE
    ]
    if len(gate_indexes) != 1:
        raise RuntimeError(
            f'PLS-682 reverse expected exactly one deployment evidence gate '
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
        ('tasks', '0157_alter_externalwaitauthority_branch'),
    ]

    operations = [
        migrations.RunPython(
            activate_deployment_evidence_gate,
            remove_deployment_evidence_gate,
        ),
    ]
