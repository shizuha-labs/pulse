from django.db import migrations
from django.utils import timezone


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


def normalize_digital_operator_tasks(apps, schema_editor):
    Item = apps.get_model("tasks", "Item")
    Comment = apps.get_model("tasks", "Comment")
    TeamRoster = apps.get_model("tasks", "TeamRoster")

    roster = TeamRoster.objects.filter(
        organization_id=2,
        team_name="ceo-decisions",
        is_active=True,
    ).first()
    if roster is None:
        return

    owner_email = ""
    for tier in roster.tiers or []:
        if isinstance(tier, list) and tier:
            owner_email = str(tier[0] or "").strip().lower()
            if owner_email:
                break
    if not owner_email:
        return

    owner_id = (
        Item.objects.filter(
            project__org_id=2,
            assignee_email__iexact=owner_email,
            assignee_id__isnull=False,
        )
        .values_list("assignee_id", flat=True)
        .first()
    )
    if owner_id is None:
        return

    candidates = Item.objects.filter(
        project__org_id=2,
        assignment_group__in=["admin-ops", "human-ops", "ceo-decisions"],
    ).exclude(status__in=TERMINAL_STATUSES)

    for item in candidates:
        old_group = item.assignment_group or ""
        old_email = item.assignee_email or ""
        if old_group == "ceo-decisions" and old_email.lower() == owner_email:
            continue

        metadata = dict(item.metadata or {})
        history = list(metadata.get("org_local_operator_normalizations") or [])
        history.append({
            "version": "0117",
            "from_group": old_group,
            "from_assignee_email": old_email,
            "to_group": "ceo-decisions",
            "to_assignee_email": owner_email,
            "normalized_at": timezone.now().isoformat(),
        })
        metadata["org_local_operator_normalizations"] = history[-10:]

        item.assignment_group = "ceo-decisions"
        item.assignee_id = owner_id
        item.assignee_email = owner_email
        item.metadata = metadata
        item.save(update_fields=[
            "assignment_group",
            "assignee_id",
            "assignee_email",
            "metadata",
            "updated_at",
        ])
        Comment.objects.create(
            item=item,
            author_id=0,
            author_email="system@pulse",
            content=(
                "Organization-isolation repair (migration 0117): normalized "
                f"legacy operator lane `{old_group or 'none'}` / "
                f"`{old_email or 'unassigned'}` to exact-org Digital LLP "
                f"`ceo-decisions` / `{owner_email}`. No cross-organization "
                "task, team, or agent assignment remains on this item."
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0116_make_typed_escalation_holds_self_repairing"),
    ]

    operations = [
        migrations.RunPython(
            normalize_digital_operator_tasks,
            migrations.RunPython.noop,
        ),
    ]
