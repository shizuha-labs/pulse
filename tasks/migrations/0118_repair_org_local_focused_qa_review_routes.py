from django.db import migrations


def repair_digital_focused_qa_review_routes(apps, schema_editor):
    Item = apps.get_model("tasks", "Item")
    Comment = apps.get_model("tasks", "Comment")
    TeamRoster = apps.get_model("tasks", "TeamRoster")

    candidates = Item.objects.filter(
        project__org_id=2,
        source="focused-qa-automation",
        status="in_review",
        metadata__focused_qa_routing_mode="origin-team-fallback",
        assignment_group="review",
    )

    for item in candidates:
        metadata = item.metadata or {}
        recorded_group = str(
            metadata.get("focused_qa_assignment_group") or ""
        ).strip().lower()
        if not recorded_group:
            continue

        roster = TeamRoster.objects.filter(
            organization_id=2,
            team_name=recorded_group,
            is_active=True,
        ).first()
        if roster is None:
            continue

        members = []
        for tier in roster.tiers or []:
            if isinstance(tier, list):
                members.extend(
                    str(email or "").strip().lower()
                    for email in tier
                    if str(email or "").strip()
                )
        if not members:
            continue

        implementer = metadata.get("implementer") or {}
        owner_email = str(implementer.get("email") or "").strip().lower()
        owner_id = implementer.get("id")
        if owner_email not in members:
            owner_email = members[0]
            owner_id = None
        if owner_id is None:
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
            continue

        item.assignment_group = recorded_group
        item.assignee_id = owner_id
        item.assignee_email = owner_email
        item.save(update_fields=[
            "assignment_group",
            "assignee_id",
            "assignee_email",
            "updated_at",
        ])
        Comment.objects.create(
            item=item,
            author_id=0,
            author_email="system@pulse",
            content=(
                "Organization-isolation repair (migration 0118): restored "
                f"this focused-QA approval lane from the nonexistent `review` "
                f"seat to its recorded exact-org `{recorded_group}` team / "
                f"`{owner_email}`. The documented QA independence exception "
                "remains explicit."
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0117_normalize_digital_operator_tasks_to_org_local_team"),
    ]

    operations = [
        migrations.RunPython(
            repair_digital_focused_qa_review_routes,
            migrations.RunPython.noop,
        ),
    ]
