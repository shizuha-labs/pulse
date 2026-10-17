"""
Append PLS-4 junior agents as deepest failover tiers in DB-backed rosters.

PLAT-355 made TeamRoster rows authoritative over code defaults. Fresh installs
receive the PLS-4 junior tiers from the updated 0055 seed snapshot; existing
environments already have active review/documentation rows, so this migration
adds the juniors to those DB rows without disturbing the senior tiers.
"""
from django.db import migrations


_JUNIOR_TIERS = {
    'review': 'revi-jr@shizuha.com',
    'documentation': 'yuki-jr@shizuha.com',
}


def _tiers_without_email(tiers, email):
    target = email.strip().lower()
    cleaned = []
    for tier in tiers or []:
        if not isinstance(tier, list):
            tier = [tier]
        kept = [e for e in tier if str(e).strip().lower() != target]
        if kept:
            cleaned.append(kept)
    return cleaned


def append_junior_tiers(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for team_name, junior_email in _JUNIOR_TIERS.items():
        row = TeamRoster.objects.filter(team_name=team_name, is_active=True).first()
        if row is None:
            row = TeamRoster(team_name=team_name, tiers=[], is_active=True)
        tiers = _tiers_without_email(row.tiers or [], junior_email)
        tiers.append([junior_email])
        row.tiers = tiers
        row.note = ((row.note or '').rstrip() + '\nPLS-4: junior deepest failover tier seeded.').strip()
        row.updated_by = 'migration:0057-pls4'
        row.save()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0056_normalize_team_roster_names'),
        ('tasks', '0052_pls25_mark_umbrella_anchors'),
    ]

    operations = [
        migrations.RunPython(append_junior_tiers, noop),
    ]
