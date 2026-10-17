import os

from django.db import migrations


FORWARD_CANONICAL_TEAM_SQL = """
CREATE OR REPLACE FUNCTION tasks_canonical_team_key(p_team text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
        WHEN 'personal-accounting' THEN 'accounting'
        WHEN 'digital-accounting' THEN 'accounting'
        WHEN 'digital-quant' THEN 'quant'
        WHEN 'securities-engineering' THEN 'engineering'
        WHEN 'trading-engineering' THEN 'engineering'
        WHEN 'admin_ops' THEN 'admin-ops'
        WHEN 'leads' THEN 'human-ops'
        WHEN 'triage' THEN 'human-ops'
        WHEN 'security-triage' THEN 'security'
        WHEN 'platform-ops' THEN 'devops'
        WHEN 'analytics' THEN 'research-analytics'
        WHEN 'research' THEN 'research-analytics'
        WHEN 'governance' THEN 'admin-ops'
        WHEN 'program-management' THEN 'admin-ops'
        ELSE replace(replace(lower(trim(COALESCE(p_team, ''))), '_', '-'), ' ', '-')
    END
$$;
"""


REVERSE_CANONICAL_TEAM_SQL = FORWARD_CANONICAL_TEAM_SQL.replace(
    "        WHEN 'admin_ops' THEN 'admin-ops'\n",
    "        WHEN 'admin_ops' THEN 'admin-ops'\n"
    "        WHEN 'ceo-decisions' THEN 'human-ops'\n",
)


SCHEMA_REPAIR_NOTICE = """\
## Escalation evidence schema repair required

This blocker is agent-doable schema repair, not the holder-controlled action
described below. The parent escalation was held because
`screen_data.escalation_evidence` did not use
`shizuha.pulse.escalation-evidence.v1`.

Complete this repair by:

1. releasing this schema-repair blocker so the parent auto-resumes;
2. re-running the operator-bound transition with a fresh `request_id`, the
   latest receipt `generation` as `expected_generation`, and all five top-level
   fields: `schema`, `request_id`, `expected_generation`, `dependency`, and
   `technical_action`;
3. classifying holder-controlled source files as
   `human_held_source_evidence` / `artifact` / `operator`, with the parent task
   itself in `dependency.source_refs`; use `technical_action: null` when no
   independent technical work exists.

Do not retry a manual `admin-ops` or `human-ops` assignment. A validated
operator-held dependency routes to the task organization's own
`ceo-decisions` Team.

---

"""


def seed_org_local_decision_team_and_repair_schema_holds(apps, schema_editor):
    TeamRoster = apps.get_model("tasks", "TeamRoster")
    Item = apps.get_model("tasks", "Item")
    ItemLink = apps.get_model("tasks", "ItemLink")
    Disposition = apps.get_model("tasks", "EscalationRoutingDisposition")

    operator_email = os.environ.get('PULSE_OPERATOR_EMAIL', '').strip().lower()
    if operator_email:
        TeamRoster.objects.update_or_create(
            organization_slug="shizuha-digital",
            team_name="ceo-decisions",
            defaults={
                "organization_id": 2,
                "tiers": [[operator_email]],
                "junior_emails": [],
                "is_active": True,
                "note": (
                    "Organization-local human decision and holder-controlled "
                    "evidence lane; mirrors Admin team 152."
                ),
                "updated_by": "migration-0115",
            },
        )

    parent_ids = list(
        Disposition.objects.filter(
            classification="mixed_or_unknown_escalation_hold",
            reason_code="missing_evidence_schema",
        ).values_list("item_id", flat=True)
    )
    if not parent_ids:
        return

    child_ids = ItemLink.objects.filter(
        target_item_id__in=parent_ids,
        link_type="blocks",
    ).values_list("source_item_id", flat=True)
    for child in Item.objects.filter(pk__in=child_ids):
        metadata = dict(child.metadata or {})
        if not metadata.get("auto_blocker_parent_id"):
            continue
        if metadata.get("escalation_schema_repair") == "v1":
            continue
        metadata["escalation_schema_repair"] = "v1"
        child.metadata = metadata
        child.title = (
            f"[SCHEMA REPAIR] {child.title}"
            if not child.title.startswith("[SCHEMA REPAIR]")
            else child.title
        )[:400]
        if not child.description.startswith(
            "## Escalation evidence schema repair required"
        ):
            child.description = SCHEMA_REPAIR_NOTICE + child.description
        child.save(
            update_fields=["title", "description", "metadata", "updated_at"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0114_canonicalize_org_safe_condition_capsules"),
    ]

    operations = [
        migrations.RunSQL(
            FORWARD_CANONICAL_TEAM_SQL,
            REVERSE_CANONICAL_TEAM_SQL,
        ),
        migrations.RunPython(
            seed_org_local_decision_team_and_repair_schema_holds,
            migrations.RunPython.noop,
        ),
    ]
