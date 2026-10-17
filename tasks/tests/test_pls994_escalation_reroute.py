"""PLS-994: re-raising a waiting_external blocker must not 503.

Regression: firing a typed escalation ("Raise to Admin Ops") on a
``waiting_external`` blocker child returned 503 ``escalation_hold_commit_failed``
because (1) the dependency registry's registered blocker IS the item being
re-raised, so the reuse path tried to link the item to itself
(``BLOCKER_CYCLE``), and (2) the Item model refuses any status change away from
``waiting_external`` without an armed exit authority
(``waiting_external_requires_typed_release``).
"""

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from tasks.models import (
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


def _evidence_payload(item_key: str, request_id: str) -> dict:
    return {
        "schema": "shizuha.pulse.escalation-evidence.v1",
        "request_id": request_id,
        "expected_generation": 0,
        "dependency": {
            "evidence_class": "human_held_source_evidence",
            "requested_kind": "artifact",
            "requested_subject": "mint a deploy_verify_waiver",
            "holder_type": "operator",
            "holder_ref": "operator@example.test",
            "acceptance_check": "waiver receipt attached and terminal transition succeeds",
            "source_refs": [{"kind": "task", "id": item_key}],
        },
        "technical_action": None,
    }


class Pls994EscalationReRaiseTests(TransactionTestCase):
    """Re-raising a waiting_external blocker with a typed escalation commits."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="pls994-actor",
            email="pls994-actor@example.com",
            is_staff=True,
        )
        self.owner = user_model.objects.create_user(
            username="pls994-owner",
            email="pls994-owner@example.com",
        )
        self.project = Project.objects.create(name="PLS-994", key="P994", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLS-994 WF",
            slug="pls-994-wf",
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=blocked_status, position=1)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Raise to Admin Ops",
            from_status="open",
            to_status="blocked",
            post_functions=[{"type": "assign_team", "value": "admin-ops"}],
        )
        for team in ("engineering", "admin-ops", "devops", "ceo-decisions"):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team,
                defaults={
                    "organization_slug": "shizuha",
                    "tiers": [[self.actor.email, self.owner.email]],
                    "is_active": True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()
        self.item = self._make_item("1")

    def _make_item(self, suffix, *, group="engineering"):
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=f"P994-{suffix}",
                mode="task",
                title="Escalation re-raise parent",
                description="needs hold",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group=group,
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def _execute(self, item, *, screen_data=None):
        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            "_seat_assignment_allowed",
            return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return WorkflowService.execute_transition(
                item,
                "blocked",
                user=self.actor,
                screen_data=screen_data or {"comment": "hold"},
                transition_id=self.transition.id,
            )

    def test_re_raise_waiting_external_blocker_commits(self):
        """Re-raising a waiting_external blocker with a typed escalation commits."""
        # 1. Escalate the parent with a TYPED escalation -> blocker child
        # admitted to waiting_external in the operator decision team.
        self._execute(
            self.item,
            screen_data={
                "escalation_evidence": _evidence_payload(
                    self.item.item_key, str(uuid.uuid4())
                ),
            },
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "blocked")
        links = ItemLink.objects.filter(target_item=self.item, link_type="blocks")
        self.assertEqual(links.count(), 1)
        blocker = links.get().source_item
        blocker.refresh_from_db()
        self.assertEqual(blocker.status, "todo")
        self.assertNotEqual(blocker.status, "waiting_external")

        # Seed a "Raise to Admin Ops" transition from the blocker's status in
        # the simple workflow (the live DB has one; the test seed does not).
        from tasks.models import Workflow as WF, WorkflowTransition as WT
        wf = WF.objects.get(slug=blocker.workflow_name)
        reroute, _ = WT.objects.get_or_create(
            workflow=wf,
            name="Raise to Admin Ops",
            from_status=blocker.status,
            to_status="blocked",
            defaults={"post_functions": [{"type": "assign_team", "value": "admin-ops"}]},
        )

        # 2. Re-raise the blocker child to Admin Ops with a valid typed
        # escalation. This must commit its own HOLD, not 503.
        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            return_value=(self.owner.id, self.owner.email),
        ), patch.object(
            WorkflowService,
            "_seat_assignment_allowed",
            return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = WorkflowService.execute_transition(
                blocker,
                "blocked",
                user=self.actor,
                screen_data={
                    "escalation_evidence": _evidence_payload(
                        blocker.item_key, str(uuid.uuid4())
                    ),
                },
                transition_id=reroute.id,
            )
        blocker.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        # The re-raise minted a fresh owned blocker child for the re-raised ask.
        child_links = ItemLink.objects.filter(target_item=blocker, link_type="blocks")
        self.assertEqual(child_links.count(), 1)
        self.assertNotEqual(child_links.get().source_item.pk, blocker.pk)
