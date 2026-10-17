"""PLS-714: ensure every workflow carries the full escalation lane set.

Migrations 0044/0053/0069 seeded Escalate to Human Ops / Raise to Admin Ops /
Escalate to Leads across `Workflow.objects.all()`, but only for workflows that
existed at migration time. Workflows created later (feature-request-rfc by 0072,
channel-message-review, and any fixture/runtime-loaded workflow in a fresh DB)
never received the lanes — a seeding-order gap, not a deliberate omission.

This migration seeds the missing escalation lanes for EVERY workflow present at
migration time (idempotent upsert by workflow+name+from_status+to_status), so a
fresh DB converges to the same shape as production. Escalations target `blocked`
so the PLAT-4215 `create_blocker_task` post-function applies.

Canonical shapes (from migration 0053 + 0100):
- Escalate to Human Ops: per-active-status -> blocked; assign_team human-ops.
- Raise to Admin Ops: global -> blocked; assign_team admin-ops.
- Escalate to Leads: global -> blocked; assign_team ceo-decisions.
"""
from django.db import migrations

ESCALATION_SPECS = {
    'Escalate to Human Ops': {
        'to_status': 'blocked',
        'global': False,
        'team': 'human-ops',
        'description': (
            'LAST RESORT: escalate work that is genuinely impossible for any agent and '
            'needs a human (the operator) to act physically/in person — cables, reseats, '
            'power-cycle, firmware-at-console, physical device actions. Exhaust retry / '
            'fix / restart / wiki / peer / host-sudo first. Routes to human-ops (operator).'
        ),
        'button_style': 'warning',
        'position': 60,
    },
    'Raise to Admin Ops': {
        'to_status': 'blocked',
        'global': True,
        'team': 'admin-ops',
        'description': (
            'Escalate a task that is blocked on something no agent can do to the Admin Ops '
            'team (operator). Auto-assigns to the operator.'
        ),
        'button_style': 'warning',
        'position': 60,
    },
    'Escalate to Leads': {
        'to_status': 'blocked',
        'global': True,
        'team': 'ceo-decisions',
        'description': (
            'Escalate this task to the CEO Decisions team when the next step requires a '
            'CEO-only action (billing, legal, privacy trade-off, org-level credential). '
            'The task is marked blocked and a child task auto-assigned to the CEO Decisions '
            'team lead is expected via pulse_create_blocker_task.'
        ),
        'button_style': 'warning',
        'position': 60,
    },
}


def _post_functions(team):
    return [
        {
            'type': 'create_blocker_task',
            'team': team,
            'workflow': 'simple',
            'priority': 'urgent',
            'source': 'pls714-seed-missing-escalation-lanes',
        },
        {'type': 'assign_team', 'value': team},
        {
            'type': 'send_notification',
            'template': 'status_changed',
            'recipients': ['assignee', 'watchers'],
        },
    ]


def seed_missing_escalation_lanes(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    for workflow in Workflow.objects.all():
        existing = WorkflowTransition.objects.filter(workflow=workflow)
        existing_keys = {(t.name, t.from_status, t.to_status) for t in existing}
        target_statuses = set(
            workflow.workflow_statuses.values_list('status__slug', flat=True)
        )
        active_sources = [
            s for s in target_statuses
            if s != 'blocked' and s not in ('done', 'cancelled', 'deferred')
        ]
        for name, spec in ESCALATION_SPECS.items():
            if spec['global']:
                sources = ['*']
            else:
                sources = [s for s in active_sources if s in target_statuses]
            for source in sources:
                key = (name, source, spec['to_status'])
                if key in existing_keys:
                    continue
                WorkflowTransition.objects.create(
                    workflow=workflow,
                    name=name,
                    from_status=source,
                    to_status=spec['to_status'],
                    description=spec['description'],
                    button_style=spec['button_style'],
                    position=spec['position'],
                    is_global=spec['global'],
                    post_functions=_post_functions(spec['team']),
                )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0172_item_current_state_item_current_state_updated_at'),
    ]

    operations = [
        migrations.RunPython(seed_missing_escalation_lanes, noop),
    ]
