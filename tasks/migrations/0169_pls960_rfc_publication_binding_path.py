"""PLS-960: add the same-carrier RFC publication preparation action.

``Accept — Capacity-Held`` deliberately consumes a PLS-482
``PublicationBinding``.  The RFC workflow previously had no edge that could
produce one without first leaving ``rfc_design``.  This same-status action runs
the existing Wiki-CAS publication saga and keeps the design carrier in place so
Architecture and Security can bind their verdicts to the minted review round.
"""

from django.db import migrations


TRANSITION_NAME = 'Prepare Publication Binding'


def seed_rfc_publication_path(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug='feature-request-rfc').first()
    if workflow is None:
        return

    WorkflowTransition.objects.update_or_create(
        workflow=workflow,
        from_status='rfc_design',
        name=TRANSITION_NAME,
        defaults={
            'to_status': 'rfc_design',
            'description': (
                'Freeze the exact HLD/ADR page-version/section-hash tuples via '
                'the PLS-482 Wiki CAS and mint one immutable PublicationBinding '
                'on this RFC carrier. The supplied review_round_id becomes the '
                'round Architecture and Security verdict comments must name.'
            ),
            'position': 24,
            'button_style': 'primary',
            'allowed_roles': ['architecture'],
            'validators': [],
            'screen_fields': [
                {
                    'field': 'publication_binding',
                    'label': 'Publication binding (JSON)',
                    'required': True,
                    'help_text': (
                        'Exact operation_key, fresh review_round_id, accepted '
                        'publication_result, and ordered Wiki page/version/anchor '
                        'section-hash tuples.'
                    ),
                },
            ],
            'post_functions': [{'type': 'publication_handoff'}],
        },
    )


def unseed_rfc_publication_path(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowTransition.objects.filter(
        workflow__slug='feature-request-rfc',
        from_status='rfc_design',
        name=TRANSITION_NAME,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0168_pls1047_package_publish_acceptance_mode'),
    ]

    operations = [
        migrations.RunPython(seed_rfc_publication_path, unseed_rfc_publication_path),
    ]
