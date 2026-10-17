"""PLS-525: production-order escalation holds mint before parent blocked."""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from tasks.models import (
    EscalationRoutingDisposition,
    EscalationRoutingReceipt,
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services import escalation_routing
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class Pls525EscalationHoldBlockerTests(TransactionTestCase):
    """Exercise the real WorkflowService -> typed-routing transaction sequence."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="pls525-actor",
            email="pls525-actor@example.com",
            is_staff=True,
        )
        self.owner = user_model.objects.create_user(
            username="pls525-owner",
            email="pls525-owner@example.com",
        )
        self.project = Project.objects.create(name="PLS-525", key="P525", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLS-525 WF",
            slug="pls-525-wf",
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=blocked_status, position=1)
        # Intentionally no create_blocker_task post-function: both normal HOLD
        # and forced unexpected-HOLD recovery must synthesize it transactionally.
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Raise to Admin Ops",
            from_status="open",
            to_status="blocked",
            post_functions=[{"type": "assign_team", "value": "admin-ops"}],
        )
        for team in ("engineering", "admin-ops", "devops"):
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
        # Organization membership is enforced by the live Admin authority,
        # which is intentionally unavailable in this isolated transaction test.
        # Keep that unrelated boundary deterministic; TeamRoster still supplies
        # the canonical target-team validation exercised below.
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=f"P525-{suffix}",
                mode="task",
                title="Escalation hold parent",
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

    def _configure_blocker_team(self, team):
        self.transition.post_functions = [
            {"type": "assign_team", "value": "admin-ops"},
            {
                "type": "create_blocker_task",
                "team": team,
                "workflow": "simple",
                "priority": "urgent",
            },
        ]
        self.transition.save(update_fields=["post_functions"])

    def _assert_one_owned_blocker(self, item, *, team):
        item.refresh_from_db()
        self.assertEqual(item.status, "blocked")
        links = ItemLink.objects.filter(target_item=item, link_type="blocks")
        self.assertEqual(links.count(), 1)
        blocker = links.get().source_item
        self.assertEqual(blocker.assignment_group, team)
        self.assertEqual(blocker.assignee_id, self.owner.id)
        self.assertEqual(blocker.assignee_email, self.owner.email)
        self.assertTrue(WorkflowService._has_active_assigned_blocker(item))
        return blocker

    def test_mixed_hold_without_post_function_uses_top_level_transaction(self):
        result = self._execute(self.item)

        blocker = self._assert_one_owned_blocker(result, team="engineering")
        self.assertEqual(blocker.metadata["auto_blocker_for"], self.item.item_key)
        disposition = EscalationRoutingDisposition.objects.get(item=self.item)
        self.assertEqual(disposition.classification, "mixed_or_unknown_escalation_hold")
        self.assertEqual(disposition.reason_code, "missing_evidence_schema")

    def test_valid_non_allowlisted_captured_team_is_preserved(self):
        item = self._make_item("2", group="devops")

        result = self._execute(item)

        self._assert_one_owned_blocker(result, team="devops")
        result.refresh_from_db()
        self.assertEqual(result.assignment_group, "devops")

    def test_configured_blocker_uses_same_canonical_team_as_normal_hold_parent(self):
        self._configure_blocker_team("admin-ops")

        result = self._execute(self.item)

        blocker = self._assert_one_owned_blocker(result, team="engineering")
        result.refresh_from_db()
        self.assertEqual(result.assignment_group, blocker.assignment_group)

    def test_configured_blocker_uses_same_canonical_team_as_forced_hold_parent(self):
        self._configure_blocker_team("admin-ops")

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
        ), patch.object(
            escalation_routing,
            "_prepare_evidence",
            side_effect=RuntimeError("injected classifier failure"),
        ):
            result = WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={"comment": "hold"},
                transition_id=self.transition.id,
            )

        blocker = self._assert_one_owned_blocker(result, team="engineering")
        result.refresh_from_db()
        self.assertEqual(result.assignment_group, blocker.assignment_group)

    def test_stale_captured_team_falls_back_consistently_to_admin_ops(self):
        item = self._make_item("stale", group="phantom-team")

        result = self._execute(item)

        self._assert_one_owned_blocker(result, team="admin-ops")
        result.refresh_from_db()
        self.assertEqual(result.assignment_group, "admin-ops")

    def test_other_org_team_falls_back_consistently_to_admin_ops(self):
        TeamRoster.objects.create(
            organization_id=2,
            organization_slug="other-org",
            team_name="foreign-only",
            tiers=[[self.owner.email]],
            is_active=True,
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._make_item("foreign", group="foreign-only")

        result = self._execute(item)

        self._assert_one_owned_blocker(result, team="admin-ops")
        result.refresh_from_db()
        self.assertEqual(result.assignment_group, "admin-ops")

    def test_forced_unexpected_hold_uses_top_level_recovery_path(self):
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
        ), patch.object(
            escalation_routing,
            "_prepare_evidence",
            side_effect=RuntimeError("injected classifier failure"),
        ):
            result = WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={"comment": "hold"},
                transition_id=self.transition.id,
            )

        self._assert_one_owned_blocker(result, team="engineering")
        disposition = EscalationRoutingDisposition.objects.get(item=self.item)
        self.assertEqual(disposition.classification, "mixed_or_unknown_escalation_hold")
        self.assertEqual(disposition.reason_code, "unexpected_error")

    def test_injected_link_failure_rolls_back_child_link_parent_and_receipt(self):
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
        ), patch.object(
            ItemLink,
            "save",
            side_effect=RuntimeError("injected link failure"),
        ), self.assertRaises(WorkflowValidationError) as raised:
            WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={"comment": "hold"},
                transition_id=self.transition.id,
            )

        self.assertEqual(raised.exception.code, "escalation_hold_commit_failed")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "open")
        self.assertFalse(
            Item.objects.filter(metadata__auto_blocker_for=self.item.item_key).exists()
        )
        self.assertFalse(
            ItemLink.objects.filter(target_item=self.item, link_type="blocks").exists()
        )
        self.assertFalse(EscalationRoutingDisposition.objects.filter(item=self.item).exists())
        self.assertFalse(EscalationRoutingReceipt.objects.filter(item=self.item).exists())

    def test_repeated_top_level_attempt_replays_one_child_and_link(self):
        first = self._execute(self.item)
        second = self._execute(first)

        self._assert_one_owned_blocker(second, team="engineering")
        self.assertEqual(
            Item.objects.filter(metadata__auto_blocker_for=self.item.item_key).count(),
            1,
        )
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=self.item).count(), 1)
        self.assertEqual(EscalationRoutingReceipt.objects.filter(item=self.item).count(), 1)
        self.assertTrue(second._escalation_receipt_payload["replayed"])

    @skipUnless(connection.vendor == "postgresql", "row-lock concurrency requires PostgreSQL")
    def test_concurrent_top_level_attempts_converge_on_one_child_and_link(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            try:
                item = Item.objects.get(pk=self.item.pk)
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=5)
                result = WorkflowService.execute_transition(
                    item,
                    "blocked",
                    user=actor,
                    screen_data={"comment": "hold"},
                    transition_id=self.transition.id,
                )
                results.append(result._escalation_receipt_payload)
            except Exception as exc:  # surfaced below with the concrete exception
                errors.append(exc)
            finally:
                close_old_connections()

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
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self._assert_one_owned_blocker(self.item, team="engineering")
        self.assertEqual(
            Item.objects.filter(metadata__auto_blocker_for=self.item.item_key).count(),
            1,
        )
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=self.item).count(), 1)
        self.assertEqual(EscalationRoutingReceipt.objects.filter(item=self.item).count(), 1)
        self.assertEqual(sum(bool(result["replayed"]) for result in results), 1)
