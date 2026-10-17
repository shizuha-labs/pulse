import os

from django.db import migrations
from django.utils import timezone


OWNER_EMAIL = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()


TERMINAL_STATUSES = {
    "done",
    "completed",
    "closed",
    "cancelled",
    "canceled",
    "resolved",
    "rejected",
    "duplicate",
    "wont_fix",
    "merged",
    "failed",
    "expired",
    "deferred",
}


def seed_trading_decision_team(apps, schema_editor):
    if not OWNER_EMAIL:
        return
    Item = apps.get_model("tasks", "Item")
    Comment = apps.get_model("tasks", "Comment")
    TeamRoster = apps.get_model("tasks", "TeamRoster")

    roster, _ = TeamRoster.objects.update_or_create(
        organization_id=3,
        team_name="ceo-decisions",
        defaults={
            "organization_slug": "shizuha-trading",
            "tiers": [[OWNER_EMAIL]],
            "junior_emails": [],
            "is_active": True,
            "note": (
                "Exact-org holder-controlled evidence and decision lane; "
                "materialized by PLAT-4926 without Admin/global fallback."
            ),
            "updated_by": "migration-0127",
        },
    )

    # TRD-26 is the reviewed decision-lane task identified by PLAT-4926.
    # Do not seed a physical human-ops lane or rewrite any unrelated task.
    item = (
        Item.objects.filter(
            project__org_id=3,
            item_key="TRD-26",
            assignment_group="human-ops",
        )
        .exclude(status__in=TERMINAL_STATUSES)
        .first()
    )
    if item is None or item.assignment_group == roster.team_name:
        return

    old_group = item.assignment_group or ""
    metadata = dict(item.metadata or {})
    history = list(metadata.get("org_local_operator_normalizations") or [])
    history.append(
        {
            "version": "0127",
            "from_group": old_group,
            "from_assignee_email": item.assignee_email or "",
            "to_group": roster.team_name,
            "to_assignee_email": item.assignee_email or OWNER_EMAIL,
            "normalized_at": timezone.now().isoformat(),
        }
    )
    metadata["org_local_operator_normalizations"] = history[-10:]

    item.assignment_group = roster.team_name
    item.metadata = metadata
    item.save(update_fields=["assignment_group", "metadata", "updated_at"])
    Comment.objects.create(
        item=item,
        author_id=0,
        author_email="system@pulse",
        content=(
            "Organization-isolation repair (migration 0127): moved this "
            f"operator-held dependency from the unregistered `{old_group or 'none'}` "
            f"lane to exact-org Trading LLP `{roster.team_name}` / "
            f"`{OWNER_EMAIL}`. No Admin/global roster or cross-organization "
            "assignment was used."
        ),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0126_hive1223_project_is_default"),
    ]

    operations = [
        migrations.RunPython(
            seed_trading_decision_team,
            migrations.RunPython.noop,
        ),
    ]
