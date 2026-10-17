"""PLAT-724: seed human-ops TeamRoster row with the operator.

human-ops is the operator's physical-work queue (PLS-74, PLAT-724).
Previously, ceo-decisions/leads/triage aliases folded to admin-ops (CoS),
so operator-gated escalations never reached the operator. This migration
creates the canonical human-ops row so _resolve_team_assignee (with
allow_operator=True from execute_post_functions) can elect the operator.

The row is only created if no active row with a human-ops-canonical name
already exists — safe to run on any env, including those that had an earlier
manual row inserted.
"""
import os

from django.db import migrations

_OPERATOR_EMAIL = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()

_HUMAN_OPS_ALIASES = {'human-ops', 'human_ops'}


def _canonical(name):
    return (name or '').strip().lower().replace('_', '-').replace(' ', '-')


def seed_human_ops(apps, schema_editor):
    if not _OPERATOR_EMAIL:
        return
    TeamRoster = apps.get_model('tasks', 'TeamRoster')

    existing = {
        _canonical(row.team_name)
        for row in TeamRoster.objects.filter(is_active=True)
    }
    if _HUMAN_OPS_ALIASES & existing:
        # Already present — preserve operator-managed row unchanged.
        return

    TeamRoster.objects.get_or_create(
        team_name='human-ops',
        defaults={
            'tiers': [[_OPERATOR_EMAIL]],
            'junior_emails': [],
            'is_active': True,
            'note': (
                'Operator-only queue (PLS-74 / PLAT-724). '
                'Receives billing, legal, privacy, org-creds, and physical-hardware escalations. '
                'Seeded by migration 0062; managed by the operator.'
            ),
            'updated_by': 'migration:0062',
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0061_repair_assignment_identity_drift'),
    ]

    operations = [
        migrations.RunPython(seed_human_ops, noop),
    ]
