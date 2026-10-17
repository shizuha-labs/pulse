"""PLAT-7292 reopen: Raise to Admin Ops 503s escalation_hold_commit_failed.

Live signature (san, 09-19, PLAT-9276, 4 occurrences): the typed
"Raise to Admin Ops" transition with a VALID operator_dependency envelope
503'd while ordinary transitions succeeded minutes prior. Unlike the
original human-ops variant (seat unresolvable -> _assign_group raised),
admin-ops HAS a resolvable member — the failure must live elsewhere in the
typed plan / forced-hold chain.
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
from tasks.services.workflow_service import WorkflowService


def _operator_dependency_evidence(item_key: str) -> dict:
    """san's PLAT-9276 payload shape: operator-held bootstrap step."""
    return {
        "schema": "shizuha.pulse.escalation-evidence.v1",
        "request_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "expected_generation": 0,
        "dependency": {
            "evidence_class": "human_held_source_evidence",
            "requested_kind": "artifact",
            "requested_subject": "apply k3s/origin/rbac-manager-sa.yaml @ pinned master",
            "holder_type": "operator",
            "holder_ref": "operator@example.test",
            "acceptance_check": "SA exists and org secret provisioned",
            "source_refs": [{"kind": "task", "id": item_key}],
        },
        "technical_action": None,
    }


class Plat7292AdminOpsVariantTests(TransactionTestCase):
    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="plat7292-admin-actor",
            email="plat7292-admin-actor@example.com",
            is_staff=True,
        )
        self.operator = user_model.objects.create_user(
            username="plat7292-operator",
            email="operator@example.com",
            is_staff=True,
        )
        self.project = Project.objects.create(name="PLAT-7292B", key="P7292B", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLAT-7292B WF",
            slug="plat-7292b-wf",
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
        for team in ("engineering", "admin-ops", "ceo-decisions"):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team,
                defaults={
                    "organization_slug": "shizuha",
                    "tiers": [[self.actor.email, self.operator.email]],
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
                item_key="P7292B-1",
                mode="task",
                title="operator bootstrap leg",
                description="operator-held",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group="engineering",
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def test_admin_ops_escalation_commits_with_resolvable_seat(self):
        """Raise to Admin Ops with a VALID operator_dependency envelope and a
        resolvable admin-ops seat must commit the typed escalation — not 503."""
        with patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={
                    "comment": "operator bootstrap leg",
                    "escalation_evidence": _operator_dependency_evidence(self.item.item_key),
                },
                transition_id=self.transition.id,
            )

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "blocked")
        payload = getattr(result, "_escalation_receipt_payload", {})
        self.assertNotEqual(
            payload.get("reason_code"), "escalation_hold_commit_failed",
            f"typed escalation 503'd: {payload}",
        )
        self.assertEqual(
            EscalationRoutingDisposition.objects.filter(item=self.item).count(), 1,
        )


class Plat7292HoldBranchGroupOwnedTests(TransactionTestCase):
    """The fail-loud HOLD must commit even when its target group has no
    resolvable seat (PLAT-9276 live shape: item's own group fully at WIP cap).

    `_apply_parent_plan`'s hold branch called `_assign_group` without
    ``allow_group_owned``, so a hold targeting the item's own group raised
    ``target_assignee_unresolved`` inside the forced hold -> 503
    ``escalation_hold_commit_failed``. The 43449cc fix covered the typed-plan
    call site; this is the second call site it missed.
    """

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="plat7292-hold-actor",
            email="plat7292-hold-actor@example.com",
            is_staff=True,
        )
        self.project = Project.objects.create(name="PLAT-7292C", key="P7292C", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLAT-7292C WF",
            slug="plat-7292c-wf",
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
        for team in ("engineering", "admin-ops", "ceo-decisions"):
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
                item_key="P7292C-1",
                mode="task",
                title="operator bootstrap leg (own group at cap)",
                description="operator-held",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group="engineering",
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def test_hold_commits_group_owned_when_own_group_seat_unresolvable(self):
        """First failure in _execute (any unexpected error) + hold target group
        with NO resolvable seat -> the HOLD must still commit group-owned."""
        def _no_seats(group, *args, **kwargs):
            return (None, None)

        import tasks.services.escalation_routing as er

        # Prod shape (PLAT-9276): the typed plan ALSO failed (first failure),
        # sending execute() into the forced-hold path. Simulate with a blocker
        # mint failure that fires ONCE (the hold's own mint succeeds — in prod
        # the first failure was not repeated by the hold path).
        real_prepare = er._prepare_required_blocker
        calls = {"n": 0}

        def _fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("prod blocker mint failure")
            return real_prepare(*args, **kwargs)

        with patch.object(
            er, "_prepare_required_blocker", side_effect=_fail_once,
        ), patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=_no_seats,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = WorkflowService.execute_transition(
                self.item,
                "blocked",
                user=self.actor,
                screen_data={
                    "comment": "operator bootstrap leg",
                    "escalation_evidence": _operator_dependency_evidence(self.item.item_key),
                },
                transition_id=self.transition.id,
            )

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "blocked")
        payload = getattr(result, "_escalation_receipt_payload", {})
        self.assertNotEqual(
            payload.get("reason_code"), "escalation_hold_commit_failed",
            f"fail-loud HOLD itself 503'd: {payload}",
        )
        # The hold parks the parent; when the hold targets the item's own
        # group and an assignee is already set, the existing assignment is
        # preserved (the _assign_group call is skipped entirely) — the
        # contract is COMMIT, not assignee-stripping.
        self.assertEqual(self.item.assignment_group, "engineering")
