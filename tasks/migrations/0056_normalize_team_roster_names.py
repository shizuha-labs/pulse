"""
Normalize existing TeamRoster.team_name values to canonical form.

PLAT-355 aoi P4: the TeamRoster.save() override now enforces canonical names on
all new/updated rows, but existing rows may have been inserted with aliases like
'Engineering' or 'Admin Ops'. This migration canonicalises them so the unique=True
constraint (case-sensitive on most DB backends) doesn't allow two rows that map to
the same canonical name.

If a canonical-equivalent already exists we keep the canonical row and delete the
alias row (insert-if-absent semantics in 0055 means there should never be both, but
we handle it defensively).
"""
from django.db import migrations


_ALIASES = {
    'admin_ops': 'admin-ops',
    'ceo-decisions': 'admin-ops',
    'leads': 'admin-ops',
    'triage': 'admin-ops',
    'security-triage': 'security',
}


def _canonical(name):
    norm = (name or '').strip().lower().replace('_', '-').replace(' ', '-')
    return _ALIASES.get(norm, norm)


def normalize_team_names(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    for row in list(TeamRoster.objects.all()):
        canon = _canonical(row.team_name)
        if canon == row.team_name:
            continue
        # Check for an already-canonical duplicate
        dup = TeamRoster.objects.filter(team_name=canon).exclude(pk=row.pk).first()
        if dup:
            # sora P2: one of these rows may be an active operator override while the
            # other is a stale/inactive seed row. Always preserve the ACTIVE one.
            if row.is_active and not dup.is_active:
                # alias row is the live config — nuke the stale canonical, rename alias
                dup.delete()
                row.team_name = canon
                row.save(update_fields=['team_name'])
            else:
                # canonical is active (or both/neither active) — keep canonical, drop alias
                row.delete()
        else:
            row.team_name = canon
            row.save(update_fields=['team_name'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0055_seed_team_roster_from_pod_roster'),
    ]

    operations = [
        migrations.RunPython(normalize_team_names, noop),
    ]
