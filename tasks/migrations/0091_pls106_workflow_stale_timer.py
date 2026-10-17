"""Move stale escalation visibility policy into workflow transition config.

PLS-106 removes the company-specific signal-4 matcher from ``tasks.tasks``.
The Celery sweep now provides only a generic stale-status timer mechanism; this
migration seeds the Shizuha policy as workflow data.
"""
from django.db import migrations


TIMER_NAME = 'Admin Ops stale triage SLA marker'
TRIGGER = {
    'type': 'stale_status_timer',
    'assignment_groups': ['admin-ops'],
    'after_hours_env': 'PULSE_OPERATOR_GATED_SLA_HOURS',
    'after_hours': 12,
    'age_field': 'created_at',
    'metadata_key': 'admin_ops_escalation_sla_marked_at',
    'match_any': [
        {
            'tags_any': [
                'decision-required',
                'escalation',
                'blocker',
                'operator-gated',
                'needs-operator',
            ],
        },
        {
            'statuses': ['blocked'],
            'title_markers_any': [
                '[operator]',
                '[ceo action]',
                'ceo action',
                'grant ',
                'ssh access',
                ' oauth',
                'credential',
                'sign-off',
                'sign off',
                'billing',
            ],
            'title_markers_not_any': ['rotate', 'revoke'],
        },
    ],
}
COMMENT = {
    'type': 'add_comment',
    'body': (
        '[admin-ops-escalation-sla] Human-gated-looking work has aged '
        '{{timer_age_hours}}h > {{timer_threshold_hours}}h on Admin Ops. '
        'CoS/backup must validate it, route credential/infra gaps to DevOps/Hive, '
        'or create/link a crisp human blocker only for a genuine operator-only '
        'decision. No direct CEO assignment was made.'
    ),
}


def seed_stale_timer(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.all():
        transition, _created = WorkflowTransition.objects.get_or_create(
            workflow=workflow,
            name=TIMER_NAME,
            from_status='__timer__',
            to_status='__timer__',
            defaults={
                'description': (
                    'Automation-only stale timer. The engine matches the trigger '
                    'post-function and executes the remaining post-functions when '
                    'an item exceeds the configured SLA.'
                ),
                'position': 995,
                'button_style': 'warning',
                'conditions': {'automation_only': True},
                'post_functions': [TRIGGER, COMMENT],
                'knowledge_articles': [],
                'is_global': False,
            },
        )
        transition.description = (
            'Automation-only stale timer. The engine matches the trigger '
            'post-function and executes the remaining post-functions when '
            'an item exceeds the configured SLA.'
        )
        transition.position = 995
        transition.button_style = 'warning'
        transition.conditions = {'automation_only': True}
        transition.post_functions = [TRIGGER, COMMENT]
        transition.knowledge_articles = []
        transition.is_global = False
        transition.save(update_fields=[
            'description', 'position', 'button_style', 'conditions',
            'post_functions', 'knowledge_articles', 'is_global', 'updated_at',
        ])


def unseed_stale_timer(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    WorkflowTransition.objects.filter(name=TIMER_NAME, from_status='__timer__').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0090_merge_20260706_pls373_orig73'),
    ]

    operations = [
        migrations.RunPython(seed_stale_timer, unseed_stale_timer),
    ]
