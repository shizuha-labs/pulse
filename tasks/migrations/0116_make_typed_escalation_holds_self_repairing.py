from django.db import migrations


VALIDATION_REASONS = {
    "missing_evidence_schema",
    "evidence_keys_invalid",
    "unknown_evidence_schema",
    "request_id_invalid",
    "expected_generation_invalid",
    "secret_material_forbidden",
    "dependency_malformed",
    "evidence_class_invalid",
    "requested_kind_invalid",
    "holder_type_invalid",
    "requested_subject_invalid",
    "holder_ref_invalid",
    "acceptance_check_invalid",
    "evidence_holder_conflict",
    "source_refs_required",
    "source_ref_malformed",
    "source_ref_kind_unknown",
    "source_ref_not_found",
    "source_ref_cross_org",
    "source_ref_not_linked",
    "source_ref_forbidden",
    "technical_action_malformed",
    "technical_action_without_operator_dependency",
    "technical_action_independence_unproven",
    "technical_action_relationship_invalid",
    "technical_action_invalid",
    "technical_service_ref_invalid",
    "technical_independence_evidence_invalid",
    "technical_acceptance_check_invalid",
}

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
}


def _notice(parent_key, reason, expected_generation):
    return f"""\
## Escalation evidence repair required

This is an agent-doable typed-evidence repair, not the holder-controlled
dependency itself. Pulse rejected the parent transition with
`{reason}`. Release this repair blocker, let **{parent_key}** auto-resume, then
retry the operator-bound transition with a fresh UUID and this complete shape:

```json
{{
  "schema": "shizuha.pulse.escalation-evidence.v1",
  "request_id": "<fresh UUID>",
  "expected_generation": {expected_generation},
  "dependency": {{
    "evidence_class": "human_held_source_evidence",
    "requested_kind": "artifact",
    "requested_subject": "<one exact atomic ask>",
    "holder_type": "operator",
    "holder_ref": "<exact holder>",
    "acceptance_check": "<observable completion check>",
    "source_refs": [{{"kind": "task", "id": "{parent_key}"}}]
  }},
  "technical_action": null
}}
```

The dependency object must contain all seven fields shown above, with no extra
keys. `source_refs` is a non-empty list of `{{"kind", "id"}}` objects, not a
list of task-key strings. Do not manually assign an Admin Ops or Human Ops
team: valid holder-controlled evidence routes to this task organization's own
`ceo-decisions` Team.

**Acceptance:** the retry returns `operator_held_dependency` / `routed`, names
the actual child key in its receipt, and assigns that child to the exact-org
`ceo-decisions` owner.

---

"""


def repair_active_validation_holds(apps, schema_editor):
    Disposition = apps.get_model("tasks", "EscalationRoutingDisposition")
    Item = apps.get_model("tasks", "Item")
    ItemLink = apps.get_model("tasks", "ItemLink")

    latest_by_parent = {}
    rows = Disposition.objects.filter(
        classification="mixed_or_unknown_escalation_hold",
        reason_code__in=VALIDATION_REASONS,
    ).order_by("item_id", "-generation")
    for disposition in rows:
        latest_by_parent.setdefault(disposition.item_id, disposition)

    for parent_id, disposition in latest_by_parent.items():
        parent = Item.objects.filter(pk=parent_id).first()
        if parent is None:
            continue
        parent_key = parent.item_key or str(parent.pk)
        child_ids = ItemLink.objects.filter(
            target_item_id=parent_id,
            link_type="blocks",
        ).values_list("source_item_id", flat=True)
        for child in Item.objects.filter(pk__in=child_ids):
            if (child.status or "").strip().lower() in TERMINAL_STATUSES:
                continue
            metadata = dict(child.metadata or {})
            if metadata.get("auto_blocker_parent_id") != parent_id:
                continue
            if metadata.get("escalation_schema_repair") == "v2":
                continue

            reason = disposition.reason_code
            expected_generation = disposition.generation
            metadata.update({
                "escalation_schema_repair": "v2",
                "escalation_validation_reason": reason,
                "escalation_expected_generation": expected_generation,
            })
            prefix = f"[SCHEMA REPAIR: {reason}] "
            if not (child.title or "").startswith(prefix):
                child.title = (prefix + (child.title or ""))[:400]
            if not (child.description or "").startswith(
                "## Escalation evidence repair required"
            ):
                child.description = (
                    _notice(parent_key, reason, expected_generation)
                    + (child.description or "")
                )
            child.metadata = metadata
            child.save(
                update_fields=["title", "description", "metadata", "updated_at"]
            )


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0115_org_local_ceo_decisions_and_schema_hold_repair"),
    ]

    operations = [
        migrations.RunPython(
            repair_active_validation_holds,
            migrations.RunPython.noop,
        ),
    ]
