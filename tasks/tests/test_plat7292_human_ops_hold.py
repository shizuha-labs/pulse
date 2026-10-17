"""PLAT-7292: Escalate to Human Ops must commit when human-ops has no live seat.

Production sequence (PLAT-7291): typed Escalate to Human Ops ran three times
and each returned 503 ``escalation_hold_commit_failed``. Root cause: `_assign_group`
raised ``target_assignee_unresolved`` when `_resolve_team_assignee('human-ops')`
returned (None, None); the fail-loud HOLD retried the same lookup and 503'd.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from tasks.models import (
    EscalationRoutingDisposition,
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


def _evidence(item_key: str) -> dict:
    return {
        "schema": "shizuha.pulse.escalation-evidence.v1",
        "request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "expected_generation": 0,
        "dependency": {
            "evidence_class": "human_physical_action",
            "requested_kind": "physical_action",
            "requested_subject": "reseating i9-ws SFP",
            "holder_type": "physical_operator",
            "holder_ref": "operator@example.test",
            "acceptance_check": "node Ready and flannel up",
            "source_refs": [{"kind": "task", "id": item_key}],
        },
        "technical_action": None,
    }


class Plat7292HumanOpsUnresolvedSeatTests(TransactionTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="plat7292-actor",
            email="plat7292-actor@example.com",
            is_staff=True,
        )
        self.project = Project.objects.create(name="PLAT-7292", key="P7292", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLAT-7292 WF",
            slug="plat-7292-wf",
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=blocked_status, position=1)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Escalate to Human Ops",
            from_status="open",
            to_status="blocked",
            post_functions=[{"type": "assign_team", "value": "human-ops"}],
        )
        for team in ("engineering", "human-ops", "admin-ops"):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team,
                defaults={
                    "organization_slug": "shizuha",
                    "tiers": [[self.actor.email]],
                    "is_active": True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            self.item = Item.objects.create(
                project=self.project,
                item_key="P7292-1",
                mode="task",
                title="i9-ws flannel repair",
                description="physical",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group="engineering",
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def test_human_ops_escalation_commits_group_queued_when_seat_unresolved(self):
        """Two consecutive top-level Escalate to Human Ops calls must commit,
        not 503, when human-ops has no resolvable live member."""
        def _no_human_ops_seat(group, *args, **kwargs):
            if WorkflowService._canonical_team_name(group) == "human-ops":
                return (None, None)
            return (self.actor.id, self.actor.email)

        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=_no_human_ops_seat,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            first = WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={
                    "comment": "physical repair",
                    "escalation_evidence": _evidence(self.item.item_key),
                },
                transition_id=self.transition.id,
            )
            second = WorkflowService.execute_transition(
                first,
                "blocked",
                user=self.actor,
                screen_data={
                    "comment": "physical repair retry",
                    "escalation_evidence": _evidence(self.item.item_key),
                },
                transition_id=self.transition.id,
            )

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "blocked")
        self.assertEqual(self.item.assignment_group, "human-ops")
        self.assertFalse(self.item.assignee_id)
        self.assertEqual(
            EscalationRoutingDisposition.objects.filter(item=self.item).count(),
            1,
        )
        self.assertTrue(getattr(second, "_escalation_receipt_payload", {}).get("replayed"))
        # Must not have raised WorkflowValidationError(escalation_hold_commit_failed).
        self.assertNotEqual(
            getattr(second, "_escalation_receipt_payload", {}).get("reason_code"),
            "escalation_hold_commit_failed",
        )
