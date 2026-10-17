from django.db import migrations


PLATFORM_AGENT_TEAMS = (
    'architecture', 'design', 'devops', 'documentation', 'engineering', 'eval',
    'merge', 'product', 'qa', 'research-analytics', 'review', 'security',
)


def enable_corrected_platform_wip(apps, schema_editor):
    """Enable PLS-509 only after the blocked-status classifier is corrected.

    This is intentionally idempotent.  Production temporarily disabled the two
    affected rosters as a reversible stop-bleed before the first overload sweep;
    other environments may still have the 0107 values enabled.
    """
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        team_name__in=PLATFORM_AGENT_TEAMS,
        is_active=True,
    ).update(wip_enforced=True, active_wip_cap=1, ready_wip_cap=5)


def disable_platform_wip(apps, schema_editor):
    TeamRoster = apps.get_model('tasks', 'TeamRoster')
    TeamRoster.objects.filter(
        organization_slug='shizuha',
        team_name__in=PLATFORM_AGENT_TEAMS,
    ).update(wip_enforced=False)


class Migration(migrations.Migration):
    dependencies = [('tasks', '0107_pls509_atomic_wip_admission')]

    operations = [
        migrations.RunPython(enable_corrected_platform_wip, disable_platform_wip),
    ]
