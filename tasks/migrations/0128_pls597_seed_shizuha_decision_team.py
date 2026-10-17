import os

from django.db import migrations


OWNER_EMAIL = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()


def seed_shizuha_decision_team(apps, schema_editor):
    if not OWNER_EMAIL:
        return
    TeamRoster = apps.get_model("tasks", "TeamRoster")

    TeamRoster.objects.update_or_create(
        organization_id=1,
        team_name="ceo-decisions",
        defaults={
            "organization_slug": "shizuha",
            "tiers": [[OWNER_EMAIL]],
            "junior_emails": [],
            "is_active": True,
            "note": (
                "Exact-org holder-controlled evidence and decision lane; "
                "materialized by PLS-597 without Admin/global fallback."
            ),
            "updated_by": "migration-0128-pls597",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0127_seed_trading_decision_team"),
    ]

    operations = [
        migrations.RunPython(
            seed_shizuha_decision_team,
            migrations.RunPython.noop,
        ),
    ]
