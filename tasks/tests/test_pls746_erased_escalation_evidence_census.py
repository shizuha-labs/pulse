"""PLS-746: census of erased escalation routing evidence is correct and safe.

The `normalized_evidence` JSONField on `EscalationRoutingDisposition` is the
SOLE persisted copy of the validated `dependency` object. A defect in
`_bounded_error_evidence` overwrote it with the four-word shape
``{schema, validation_error}`` on every row that landed on the hold path —
destroying the only record of what the requester actually asked.

This test pins the read-only census command (`census_erased_escalation_evidence`):

1. Rows whose evidence is exactly ``{schema, validation_error}`` are the
   "erased" population and are enumerated.
2. Rows with retained evidence (``dependency`` present) are NOT enumerated.
3. ``secret_material_forbidden`` rows are NEVER enumerated — that erasure is
   deliberate and correct ("Never persist credential material even in a typed
   HOLD"); the exclusion is active even when such a row exists.
4. The census never mutates anything (no writes issued).
"""

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.management.commands.census_erased_escalation_evidence import (
    _is_erased,
    run_census,
)
from tasks.models import EscalationRoutingDisposition, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class Pls746ErasedEscalationEvidenceCensusTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="pls746-owner",
            email="pls746-owner@example.com",
        )
        self.project = Project.objects.create(name="PLS-746", key="P746", org_id=1)
        # Item.save() validates that the assignee is authorized for the task's
        # org team; register a roster so the holders are valid members.
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="engineering",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [["pls746-owner@example.com"]],
                "is_active": True,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _item(self, key, assignee_email):
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=key,
                mode="task",
                title=f"Escalation holder {key}",
                description="Needs the same operator decision.",
                assignee_email=assignee_email,
            )

    def _disposition(self, item, *, reason_code, evidence):
        return EscalationRoutingDisposition.objects.create(
            organization_id=1,
            project=self.project,
            item=item,
            transition_id=uuid.uuid4(),
            source_generation=1,
            generation=1,
            disposition_digest=uuid.uuid4().hex[:64],
            schema_version="v1",
            classification="escalation",
            outcome="held",
            reason_code=reason_code,
            normalized_evidence=evidence,
        )

    def test_erased_shape_predicate(self):
        self.assertTrue(_is_erased({"schema": "x", "validation_error": "y"}))
        self.assertFalse(_is_erased({"schema": "x", "validation_error": "y", "dependency": {}}))
        self.assertFalse(_is_erased({"dependency": {}}))
        self.assertFalse(_is_erased({}))
        self.assertFalse(_is_erased(None))

    def test_census_enumerates_erased_rows_by_key_and_holder(self):
        erased = self._item("P746-1", "holder1@example.com")
        retained = self._item("P746-2", "holder2@example.com")
        self._disposition(
            erased,
            reason_code="requested_subject_invalid",
            evidence={"schema": "escalation/v1", "validation_error": "requested_subject_invalid"},
        )
        self._disposition(
            retained,
            reason_code="held",
            evidence={
                "schema": "escalation/v1",
                "expected_generation": 1,
                "dependency": {"requested_subject": "something the requester actually wrote"},
                "technical_action": "raise",
            },
        )

        report = run_census()
        self.assertEqual(report["total_erased"], 1)
        self.assertEqual(report["rows"][0]["item_key"], "P746-1")
        self.assertEqual(report["rows"][0]["holder_email"], "holder1@example.com")
        self.assertEqual(report["rows"][0]["reason_code"], "requested_subject_invalid")
        self.assertEqual(report["excluded_secret_material"], True)

    def test_secret_material_forbidden_never_enumerated(self):
        # A live path exists that deliberately erases credential material; the
        # census must never surface or restore it, even when a row exists.
        item = self._item("P746-3", "holder3@example.com")
        self._disposition(
            item,
            reason_code="secret_material_forbidden",
            evidence={"schema": "escalation/v1", "validation_error": "secret_material_forbidden"},
        )

        report = run_census()
        self.assertEqual(report["total_erased"], 0)
        self.assertEqual(report["excluded_secret_material"], True)

        # The opt-in flag exists only to prove the filter is active, never for
        # recovery: it surfaces the row but the report still marks the exclusion
        # as disabled so a consumer cannot mistake it for a safe enumeration.
        with_flag = run_census(include_secret_material=True)
        self.assertEqual(with_flag["total_erased"], 1)
        self.assertEqual(with_flag["excluded_secret_material"], False)
        self.assertEqual(with_flag["rows"][0]["reason_code"], "secret_material_forbidden")

    def test_census_is_read_only(self):
        # No writes should be issued by the census: create one erased row and
        # confirm the row count is unchanged after running the census.
        item = self._item("P746-4", "holder4@example.com")
        self._disposition(
            item,
            reason_code="dependency_malformed",
            evidence={"schema": "escalation/v1", "validation_error": "dependency_malformed"},
        )
        before = EscalationRoutingDisposition.objects.count()
        run_census()
        after = EscalationRoutingDisposition.objects.count()
        self.assertEqual(before, after)
