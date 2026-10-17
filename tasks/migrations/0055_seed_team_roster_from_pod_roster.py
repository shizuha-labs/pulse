"""
Seed TeamRoster rows from the code-level _POD_ROSTER (idempotent, insert-if-absent).

Operator directive 2026-06-12: routing config lives in the DB, never baked into the
image.  PR #72/#88 added the model + overlay reader; this migration makes a fresh
deploy/migration-replay IMMEDIATELY authoritative — so the image-stale regression
class is killed from the first migrate, not just on prod environments that were
manually seeded.

Insert-if-absent semantics: existing rows (e.g. the prod rows seeded by the
amd64-dbroster image) are NOT touched, so no live config is overwritten.
"""
from django.db import migrations


# Snapshot of WorkflowService._POD_ROSTER at the time of this migration.
# Stored inline (not imported) to stay stable as the live dict evolves.
_SEED_ROSTER = {
    'program-management': [['shizuha@shizuha.com'], ['shion@shizuha.com']],
    # PLS-1164 fix: these four entries were FLAT lists of member strings —
    # not tier-nested like every other entry. Nested through ``list(tier)``
    # at seed time they became the exact char-splat corruption class the
    # 2026-06-15 incident guard (validate_roster_structure) now rejects,
    # breaking routing on every fresh environment. Tier-nest them properly.
    'engineering': [['ryo@shizuha.com', 'jun@shizuha.com', 'kai@shizuha.com', 'sara@shizuha.com']],
    'devops': [['ichi@shizuha.com', 'ni@shizuha.com']],
    'architecture': [['aoi@shizuha.com', 'sora@shizuha.com']],
    'governance': [['shizuha@shizuha.com']],
    'review': [['revi@shizuha.com', 'kei@shizuha.com'], ['rei@shizuha.com', 'ren@shizuha.com'], ['revi-jr@shizuha.com']],
    'merge': [['aoi@shizuha.com'], ['nao@shizuha.com']],
    'qa': [['zen@shizuha.com'], ['mika@shizuha.com']],
    'security': [['akira@shizuha.com'], ['ren@shizuha.com']],
    'documentation': [['haru@shizuha.com', 'yuki@shizuha.com'], ['scout@shizuha.com'], ['yuki-jr@shizuha.com']],
    'analytics': [['tomo@shizuha.com'], ['hana@shizuha.com']],
    'product': [['nao@shizuha.com'], ['shion@shizuha.com']],
    # PLAT-355 aoi P5: social-media and admin-ops are NOT in _POD_ROSTER — seeding
    # them here caused fresh-deploy vs migrated-prod divergence (PLS-16 / aoi finding).
    # These teams' DB rows are managed per-env by operators, not seeded from code.
}


def _canonical(name):
    """Mirror WorkflowService._canonical_team_name without importing it.
    Inline to stay stable as the live service evolves; migration logic must not
    import from the app (schema state at migration time may differ from HEAD).
    """
    ALIASES = {
        'admin_ops': 'admin-ops',
        'ceo-decisions': 'admin-ops',
        'leads': 'admin-ops',
        'triage': 'admin-ops',
        'security-triage': 'security',
    }
    norm = (name or '').strip().lower().replace('_', '-').replace(' ', '-')
    return ALIASES.get(norm, norm)


def seed_team_roster(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')

    # kei P2: build a set of canonical names already present in the DB so we
    # don't insert a second active row for a pre-existing alias-spelling like
    # 'Engineering' or 'Admin Ops' (get_or_create uses exact match, so it would
    # insert a new row rather than finding the canonical-equivalent one, leaving
    # two active rows with order-dependent overlay behaviour).
    existing_canonical = {
        _canonical(row.team_name)
        for row in TeamRoster.objects.filter(is_active=True)
    }

    for team_name, tiers in _SEED_ROSTER.items():
        if _canonical(team_name) in existing_canonical:
            # A canonical-equivalent active row already exists — preserve it unchanged.
            continue
        TeamRoster.objects.get_or_create(
            team_name=team_name,
            defaults={
                'tiers': tiers,
                'is_active': True,
                'note': 'Seeded from _POD_ROSTER by migration 0055',
                'updated_by': 'migration:0055',
            },
        )


def noop(apps, schema_editor):
    # Reverse is a no-op: don't delete rows that may have been modified on prod.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0054_team_roster'),
    ]

    operations = [
        migrations.RunPython(seed_team_roster, noop),
    ]
