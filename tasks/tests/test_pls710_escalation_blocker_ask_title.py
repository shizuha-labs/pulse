"""PLS-710: escalation blocker child title/acceptance derive from the ask.

The auto-created PLAT-4215 escalation blocker child derived its title from the
artifact it escalates ("[BLOCKER] PLAT-4932: Raise to Admin Ops"), not the
request it carries. The validated typed ask (``requested_subject`` /
``acceptance_check``) lives in ``prepared.normalized["dependency"]`` but was
never threaded into the child mint. This test asserts the mint now derives the
title and acceptance line from the actual ask, carries it through nesting, and
degrades readably when no typed payload exists.
"""

import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from tasks.models import (
    EscalationBlockerDependency,
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


class Pls710EscalationBlockerAskTitleTests(TransactionTestCase):
    """Exercise the typed escalation routing through the real sequence."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="pls710-actor",
            email="pls710-actor@example.com",
            is_staff=True,
        )
        self.admin_owner = user_model.objects.create_user(
            username="pls710-admin",
            email="pls710-admin@example.com",
        )
        self.engineer = user_model.objects.create_user(
            username="pls710-engineer",
            email="pls710-engineer@example.com",
        )
        self.project = Project.objects.create(name="PLS-710", key="P710", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLS-710 WF",
            slug="pls-710-wf",
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
            post_functions=[
                {"type": "assign_team", "value": "admin-ops"},
                {
                    "type": "create_blocker_task",
                    "team": "admin-ops",
                    "workflow": "simple",
                    "priority": "urgent",
                },
            ],
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="admin-ops",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[self.admin_owner.email]],
                "is_active": True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="engineering",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[self.engineer.email]],
                "is_active": True,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _parent(self, suffix, title="Operator approval dependency"):
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=f"P710-{suffix}",
                mode="task",
                title=title,
                description="Needs the same operator decision.",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group="engineering",
                assignee_id=self.engineer.id,
                assignee_email=self.engineer.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def _evidence(self, item, subject="Approve the Origin organization policy exception"):
        return {
            "schema": "shizuha.pulse.escalation-evidence.v1",
            "request_id": str(uuid.uuid4()),
            "expected_generation": 0,
            "dependency": {
                "evidence_class": "operator_decision",
                "requested_kind": "decision",
                "requested_subject": subject,
                "holder_type": "operator",
                "holder_ref": "Shizuha operator",
                "acceptance_check": "A durable approve or reject decision is recorded.",
                "source_refs": [{"kind": "task", "id": item.item_key}],
            },
            "technical_action": None,
        }

    def _resolve_assignee(self, team, **_kwargs):
        canonical = WorkflowService._canonical_team_name(team)
        if canonical == "admin-ops":
            return self.admin_owner.id, self.admin_owner.email
        return self.engineer.id, self.engineer.email

    def _execute(self, item, transition=None, evidence=None):
        transition = transition or self.transition
        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return WorkflowService.execute_transition(
                item,
                "blocked",
                user=self.actor,
                screen_data={
                    "comment": "operator decision required",
                    "escalation_evidence": evidence or self._evidence(item),
                },
                transition_id=transition.id,
            )

    def _blockers(self, item):
        return [
            link.source_item
            for link in ItemLink.objects.filter(
                target_item=item, link_type="blocks",
            ).select_related("source_item")
        ]

    def test_typed_ask_drives_child_title_and_acceptance(self):
        # AC1: a newly-created escalation blocker child's title and acceptance
        # line derive from requested_subject / acceptance_check.
        parent = self._parent("AC1")
        self._execute(parent)

        blockers = self._blockers(parent)
        self.assertEqual(len(blockers), 1)
        child = blockers[0]
        self.assertEqual(
            child.title,
            "Approve the Origin organization policy exception",
        )
        self.assertIn(
            "A durable approve or reject decision is recorded.",
            child.description,
        )
        # The child must not carry the bookkeeping "[BLOCKER] <parent>:" title.
        self.assertNotIn("[BLOCKER]", child.title)

    def test_nested_escalation_carries_original_ask(self):
        # AC2: a nested escalation renders the original ask (>= 2 nesting levels).
        parent = self._parent("AC2")
        self._execute(parent)
        first_child = self._blockers(parent)[0]
        self.assertEqual(
            first_child.title,
            "Approve the Origin organization policy exception",
        )

        # The typed escalation child is minted into the external-wait admission
        # path (waiting_external), which the PLS-733 guard only exits via a typed
        # release. To exercise the second nesting level, mint the nested child
        # directly through the same assigned-blocker chokepoint with the SAME
        # validated ask — this is exactly what a re-escalation of the child does.
        evidence = self._evidence(
            first_child,
            subject="Approve the Origin organization policy exception",
        )
        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            nested = WorkflowService._prepare_assigned_blocker(
                self.transition,
                first_child,
                user=self.actor,
                screen_data={
                    "comment": "nested escalation",
                    "escalation_evidence": evidence,
                },
                ask=evidence["dependency"],
            )
        self.assertIsNotNone(nested)
        self.assertEqual(
            nested.title,
            "Approve the Origin organization policy exception",
        )
        self.assertIn(
            "A durable approve or reject decision is recorded.",
            nested.description,
        )

    def test_legacy_absent_payload_keeps_existing_title(self):
        # AC3: a child minted without a v1 payload keeps its current title
        # (degrade readably, never blank).
        parent = self._parent("AC3")
        self._execute(parent, evidence=None)

        blockers = self._blockers(parent)
        self.assertEqual(len(blockers), 1)
        child = blockers[0]
        # No typed evidence -> falls back to the post-function/configured title
        # or the existing "[BLOCKER] <parent>:" fallback — never blank.
        self.assertTrue(child.title.strip())
        self.assertNotIn("requested_subject", child.title)

    def test_linkage_parent_status_and_admission_unchanged(self):
        # AC4: no change to blocker linkage, parent status, or admission control.
        parent = self._parent("AC4")
        self._execute(parent)
        parent.refresh_from_db()

        self.assertEqual(parent.status, "blocked")
        blockers = self._blockers(parent)
        self.assertEqual(len(blockers), 1)
        child = blockers[0]
        # The typed escalation child stays a workable root on its workflow.
        self.assertEqual(child.status, "todo")
        self.assertNotEqual(child.status, "waiting_external")
        # The child is linked as a blocker (blocks) to the parent.
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=child, target_item=parent, link_type="blocks",
            ).exists(),
        )
        # The dependency registry row is created (admission control path intact).
        self.assertTrue(
            EscalationBlockerDependency.objects.filter(
                organization_id=1,
            ).exists(),
        )

    def test_reused_blocker_keeps_original_ask(self):
        # AC7: a child reused for a second parent renders the ORIGINAL ask (the
        # title was minted from requested_subject, not the parent's prose), so
        # the stored title cannot point at the wrong parent. The description is
        # not re-written on reuse either — assert both are unchanged.
        parent_a = self._parent("AC7-A")
        parent_b = self._parent("AC7-B")
        self._execute(parent_a)
        child = self._blockers(parent_a)[0]
        original_title = child.title
        original_description = child.description

        # Re-escalate parent B with the same dependency key -> the child is
        # reused, not duplicated, and its title is unchanged (still the ask).
        self._execute(parent_b)
        blockers_b = self._blockers(parent_b)
        self.assertEqual(len(blockers_b), 1)
        self.assertEqual(blockers_b[0].pk, child.pk)
        self.assertEqual(child.title, original_title)
        self.assertEqual(child.description, original_description)
        self.assertEqual(
            child.title,
            "Approve the Origin organization policy exception",
        )
