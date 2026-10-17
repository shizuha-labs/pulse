"""
PLAT-431: Sync review and merge TeamRoster rows to the canonical state that was
hardcoded in _POD_ROSTER but had diverged from the DB seed rows in 0055/0057.

Two discrepancies to fix (detected by comparing _POD_ROSTER at commit time with
the 0055 _SEED_ROSTER):

review:
  DB after 0055+0057: [[revi, kei], [rei, ren], [revi-jr]]   (rei/ren split across tiers)
  Canonical          : [[revi, kei, rei], [ren], [revi-jr]]   (rei promoted to tier-1)
  Why: operator directive 2026-06-14 — each critical team must have active
  claude+codex redundancy; rei(claude) was promoted from failover to active tier-1.

merge:
  DB after 0055:  [[aoi], [nao]]   (two-tier with nao as failover)
  Canonical:      [[aoi, sora]]    (flat single tier; sora added, nao removed)
  Why: operator directive 2026-06-14 — merge must have claude+codex redundancy.
  sora holds the same kai2574 WRITE PAT as aoi; nao is a PM without merge creds.

This migration also removes PLAT-431's dependency on the hardcoded _POD_ROSTER
by ensuring the DB reflects exactly the roster that was in code, so emptying
_POD_ROSTER causes no routing regression on fresh deploys or existing envs.

Idempotent: updates are set-based so re-running is safe.
"""
from django.db import migrations


# Final canonical tiers for the two diverged teams.
_SYNC = {
    'review': [['revi@shizuha.com', 'kei@shizuha.com', 'rei@shizuha.com'], ['ren@shizuha.com'], ['revi-jr@shizuha.com']],
    'merge': [['aoi@shizuha.com', 'sora@shizuha.com']],
}


def sync_roster_rows(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for team_name, tiers in _SYNC.items():
        # PLAT-431 (cycle 3): migration 0055 already INSERTED `review` and `merge`
        # rows with the OLD tier layout (rei un-promoted, nao in merge, no sora), so
        # a get_or_create here is a silent no-op and the canonical sync never lands.
        # update_or_create the specific slugs to write the canonical tiers. This is a
        # migration→migration correction that runs once at deploy, before any operator
        # edits exist — it is NOT clobbering live operator state (operators edit via
        # admin only after deploy; this just fixes the stale 0055 seed).
        TeamRoster.objects.update_or_create(
            team_name=team_name,
            defaults={
                'tiers': tiers,
                'is_active': True,
                'note': 'Synced to canonical tiers by migration 0058 (PLAT-431)',
                'updated_by': 'migration:0058-plat431',
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0057_pls4_junior_failover_tiers'),
    ]

    operations = [
        migrations.RunPython(sync_roster_rows, noop),
    ]
