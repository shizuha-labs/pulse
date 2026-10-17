"""PLS-746 read-only census of erased escalation routing evidence.

The `normalized_evidence` JSONField on `EscalationRoutingDisposition` is the
SOLE persisted copy of the validated `dependency` object. A defect in
`_bounded_error_evidence` (escalation_routing.py) overwrote it with the
four-word shape ``{schema, validation_error}`` on every row that landed on the
hold path — destroying the only record of what the requester actually asked.

This command enumerates those rows (the "erased" population) by task key and
current holder. It is strictly read-only: no schema change, no mutation, no
recovery. It also hard-excludes the ``secret_material_forbidden`` reason code —
that erasure is deliberate and correct ("Never persist credential material even
in a typed HOLD"), and no enumeration or recovery may ever surface or restore
credential material into a Pulse JSONField.

Usage:
    python manage.py census_erased_escalation_evidence
    python manage.py census_erased_escalation_evidence --json
    python manage.py census_erased_escalation_evidence --include-secret-material
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db.models import F, Q

from tasks.models import EscalationRoutingDisposition

# The erased shape produced by `_bounded_error_evidence`: exactly
# {"schema": ..., "validation_error": <reason>}. A retained disposition has
# additional keys (dependency, expected_generation, technical_action).
_ERASED_KEYS = {"schema", "validation_error"}


def _is_erased(evidence) -> bool:
    if not isinstance(evidence, dict):
        return False
    return set(evidence.keys()) == _ERASED_KEYS


def run_census(*, include_secret_material: bool = False) -> dict:
    qs = EscalationRoutingDisposition.objects.select_related("item", "item__project").all()
    if not include_secret_material:
        qs = qs.exclude(reason_code="secret_material_forbidden")

    rows = []
    for d in qs.iterator(chunk_size=500):
        if not _is_erased(d.normalized_evidence):
            continue
        item = d.item
        rows.append(
            {
                "disposition_id": str(d.id),
                "item_key": item.item_key if item else None,
                "item_title": item.title if item else None,
                "item_status": item.status if item else None,
                "holder_email": item.assignee_email if item else None,
                "holder_id": item.assignee_id if item else None,
                "project": item.project.name if item and item.project else None,
                "reason_code": d.reason_code,
                "classification": d.classification,
                "outcome": d.outcome,
                "source_generation": d.source_generation,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "evidence": d.normalized_evidence,
            }
        )

    rows.sort(key=lambda r: (r["item_key"] or "", r["created_at"] or ""))
    return {
        "schema": "pls746-erased-escalation-evidence-v1",
        "total_erased": len(rows),
        "excluded_secret_material": not include_secret_material,
        "rows": rows,
    }


class Command(BaseCommand):
    help = (
        "PLS-746 read-only census of EscalationRoutingDisposition rows whose "
        "normalized_evidence was erased to {schema, validation_error} — by task "
        "key and current holder. No mutation; excludes secret_material_forbidden."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the full structured report as JSON.",
        )
        parser.add_argument(
            "--include-secret-material",
            action="store_true",
            help=(
                "DANGER: include secret_material_forbidden rows in the census. "
                "Never use this for recovery; it exists only to prove the "
                "exclusion filter is active (expected: 0 rows today)."
            ),
        )

    def handle(self, *args, **options):
        report = run_census(include_secret_material=options["include_secret_material"])
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return

        self.stdout.write(
            f"PLS-746 census  schema={report['schema']}  "
            f"erased={report['total_erased']}  "
            f"excluded_secret_material={report['excluded_secret_material']}"
        )
        for row in report["rows"]:
            holder = row["holder_email"] or f"id:{row['holder_id']}" or "UNASSIGNED"
            self.stdout.write(
                f"  {row['item_key'] or '<no-key>':<14} holder={holder:<32} "
                f"status={row['item_status'] or '-':<14} reason={row['reason_code']} "
                f"created={row['created_at']}"
            )
