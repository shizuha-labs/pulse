"""
Keep critical routing pods from becoming single-agent failure domains.

The live 2026-06-26 roster had architecture/merge/security as single-member
teams. When those members were disabled/down, sweep_rebalance_pods correctly had
no safe recipient and left active work stranded. Seed explicit failover tiers so
the normal availability-driven routing and rebalance paths can evacuate work.
"""
from django.db import migrations


_FAILOVER_TIERS = {
    # Architecture reviews/design decisions should stay with Sora when healthy;
    # Shizuha is the coordinator fallback and Ni is the platform-maintainer floor.
    'architecture': [
        ['sora@shizuha.com'],
        ['shizuha@shizuha.com'],
        ['ni@shizuha.com'],
    ],
    # Merge still requires the explicit merge-capability allow-list in
    # WorkflowService; this roster only defines failover order.
    'merge': [
        ['sora@shizuha.com'],
        ['kei@shizuha.com'],
    ],
    # Security should prefer Akira, then fall back to DevOps/QA instead of going
    # dark when the security specialist is stopped.
    'security': [
        ['akira@shizuha.com'],
        ['ichi@shizuha.com'],
        ['kei@shizuha.com'],
    ],
}


def seed_failover_tiers(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for team_name, tiers in _FAILOVER_TIERS.items():
        TeamRoster.objects.update_or_create(
            team_name=team_name,
            defaults={
                'tiers': tiers,
                'is_active': True,
                'note': (
                    'Critical team failover tiers seeded by migration 0065; '
                    'prevents active work stranding when the primary is down.'
                ),
                'updated_by': 'migration:0065-critical-failover',
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0064_unique_active_qa_cadence_source'),
    ]

    operations = [
        migrations.RunPython(seed_failover_tiers, noop),
    ]
