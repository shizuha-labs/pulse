from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.deduplication import AlertDeduplicationService
from tasks.views import _resolve_item, _resolve_item_identifier


class AlertOnCallAssignmentTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Alert routing",
            key="ALRT",
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="devops",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [["ni@shizuha.com"]],
                "is_active": True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="engineering",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [["nagi@shizuha.com", "kai@shizuha.com"]],
                "is_active": True,
            },
        )

    def test_project_team_alert_gets_concrete_oncall_without_wip_cap(self):
        def apply_assignee(item, uid, email, update_fields):
            item.assignee_id = uid
            item.assignee_email = email
            update_fields.extend(["assignee_id", "assignee_email"])
            return uid, email

        with mock.patch(
            "tasks.services.workflow_service.WorkflowService._resolve_team_assignee",
            return_value=(73, "nagi@shizuha.com"),
        ) as resolve, mock.patch(
            "tasks.services.workflow_service.WorkflowService._apply_resolved_assignee",
            side_effect=apply_assignee,
        ), mock.patch(
            "tasks.services.workflow_service.WorkflowService.assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ), mock.patch(
            "tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment",
            return_value=True,
        ):
            alert, is_new = AlertDeduplicationService.submit_alert({
                "title": "Origin CI failed",
                "source": "origin-ci",
                "source_id": "origin-ci:lane",
                "severity": "error",
                "project_id": self.project.id,
                "assignment_group": "engineering",
            })

        self.assertTrue(is_new)
        alert.refresh_from_db()
        self.assertEqual(alert.assignee_email, "nagi@shizuha.com")
        self.assertEqual(alert.assignee_id, 73)
        self.assertEqual(alert.priority, "urgent")
        self.assertTrue(alert.item_key)
        self.assertTrue(alert.item_key.startswith("ALRT-"))
        self.assertFalse(resolve.call_args.kwargs["enforce_cap"])
        self.assertEqual(resolve.call_args.kwargs.get("item").pk, alert.pk)
        self.assertFalse(resolve.call_args.kwargs.get("allow_wakeable"))
        self.assertTrue(resolve.call_args.kwargs.get("require_live_evidence"))

    def test_resolver_failure_keeps_durable_group_owned_alert(self):
        with mock.patch(
            "tasks.services.workflow_service.WorkflowService._resolve_team_assignee",
            side_effect=RuntimeError("routing unavailable"),
        ):
            alert, is_new = AlertDeduplicationService.submit_alert({
                "title": "Origin CI failed",
                "source": "origin-ci",
                "source_id": "origin-ci:fallback",
                "severity": "error",
                "project_id": self.project.id,
                "assignment_group": "engineering",
            })

        self.assertTrue(is_new)
        self.assertTrue(
            Item.objects.filter(
                id=alert.id,
                mode="alert",
                assignment_group="engineering",
                assignee_email__isnull=True,
            ).exists()
        )

    def test_group_only_alert_without_project_still_gets_oncall(self):
        """Ops-anomaly rows have assignment_group but no project_id."""

        def apply_assignee(item, uid, email, update_fields):
            item.assignee_id = uid
            item.assignee_email = email
            update_fields.extend(["assignee_id", "assignee_email"])
            return uid, email

        with mock.patch(
            "tasks.services.workflow_service.WorkflowService._resolve_team_assignee",
            return_value=(89, "ni@shizuha.com"),
        ) as resolve, mock.patch(
            "tasks.services.workflow_service.WorkflowService._apply_resolved_assignee",
            side_effect=apply_assignee,
        ), mock.patch(
            "tasks.services.workflow_service.WorkflowService.assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ), mock.patch(
            "tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment",
            return_value=True,
        ):
            alert, is_new = AlertDeduplicationService.submit_alert({
                "title": "Pulse repeated notification storm to user 89",
                "source": "pulse-ops-anomaly",
                "source_id": "identical_notification_storm:notification-user-89:",
                "severity": "warning",
                "assignment_group": "devops",
            })

        self.assertTrue(is_new)
        alert.refresh_from_db()
        self.assertIsNone(alert.project_id)
        self.assertEqual(alert.assignee_email, "ni@shizuha.com")
        self.assertEqual(alert.assignee_id, 89)
        self.assertFalse(resolve.call_args.kwargs["enforce_cap"])
        self.assertIsNone(resolve.call_args.kwargs.get("item"))
        self.assertEqual(resolve.call_args.kwargs.get("organization_id"), 1)
        self.assertFalse(resolve.call_args.kwargs.get("allow_wakeable"))

    def test_hibernated_assignee_is_rebound_to_live_oncall(self):
        def apply_assignee(item, uid, email, update_fields):
            item.assignee_id = uid
            item.assignee_email = email
            update_fields.extend(["assignee_id", "assignee_email"])
            return uid, email

        alert = Item.objects.create(
            mode="alert",
            title="Origin CI failed: premerge-ci.yml",
            source="origin-ci",
            source_id="origin-ci:premerge",
            status="firing",
            severity="error",
            priority="high",
            project=self.project,
            assignment_group="engineering",
            assignee_id=13,
            assignee_email="kai@shizuha.com",
        )

        def available(email, **_kwargs):
            return email == "nagi@shizuha.com"

        with mock.patch(
            "tasks.services.workflow_service.WorkflowService._resolve_team_assignee",
            return_value=(73, "nagi@shizuha.com"),
        ) as resolve, mock.patch(
            "tasks.services.workflow_service.WorkflowService._apply_resolved_assignee",
            side_effect=apply_assignee,
        ), mock.patch(
            "tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment",
            side_effect=available,
        ):
            AlertDeduplicationService._ensure_concrete_assignee(alert)

        alert.refresh_from_db()
        self.assertEqual(alert.assignee_email, "nagi@shizuha.com")
        self.assertEqual(alert.priority, "urgent")
        self.assertEqual(resolve.call_args.kwargs.get("exclude_email"), "kai@shizuha.com")

    def test_keyless_project_alert_resolves_via_display_key(self):
        alert = Item.objects.create(
            mode="alert",
            title="Origin CI failed: keyless",
            source="origin-ci",
            status="firing",
            project=self.project,
            assignment_group="engineering",
            item_key=None,
        )
        display = f"{self.project.key}-{alert.pk}"
        self.assertEqual(_resolve_item_identifier(display, mode=None).pk, alert.pk)
        self.assertEqual(_resolve_item(display).pk, alert.pk)
