"""PLS-792: backfill Run Now CAS screen-field declaration.

The Run Now transition's mutation boundary requires expected_cycle_revision
(consumed by manual_start_cycle). Migrations 0037/0077 seeded the Run Now
transition before the CAS field declaration existed, so their rows carry an
empty/default screen_fields list. Backfill those rows so
pulse_get_available_transitions / MCP metadata advertises the exact fields a
client must supply (read the current values from the item's recurring_cycle
snapshot at runtime). This is a one-time data backfill — the runtime backstop
that seeds NEW Run Now transitions already carries the declaration, and the hot
path stays lock-free (HIVE-1153).
"""

from django.db import migrations

RUN_NOW_SCREEN_FIELDS = [
    {'key': 'expected_cycle_revision', 'type': 'integer', 'required': True},
    {'key': 'expected_generation', 'type': 'integer', 'required': False},
    {'key': 'expected_due_at', 'type': 'string', 'required': False},
    {'key': 'expected_completion_watermark', 'type': 'string', 'required': False},
    {'key': 'recurring_event_id', 'type': 'string', 'required': False},
]


def backfill_run_now_screen_fields(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    target = [dict(field) for field in RUN_NOW_SCREEN_FIELDS]
    WorkflowTransition.objects.filter(name='Run Now').exclude(
        screen_fields=target,
    ).update(screen_fields=target)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0158_pls682_activate_deployment_evidence_gate'),
    ]

    operations = [
        migrations.RunPython(backfill_run_now_screen_fields, noop),
    ]
